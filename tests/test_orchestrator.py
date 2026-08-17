"""orchestrator 主入口（mirror_albums / download_albums）的集成测试。

用 mock client/uploader/browser 验证编排逻辑：
- 创建相册 + 上传 + 状态落盘
- 续传：复用已有相册 + 跳过已 uploaded
- cookie 缺失时优雅退出
"""

import os
import tempfile
import unittest
from unittest import mock

from imgbb_image_mirror.metadata import load_metadata_file
from imgbb_image_mirror.orchestrator import (
    mirror_albums,
    mirror_state_path,
    nullcontext_bridge,
    prepare_mirror_metadata,
)


class FakeArgs:
    """模拟 argparse Namespace，mirror_albums 只用 url 和 quiet。"""

    def __init__(self, url="", quiet=True):
        self.url = url
        self.quiet = quiet


class FakeConfig:
    def __init__(self, output, cookie="LID=x; PHPSESSID=y", token="abc123"):
        self.output = output
        self.workers = 2
        self.delay = 0.01
        self.max_pages = 1
        self.imgbb = mock.MagicMock()
        self.imgbb.cookie = cookie
        self.imgbb.auth_token = token
        self.imgbb.enabled = True


class FakeImagePage(dict):
    """带 source 字段的图片页 dict（magictestDict）。"""


class FakeClient:
    """orchestrator 用到的 client 方法桩。"""

    def __init__(self, image_pages):
        self._pages = image_pages


class FakeUploader:
    """记录调用，按预设结果返回。

    results 为空时返回默认成功结果；列表元素是 Exception 实例则抛出
    （用于模拟上传失败，触发 worker 重试），否则作为成功 result 返回。
    """

    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = []
        self.created = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def create_album(self, name):
        a = {
            "id": f"album-{len(self.created)}",
            "url": f"https://ibb.co/album/album-{len(self.created)}",
            "name": name,
        }
        self.created.append(a)
        return a

    def upload_file(self, file_path, album_id, name="", upload_filename=""):
        self.calls.append(("file", file_path, album_id, name))
        return self._next_result()

    def upload_image(self, url, album_id, name=""):
        self.calls.append(("url", url, album_id, name))
        return self._next_result()

    def _next_result(self):
        if self._results:
            r = self._results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return {"url": "https://i.ibb.co/x/y.jpg", "thumb": "t", "viewer": "v"}


class FakeBrowser:
    """download_to_temp 调用 browser.download_xchina_direct。"""

    def __init__(self):
        self.downloads = []

    def download_xchina_direct(self, url, referer, dest):
        self.downloads.append((url, dest))
        with open(dest, "wb") as f:
            f.write(b"fake-image-bytes")

    def download_xchina_image(self, page_url, dest):
        with open(dest, "wb") as f:
            f.write(b"fake-image-bytes")


def _xchina_pages(n, direct=True):
    return [
        {
            "page_url": f"https://xchina.co/photoShow.html?id={i}",
            "thumb": "",
            "alt": f"{i + 1:05d}.jpg",
            "source": "xchina",
            "direct_url": f"https://img.xchina.io/photos/abc/{i + 1:05d}.jpg" if direct else "",
            "referer_url": "https://xchina.co/photo/id-abc.html",
        }
        for i in range(n)
    ]


