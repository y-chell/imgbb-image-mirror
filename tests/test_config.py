"""config.py 的密钥读取优先级测试：环境变量 > keyring > 配置文件。"""

import os
import tempfile
import unittest
from unittest import mock

from imgbb_image_mirror.config import load_config


class ConfigSecretTests(unittest.TestCase):
    def _write_toml(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        self.addCleanup(os.unlink, path)
        return path

    def test_config_file_cookie_used_when_no_env_no_keyring(self):
        path = self._write_toml(
            '[imgbb]\nenabled = true\ncookie = "FROM_FILE"\nauth_token = "TOK_FILE"\n'
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMGBB_COOKIE", None)
            os.environ.pop("IMGBB_TOKEN", None)
            cfg = load_config(path)
        self.assertEqual(cfg.imgbb.cookie, "FROM_FILE")
        self.assertEqual(cfg.imgbb.auth_token, "TOK_FILE")
        self.assertTrue(cfg.imgbb.enabled)

    def test_env_overrides_file(self):
        path = self._write_toml('[imgbb]\ncookie = "FROM_FILE"\n')
        with mock.patch.dict(os.environ, {"IMGBB_COOKIE": "FROM_ENV"}, clear=False):
            os.environ.pop("IMGBB_TOKEN", None)
            cfg = load_config(path)
        self.assertEqual(cfg.imgbb.cookie, "FROM_ENV")

    def test_keyring_used_when_no_env(self):
        path = self._write_toml('[imgbb]\ncookie = "FROM_FILE"\n')
        fake_keyring = mock.MagicMock()
        fake_keyring.get_password.side_effect = lambda service, key: {
            ("imgbb-image-mirror", "cookie"): "FROM_KEYRING",
            ("imgbb-image-mirror", "auth_token"): "TOK_KEYRING",
        }.get((service, key))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMGBB_COOKIE", None)
            os.environ.pop("IMGBB_TOKEN", None)
            with (
                mock.patch("imgbb_image_mirror.config.keyring", fake_keyring),
                mock.patch("imgbb_image_mirror.config._HAS_KEYRING", True),
            ):
                cfg = load_config(path)
        self.assertEqual(cfg.imgbb.cookie, "FROM_KEYRING")
        self.assertEqual(cfg.imgbb.auth_token, "TOK_KEYRING")

    def test_env_overrides_keyring(self):
        path = self._write_toml('[imgbb]\ncookie = "FROM_FILE"\n')
        fake_keyring = mock.MagicMock()
        fake_keyring.get_password.return_value = "FROM_KEYRING"
        with mock.patch.dict(os.environ, {"IMGBB_COOKIE": "FROM_ENV"}, clear=False):
            with (
                mock.patch("imgbb_image_mirror.config.keyring", fake_keyring),
                mock.patch("imgbb_image_mirror.config._HAS_KEYRING", True),
            ):
                cfg = load_config(path)
        # 环境变量优先级最高
        self.assertEqual(cfg.imgbb.cookie, "FROM_ENV")

    def test_keyring_exception_falls_back_to_file(self):
        path = self._write_toml('[imgbb]\ncookie = "FROM_FILE"\n')
        fake_keyring = mock.MagicMock()
        fake_keyring.get_password.side_effect = RuntimeError("no backend")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMGBB_COOKIE", None)
            with (
                mock.patch("imgbb_image_mirror.config.keyring", fake_keyring),
                mock.patch("imgbb_image_mirror.config._HAS_KEYRING", True),
            ):
                cfg = load_config(path)
        self.assertEqual(cfg.imgbb.cookie, "FROM_FILE")

    def test_scraper_fields_loaded(self):
        path = self._write_toml(
            '[scraper]\noutput = "./out"\nworkers = 8\ndelay = 0.25\nmax_pages = 50\n'
        )
        cfg = load_config(path)
        self.assertEqual(cfg.output, "./out")
        self.assertEqual(cfg.workers, 8)
        self.assertEqual(cfg.delay, 0.25)
        self.assertEqual(cfg.max_pages, 50)


if __name__ == "__main__":
    unittest.main()
