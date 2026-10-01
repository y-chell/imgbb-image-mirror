"""browser_bridge 中不依赖真 Chrome 的纯函数测试。"""

import os
import tempfile
import unittest
from unittest import mock

from imgbb_image_mirror.browser_bridge import _read_devtools_endpoint


def _write_endpoint(base: str, chrome_dir: str, port: str, ws: str):
    d = os.path.join(base, "Google", chrome_dir, "User Data")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "DevToolsActivePort"), "w", encoding="utf-8") as f:
        f.write(f"{port}\n{ws}\n")


class ReadDevtoolsEndpointTests(unittest.TestCase):
    def test_prefers_first_existing_candidate(self):
        # 候选顺序 Chrome > Chrome Dev > Chrome Beta > Chrome SxS，取第一个存在的
        with tempfile.TemporaryDirectory() as base:
            _write_endpoint(base, "Chrome SxS", "9333", "/sxs")
            _write_endpoint(base, "Chrome Dev", "9222", "/dev")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": base}):
                self.assertEqual(_read_devtools_endpoint(), "ws://127.0.0.1:9222/dev")

    def test_raises_when_no_candidate(self):
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": base}):
                with self.assertRaisesRegex(RuntimeError, "DevToolsActivePort"):
                    _read_devtools_endpoint()


if __name__ == "__main__":
    unittest.main()
