import unittest
from unittest import mock

from imgbb_image_mirror.__main__ import (
    _build_upload_name,
    _get_cached_original_urls,
    _mirror_state_path,
    _prepare_mirror_metadata,
)
from imgbb_image_mirror.downloader import (
    build_album_from_url,
    get_download_headers,
    scrape_album_list,
    collect_image_pages,
)
from imgbb_image_mirror.parser import (
    parse_xchina_albums_from_page,
    parse_xchina_album_name,
    parse_next_page_url,
    parse_taotu_album_name,
    parse_taotu_image_pages,
    parse_xchina_original_url,
    parse_xchina_photo_pages,
)


LISTING_HTML = """
<div class="list photo-home" pc-cols="3">
  <div class="item photo">
    <a href="/photo/id-69d0cc66ab1f7.html" title="Tenter Touchs清纯系女主播的吃鸡日记">
      <div role="img" class="img" style="background-image:url('https://img.xchina.io/photos2/69d0cc66ab1f7/0005_600x0.webp');"></div>
    </a>
    <div class="text">
      <div class="title"><a href="/photo/id-69d0cc66ab1f7.html">Tenter Touchs清纯系女主播的吃鸡日记</a></div>
    </div>
    <div class="tags"><div>15P + 1V</div></div>
  </div>
</div>
"""

DETAIL_HTML = """
<html>
<head>
  <meta property="og:title" content="Tenter Touchs清纯系女主播的吃鸡日记 - 国模套图 - 各国其他套图">
</head>
<body>
  <a href="/photoShow.html?id=aaa"></a>
  <a href="/photoShow.html?id=bbb"></a>
  <a href="/photoShow.html?id=ccc"></a>
</body>
</html>
"""

DETAIL_HTML_PRIVATE = """
<html>
<head>
  <meta property="og:title" content="李丽莎《私购流出》 - 私购流出 - 秀人网旗下 - 小黄书 xChina">
</head>
</html>
"""

MODEL_LISTING_HTML = """
<html>
<body>
  <div class="list photo-home" pc-cols="3">
    <div class="item photo">
      <a href="/photo/id-69d00ea06de0e.html" title="菲伦修道服の写真">
        <div role="img" class="img" style="background-image:url('https://img.xchina.io/photos2/69d00ea06de0e/0001_600x0.webp');"></div>
      </a>
      <div class="text"><div class="title"><a href="/photo/id-69d00ea06de0e.html">菲伦修道服の写真</a></div></div>
      <div class="tags"><div>24P</div></div>
    </div>
  </div>
  <a class="next" href="/photos/model-69ac9ff891d18/2.html"></a>
</body>
</html>
"""

MODEL_LISTING_HTML_PAGE_2 = """
<html>
<body>
  <div class="list photo-home" pc-cols="3">
    <div class="item photo">
      <a href="/photo/id-69acafe152e42.html" title="阿格莱雅の内衣报告">
        <div role="img" class="img" style="background-image:url('https://img.xchina.io/photos2/69acafe152e42/0001_600x0.webp');"></div>
      </a>
      <div class="text"><div class="title"><a href="/photo/id-69acafe152e42.html">阿格莱雅の内衣报告</a></div></div>
      <div class="tags"><div>106P</div></div>
    </div>
  </div>
</body>
</html>
"""

PHOTO_SHOW_HTML = """
<html>
<head>
  <title>Tenter Touchs清纯系女主播的吃鸡日记 (1/15)</title>
  <link rel="preload" as="image" href="https://img.xchina.io/photos2/69d0cc66ab1f7/0001.jpg" fetchpriority="high">
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "ImageObject",
    "contentUrl": "https://img.xchina.io/photos2/69d0cc66ab1f7/0001.jpg",
    "name": "Tenter Touchs清纯系女主播的吃鸡日记 - 第1张"
  }
  </script>
</head>
</html>
"""

TAOTU_HTML = """
<html>
<head>
  <title>爆机少女喵小吉 Nekokoyoshi - 九月番T3会员 NO.048 顶级画风《风铃公主》 - 套图</title>
</head>
<body>
  <a href="https://res.taotu.org/cosplay/nekokoyoshi/demo/0001vhoxvh.jpg">
    <img src="https://res.taotu.org/cosplay/nekokoyoshi/demo/thumbnail/0001.jpg" alt="爆机少女喵小吉 - 0001.jpg">
  </a>
  <a href="https://res.taotu.org/cosplay/nekokoyoshi/demo/0002yyeatc.jpg">
    <img src="https://res.taotu.org/cosplay/nekokoyoshi/demo/thumbnail/0002.jpg" alt="爆机少女喵小吉 - 0002.jpg">
  </a>
</body>
</html>
"""