class MirrorAlbumsTests(unittest.TestCase):
    def _patch_collect(self, pages):
        """让 collect_image_pages 返回固定 pages。"""
        return mock.patch("imgbb_image_mirror.orchestrator.collect_image_pages", return_value=pages)

    def _patch_resolve(self, urls):
        return mock.patch(
            "imgbb_image_mirror.orchestrator.resolve_original_urls", return_value=urls
        )

    def _patch_uploader(self, uploader):
        # mirror_albums 内部 from .uploader import ImgbbUploader，patch 模块级引用
        return mock.patch("imgbb_image_mirror.uploader.ImgbbUploader", return_value=uploader)

    def test_creates_album_and_uploads_all(self):
        with tempfile.TemporaryDirectory() as d:
            pages = _xchina_pages(3)
            urls = [p["direct_url"] for p in pages]
            album = {
                "id": "abc",
                "name": "Test",
                "url": "https://xchina.co/photo/id-abc.html",
                "source": "xchina",
            }
            cfg = FakeConfig(output=d)
            uploader = FakeUploader()  # 全部成功

            with (
                self._patch_collect(pages),
                self._patch_resolve(urls),
                self._patch_uploader(uploader),
                mock.patch(
                    "imgbb_image_mirror.orchestrator.download_to_temp",
                    side_effect=lambda b, a, p, n: _make_tmp_file(n),
                ),
            ):
                results = mirror_albums(
                    client=None,
                    albums=[album],
                    cfg=cfg,
                    args=FakeArgs(url=album["url"]),
                    browser=FakeBrowser(),
                )

            self.assertEqual(len(results), 1)
            r = results[0]
            self.assertEqual(r["total"], 3)
            self.assertEqual(r["success"], 3)
            self.assertEqual(r["failed"], 0)
            self.assertEqual(len(uploader.created), 1)
            self.assertEqual(len(uploader.calls), 3)

            # state 文件落盘正确
            state_path = mirror_state_path(d, album)
            final = load_metadata_file(state_path)
            uploaded = sum(1 for im in final["images"] if im.get("status") == "uploaded")
            self.assertEqual(uploaded, 3)
            self.assertEqual(final["mirror_status"], "completed")
            self.assertTrue(final["mirror_album_id"])

    def test_resume_skips_uploaded(self):
        with tempfile.TemporaryDirectory() as d:
            pages = _xchina_pages(3)
            urls = [p["direct_url"] for p in pages]
            album = {
                "id": "abc",
                "name": "Test",
                "url": "https://xchina.co/photo/id-abc.html",
                "source": "xchina",
            }
            state_path = mirror_state_path(d, album)

            # 预置 state：第 0 张已上传，相册已建
            metadata, _ = prepare_mirror_metadata(state_path, album, album["url"], pages, urls)
            metadata["mirror_album_id"] = "existing-album"
            metadata["mirror_album_url"] = "https://ibb.co/album/existing-album"
            from imgbb_image_mirror.metadata import mark_uploaded, save_metadata_file

            save_metadata_file(state_path, metadata)
            mark_uploaded(
                metadata, 0, {"url": "https://i.ibb.co/old/00001.jpg", "thumb": "t", "viewer": "v"}
            )
            save_metadata_file(state_path, metadata)

            cfg = FakeConfig(output=d)
            uploader = FakeUploader()  # 续传2张全部成功

            with (
                self._patch_collect(pages),
                self._patch_resolve(urls),
                self._patch_uploader(uploader),
                mock.patch(
                    "imgbb_image_mirror.orchestrator.download_to_temp",
                    side_effect=lambda b, a, p, n: _make_tmp_file(n),
                ),
            ):
                results = mirror_albums(
                    client=None,
                    albums=[album],
                    cfg=cfg,
                    args=FakeArgs(url=album["url"]),
                    browser=FakeBrowser(),
                )

            r = results[0]
            self.assertEqual(r["total"], 3)
            self.assertEqual(r["success"], 3)
            self.assertEqual(r["failed"], 0)
            # 没新建相册（复用 existing-album）
            self.assertEqual(len(uploader.created), 0)
            # 只上传了 idx 1, 2（idx 0 跳过）
            self.assertEqual(len(uploader.calls), 2)

    def test_no_cookie_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            album = {
                "id": "abc",
                "name": "Test",
                "url": "https://xchina.co/photo/id-abc.html",
                "source": "xchina",
            }
            cfg = FakeConfig(output=d, cookie="", token="")
            results = mirror_albums(
                client=None, albums=[album], cfg=cfg, args=FakeArgs(url=album["url"])
            )
            self.assertEqual(results, [])

    def test_empty_album_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            album = {
                "id": "abc",
                "name": "Empty",
                "url": "https://xchina.co/photo/id-abc.html",
                "source": "xchina",
            }
            cfg = FakeConfig(output=d)
            uploader = FakeUploader(results=[])

            with self._patch_collect([]), self._patch_uploader(uploader):
                results = mirror_albums(
                    client=None, albums=[album], cfg=cfg, args=FakeArgs(url=album["url"])
                )
            r = results[0]
            self.assertEqual(r["total"], 0)
            self.assertEqual(len(uploader.created), 0)


def _make_tmp_file(name):
    """给 download_to_temp 的桩：返回一个有内容的临时文件路径。"""
    import tempfile as _t

    fd, path = _t.mkstemp(suffix=os.path.splitext(name)[1] or ".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(b"fake")
    return path


class NullcontextBridgeTests(unittest.TestCase):
    def test_nullcontext_works(self):
        with nullcontext_bridge() as v:
            self.assertIsNone(v)


if __name__ == "__main__":
    unittest.main()
