"""验证 derive_xchina_direct_urls 的位数推断。

真实站点 xchina 的原图直链序号是 5 位（00001.jpg ~ 00117.jpg），
历史代码写死 04d 会导致 0001.jpg 这种 4 位文件名 404。这里锁住从首张
URL 动态推断位数的修复。
"""

import unittest

from imgbb_image_mirror.browser_bridge import derive_xchina_direct_urls


class DeriveDirectUrlsTests(unittest.TestCase):
    def test_five_digit_width_real_site(self):
        # 真实页面首张 preload：00001.jpg（5 位）
        pages = derive_xchina_direct_urls(
            "https://img.xchina.io/photos/6a81abdbc0206/00001.jpg",
            total=117,
            referer_url="https://xchina.co/photo/id-6a81abdbc0206.html",
        )
        self.assertIsNotNone(pages)
        self.assertEqual(len(pages), 117)
        # 首张 5 位
        self.assertEqual(
            pages[0]["direct_url"], "https://img.xchina.io/photos/6a81abdbc0206/00001.jpg"
        )
        self.assertEqual(pages[0]["alt"], "00001.jpg")
        # 末张 5 位（117 -> 00117）
        self.assertEqual(
            pages[-1]["direct_url"], "https://img.xchina.io/photos/6a81abdbc0206/00117.jpg"
        )
        self.assertEqual(pages[-1]["alt"], "00117.jpg")
        # 中间张
        self.assertEqual(
            pages[1]["direct_url"], "https://img.xchina.io/photos/6a81abdbc0206/00002.jpg"
        )
        self.assertEqual(
            pages[50]["direct_url"], "https://img.xchina.io/photos/6a81abdbc0206/00051.jpg"
        )

    def test_three_digit_width(self):
        # 假设某相册首张是 001.jpg（3 位），末张应推导为 117 -> 117（仍是 3 位宽度，不补到 4 位）
        pages = derive_xchina_direct_urls(
            "https://img.xchina.io/photos/abc/001.jpg",
            total=117,
        )
        self.assertIsNotNone(pages)
        self.assertEqual(pages[0]["direct_url"], "https://img.xchina.io/photos/abc/001.jpg")
        self.assertEqual(pages[-1]["direct_url"], "https://img.xchina.io/photos/abc/117.jpg")

    def test_photos2_prefix_also_supported(self):
        # 兼容旧 photos2/ 路径
        pages = derive_xchina_direct_urls(
            "https://img.xchina.io/photos2/abc/0001.jpg",
            total=3,
        )
        self.assertIsNotNone(pages)
        self.assertEqual([p["alt"] for p in pages], ["0001.jpg", "0002.jpg", "0003.jpg"])

    def test_returns_none_on_unmatched_url(self):
        # 不符合预期格式时返回 None，让调用方回退到逐页解析
        self.assertIsNone(derive_xchina_direct_urls("https://example.com/x", total=5))
        self.assertIsNone(derive_xchina_direct_urls("not-a-url", total=5))

    def test_preserves_extension_and_referer(self):
        pages = derive_xchina_direct_urls(
            "https://img.xchina.io/photos/abc/00001.webp",
            total=2,
            referer_url="https://xchina.co/photo/id-abc.html",
        )
        self.assertEqual(pages[0]["alt"], "00001.webp")
        self.assertEqual(pages[0]["referer_url"], "https://xchina.co/photo/id-abc.html")
        self.assertEqual(pages[0]["source"], "xchina")


if __name__ == "__main__":
    unittest.main()
