"""SiteAdapter 路由与门面函数的契约测试。

不触碰真实网络，只验证：
- select_adapter 按 host 正确路由
- is_direct_album_url 与历史行为一致
- build_album_from_url 用 DummyClient 时返回结构正确（taotu/imgbb 路径）
- 不支持的站点抛 ValueError
"""

import unittest

from imgbb_image_mirror.adapters import (
    ImgbbAdapter,
    TaotuAdapter,
    XchinaAdapter,
    select_adapter,
)
from imgbb_image_mirror.downloader import (
    build_album_from_url,
    is_direct_album_url,
)
from tests.test_xchina_support import IMGBB_LISTING_HTML, TAOTU_HTML


class AdapterRoutingTests(unittest.TestCase):
    def test_route_xchina(self):
        self.assertIsInstance(select_adapter("https://xchina.co/photo/id-abc.html"), XchinaAdapter)
        self.assertIsInstance(
            select_adapter("https://xchina.co/photos/model-xyz.html"), XchinaAdapter
        )

    def test_route_taotu(self):
        self.assertIsInstance(select_adapter("https://taotu.org/cosplay/x/demo/"), TaotuAdapter)

    def test_route_imgbb(self):
        self.assertIsInstance(select_adapter("https://ibb.co/album/abc"), ImgbbAdapter)
        self.assertIsInstance(select_adapter("https://imgbb.com/albums"), ImgbbAdapter)

    def test_route_unknown_returns_none(self):
        self.assertIsNone(select_adapter("https://example.com/whatever"))


class DirectAlbumUrlTests(unittest.TestCase):
    def test_xchina_album_is_direct(self):
        self.assertTrue(is_direct_album_url("https://xchina.co/photo/id-69d0cc66ab1f7.html"))

    def test_xchina_listing_is_not_direct(self):
        self.assertFalse(is_direct_album_url("https://xchina.co/photos.html"))
        self.assertFalse(is_direct_album_url("https://xchina.co/photos/model-abc.html"))

    def test_taotu_album_is_direct(self):
        self.assertTrue(is_direct_album_url("https://taotu.org/cosplay/nekokoyoshi/demo/"))

    def test_imgbb_album_is_direct(self):
        self.assertTrue(is_direct_album_url("https://ibb.co/album/abc123"))

    def test_unknown_is_not_direct(self):
        self.assertFalse(is_direct_album_url("https://example.com/whatever"))


class DummyClient:
    def __init__(self, html):
        self.html = html

    def fetch(self, url):
        return self.html


class BuildAlbumTests(unittest.TestCase):
    def test_build_taotu_album(self):
        client = DummyClient(TAOTU_HTML)
        album = build_album_from_url(client, "https://taotu.org/cosplay/nekokoyoshi/demo/")
        self.assertEqual(album["source"], "taotu")
        self.assertTrue(album["name"])

    def test_build_imgbb_album(self):
        client = DummyClient(IMGBB_LISTING_HTML)
        album = build_album_from_url(client, "https://ibb.co/album/abc123")
        self.assertEqual(album["id"], "abc123")
        self.assertEqual(album["source"], "imgbb")

    def test_build_unknown_raises(self):
        with self.assertRaises(ValueError):
            build_album_from_url(DummyClient(""), "https://example.com/x")


if __name__ == "__main__":
    unittest.main()