class DummyClient:
    def __init__(self, html: str | dict[str, str]):
        self.html = html

    def fetch(self, url: str) -> str:
        if isinstance(self.html, dict):
            return self.html[url]
        return self.html


class XChinaParserTests(unittest.TestCase):
    def test_parse_listing(self):
        albums = parse_xchina_albums_from_page(LISTING_HTML, "https://xchina.co/photos.html")
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["id"], "69d0cc66ab1f7")
        self.assertEqual(albums[0]["name"], "Tenter Touchs清纯系女主播的吃鸡日记")
        self.assertEqual(albums[0]["count"], 15)
        self.assertEqual(albums[0]["source"], "xchina")

    def test_parse_detail(self):
        pages = parse_xchina_photo_pages(DETAIL_HTML, "https://xchina.co/photo/id-69d0cc66ab1f7.html")
        self.assertEqual([page["page_url"] for page in pages], [
            "https://xchina.co/photoShow.html?id=aaa",
            "https://xchina.co/photoShow.html?id=bbb",
            "https://xchina.co/photoShow.html?id=ccc",
        ])
        self.assertEqual(parse_xchina_album_name(DETAIL_HTML), "Tenter Touchs清纯系女主播的吃鸡日记")

    def test_parse_detail_private_sale_title(self):
        self.assertEqual(parse_xchina_album_name(DETAIL_HTML_PRIVATE), "李丽莎《私购流出》")

    def test_parse_photo_show(self):
        self.assertEqual(
            parse_xchina_original_url(PHOTO_SHOW_HTML),
            "https://img.xchina.io/photos2/69d0cc66ab1f7/0001.jpg",
        )
        self.assertEqual(
            get_download_headers("https://img.xchina.io/photos2/69d0cc66ab1f7/0001.jpg"),
            {"Referer": "https://xchina.co/"},
        )

    def test_parse_taotu_album_name(self):
        self.assertEqual(
            parse_taotu_album_name(TAOTU_HTML),
            "爆机少女喵小吉 Nekokoyoshi - 九月番T3会员 NO.048 顶级画风《风铃公主》",
        )

    def test_parse_taotu_image_pages(self):
        pages = parse_taotu_image_pages(TAOTU_HTML, "https://taotu.org/demo")
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]["direct_url"], "https://res.taotu.org/cosplay/nekokoyoshi/demo/0001vhoxvh.jpg")
        self.assertEqual(pages[0]["alt"], "0001vhoxvh.jpg")

    def test_build_direct_album(self):
        client = DummyClient(DETAIL_HTML)
        album = build_album_from_url(client, "https://xchina.co/photo/id-69d0cc66ab1f7.html")
        self.assertEqual(album["id"], "69d0cc66ab1f7")
        self.assertEqual(album["name"], "Tenter Touchs清纯系女主播的吃鸡日记")
        self.assertEqual(album["source"], "xchina")

    def test_build_taotu_direct_album(self):
        client = DummyClient(TAOTU_HTML)
        album = build_album_from_url(
            client,
            "https://taotu.org/cosplay/nekokoyoshi/demo/",
        )
        self.assertEqual(album["name"], "爆机少女喵小吉 Nekokoyoshi - 九月番T3会员 NO.048 顶级画风《风铃公主》")
        self.assertEqual(album["source"], "taotu")

    def test_build_upload_name_prefers_numeric_file_name(self):
        image_info = {
            "direct_url": "https://img.xchina.io/photos2/69d0cc66ab1f7/0007.jpg",
            "alt": "任意标题",
        }
        self.assertEqual(
            _build_upload_name(image_info, "", 6),
            "0007.jpg",
        )

    def test_build_upload_name_has_numeric_fallback(self):
        self.assertEqual(
            _build_upload_name({}, "", 11),
            "0012.jpg",
        )

    def test_parse_next_page_url_supports_xchina_next_class(self):
        self.assertEqual(
            parse_next_page_url(MODEL_LISTING_HTML, "https://xchina.co/photos/model-69ac9ff891d18.html"),
            "https://xchina.co/photos/model-69ac9ff891d18/2.html",
        )

    def test_scrape_model_listing_pages(self):
        client = DummyClient({
            "https://xchina.co/photos/model-69ac9ff891d18.html": MODEL_LISTING_HTML,
            "https://xchina.co/photos/model-69ac9ff891d18/2.html": MODEL_LISTING_HTML_PAGE_2,
        })
        albums = scrape_album_list(client, "https://xchina.co/model/id-69ac9ff891d18.html", max_pages=5)
        self.assertEqual([album["id"] for album in albums], ["69d00ea06de0e", "69acafe152e42"])

    def test_collect_taotu_image_pages(self):
        client = DummyClient(TAOTU_HTML)
        pages = collect_image_pages(client, "https://taotu.org/cosplay/nekokoyoshi/demo/")
        self.assertEqual([page["direct_url"] for page in pages], [
            "https://res.taotu.org/cosplay/nekokoyoshi/demo/0001vhoxvh.jpg",
            "https://res.taotu.org/cosplay/nekokoyoshi/demo/0002yyeatc.jpg",
        ])

    def test_mirror_state_path_uses_source_and_album_id(self):
        album = {"id": "69d0cc66ab1f7", "source": "xchina"}
        path = _mirror_state_path("E:/tmp/output", album)
        self.assertTrue(path.endswith("_mirror_state\\xchina_69d0cc66ab1f7.json"))

    def test_prepare_mirror_metadata_preserves_uploaded_entries(self):
        album = {
            "id": "69d0cc66ab1f7",
            "name": "Tenter Touchs清纯系女主播的吃鸡日记",
            "url": "https://xchina.co/photo/id-69d0cc66ab1f7.html",
            "source": "xchina",
        }
        image_pages = [
            {"page_url": "https://xchina.co/photoShow.html?id=1", "thumb": "", "alt": "0001.jpg", "direct_url": "https://img.xchina.io/photos2/69d0cc66ab1f7/0001.jpg"},
            {"page_url": "https://xchina.co/photoShow.html?id=2", "thumb": "", "alt": "0002.jpg", "direct_url": "https://img.xchina.io/photos2/69d0cc66ab1f7/0002.jpg"},
        ]
        original_urls = [
            "https://img.xchina.io/photos2/69d0cc66ab1f7/0001.jpg",
            "https://img.xchina.io/photos2/69d0cc66ab1f7/0002.jpg",
        ]

        state_path = _mirror_state_path("E:/tmp/output", album)
        previous = {
            "album_id": album["id"],
            "album_name": album["name"],
            "album_url": album["url"],
            "source_url": album["url"],
            "mirror_album_id": "abc123",
            "mirror_album_url": "https://ibb.co/album/abc123",
            "images": [
                {
                    "index": 0,
                    "status": "uploaded",
                    "uploaded_url": "https://i.ibb.co/1.jpg",
                    "viewer_url": "https://ibb.co/1",
                    "uploaded_thumb": "https://i.ibb.co/t1.jpg",
                }
            ],
        }

        with mock.patch("imgbb_image_mirror.__main__.load_metadata_file", return_value=previous), \
                mock.patch("imgbb_image_mirror.__main__.save_metadata_file") as save_mock:
            metadata, upload_names = _prepare_mirror_metadata(
                state_path, album, album["url"], image_pages, original_urls
            )

        self.assertEqual(upload_names, ["0001.jpg", "0002.jpg"])
        self.assertEqual(metadata["mirror_album_id"], "abc123")
        self.assertEqual(metadata["mirror_album_url"], "https://ibb.co/album/abc123")
        self.assertEqual(metadata["images"][0]["status"], "uploaded")
        self.assertEqual(metadata["images"][0]["viewer_url"], "https://ibb.co/1")
        self.assertEqual(metadata["images"][1]["status"], "pending")
        save_mock.assert_called_once()

    def test_get_cached_original_urls_reads_ordered_values(self):
        previous = {
            "images": [
                {"index": 1, "original_url": "https://img.xchina.io/photos2/a/0002.jpg"},
                {"index": 0, "original_url": "https://img.xchina.io/photos2/a/0001.jpg"},
            ]
        }
        with mock.patch("imgbb_image_mirror.__main__.load_metadata_file", return_value=previous):
            urls = _get_cached_original_urls("E:/tmp/output/_mirror_state/xchina_a.json", 2)
        self.assertEqual(urls, [
            "https://img.xchina.io/photos2/a/0001.jpg",
            "https://img.xchina.io/photos2/a/0002.jpg",
        ])


if __name__ == "__main__":
    unittest.main()
