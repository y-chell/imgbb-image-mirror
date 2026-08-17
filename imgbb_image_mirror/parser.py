"""站点 HTML 解析。

历史实现全部基于正则，对 HTML 结构变化非常脆弱。这里改用
``parsel`` 的 CSS/XPath 选择器做主解析路径，正则只在以下场景兜底：

- 翻页链接的 ``data-pagination="next"`` 属性（属性名稳定，正则够用）
- xchina 缩略图背景图 URL（CSS 背景图字符串，选择器取不到属性值）
- JSON-LD 内部取值时先定位 ``<script>`` 再 ``json.loads``，不再用正则抠字段

所有对外函数签名与返回结构保持不变，下游 downloader 和测试无需改动。
"""

import json
import os
import re
from html import unescape
from urllib.parse import urljoin, urlparse

from parsel import Selector


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def parse_albums_from_page(html: str) -> list[dict]:
    """imgbb 相册列表页：从 ``data-type="album"`` 卡片里取相册信息。"""
    sel = Selector(html)
    albums = []
    for node in sel.css('[data-type="album"]'):
        aid = node.attrib.get("data-id", "")
        name = node.attrib.get("data-name", "")
        href = node.css("a::attr(href)").get("")
        thumb = node.css("img::attr(src)").get("")
        if not aid or not href:
            continue
        albums.append({"id": aid, "name": name, "url": href, "thumb": thumb})
    return albums


def parse_image_pages_from_album(html: str) -> list[dict]:
    """imgbb 相册内页：取每张图片的展示页、缩略图、alt。"""
    sel = Selector(html)
    results = []
    seen = set()
    for a in sel.css("a.image-container"):
        page_url = a.attrib.get("href", "")
        img = a.css("img")
        thumb = img.attrib.get("src", "") if img else ""
        alt = img.attrib.get("alt", "") if img else ""
        if not page_url or page_url in seen:
            continue
        seen.add(page_url)
        results.append({"page_url": page_url, "thumb": thumb, "alt": alt})
    return results


def parse_original_url(html: str) -> str | None:
    """imgbb 单图页：原图优先取 og:image。"""
    sel = Selector(html)
    return sel.css('meta[property="og:image"]::attr(content)').get()


def parse_next_page_url(html: str, base_url: str) -> str | None:
    """通用翻页：优先 data-pagination="next"，其次带 next class 的 <a>。"""
    sel = Selector(html)

    href = sel.css('[data-pagination="next"]::attr(href)').get()
    if not href:
        # 属性书写顺序不固定，两种都试
        href = sel.css('a[data-pagination="next"]::attr(href)').get()
    if not href:
        href = sel.css('a.next::attr(href), a[class*="next"]::attr(href)').get()

    if not href:
        return None
    if href.startswith("/"):
        parsed = urlparse(base_url)
        href = f"{parsed.scheme}://{parsed.netloc}{href}"
    return href


def parse_album_name(html: str) -> str | None:
    """imgbb 相册名：优先 data-name，其次 og:title。"""
    sel = Selector(html)
    name = sel.css("[data-name]::attr(data-name)").get()
    if name:
        return _clean_text(name)
    title = sel.css('meta[property="og:title"]::attr(content)').get()
    return _clean_text(title) if title else None


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen = set()
    results = []
    for value in values:
        if value not in seen:
            seen.add(value)
            results.append(value)
    return results


def _clean_xchina_album_title(text: str) -> str:
    title = _clean_text(text)
    title = re.sub(r"\s*-\s*小黄书 xChina$", "", title).strip()
    parts = [part.strip() for part in title.split(" - ") if part.strip()]
    if len(parts) <= 1:
        return title

    suffix_markers = {
        "私购流出",
        "秀人网旗下",
        "秀人网",
    }
    if all(part in suffix_markers or "套图" in part for part in parts[1:]):
        return parts[0]
    return title


def parse_taotu_album_name(html: str) -> str | None:
    sel = Selector(html)
    title = sel.css('meta[property="og:title"]::attr(content)').get()
    if not title:
        title = sel.css("title::text").get()
    if not title:
        return None
    title = _clean_text(title)
    return re.sub(r"\s*-\s*套图\s*$", "", title).strip()


def parse_xchina_albums_from_page(html: str, base_url: str) -> list[dict]:
    """xchina 模特/列表页：每个 ``.item.photo`` 卡片是一个相册。

    缩略图是 CSS 背景图（``background-image:url('...')``），选择器取不到
    属性值，这里对 style 字符串做一次正则。
    """
    sel = Selector(html)
    albums = []
    for node in sel.css("div.item.photo"):
        a = node.css("a[href*='/photo/id-']")
        if not a:
            continue
        href = a.attrib.get("href", "")
        title = a.attrib.get("title", "")

        thumb = ""
        style = node.css("div.img::attr(style)").get() or ""
        thumb_match = re.search(r"background-image:url\('([^']+)'\)", style)
        if thumb_match:
            thumb = thumb_match.group(1)

        meta_text = node.css("div.tags div::text").get("") or ""

        album_id_match = re.search(r"/photo/id-([^.]+)\.html", href)
        if not album_id_match:
            continue
        count_match = re.search(r"(\d+)P", meta_text)
        albums.append(
            {
                "id": album_id_match.group(1),
                "name": _clean_text(title),
                "url": urljoin(base_url, href),
                "thumb": thumb,
                "count": int(count_match.group(1)) if count_match else 0,
                "source": "xchina",
            }
        )
    return albums


def parse_xchina_photo_pages(html: str, base_url: str) -> list[dict]:
    """xchina 相册详情页：收集 photoShow 跳转链接，去重保序。"""
    sel = Selector(html)
    raw_links = sel.css("a::attr(href)").re(r"/photoShow\.html\?id=[^\"']+")
    links = _dedupe_keep_order(raw_links)
    return [
        {
            "page_url": urljoin(base_url, href),
            "thumb": "",
            "alt": "",
            "source": "xchina",
        }
        for href in links
    ]


def parse_taotu_image_pages(html: str, base_url: str) -> list[dict]:
    """taotu 相册页：``<a>`` 包 ``<img>``，原图在 a[href]，缩略图在 img[src]。"""
    sel = Selector(html)
    seen = set()
    pages = []
    for a in sel.css('a[href*="res.taotu.org"]'):
        href = a.attrib.get("href", "")
        if not href or href in seen:
            continue
        img = a.css("img")
        thumb = img.attrib.get("src", "") if img else ""
        alt = _clean_text(img.attrib.get("alt", "")) if img else ""
        filename = os.path.basename(urlparse(href).path)
        seen.add(href)
        pages.append(
            {
                "page_url": href,
                "thumb": thumb,
                "alt": filename if filename else alt,
                "source": "taotu",
                "direct_url": href,
                "referer_url": base_url,
            }
        )
    return pages


def parse_xchina_album_name(html: str) -> str | None:
    sel = Selector(html)
    title = sel.css('meta[property="og:title"]::attr(content)').get()
    if not title:
        title = sel.css("title::text").get()
    if not title:
        return None
    return _clean_xchina_album_title(title)


def _extract_json_ld_image_urls(html: str) -> list[str]:
    """从所有 JSON-LD 块里提取 contentUrl，返回按出现顺序的列表。

    优先结构化解析；只有当某个块本身不是合法 JSON 时才回退到正则。

    注意：xchina 的原图直链用 ``img.xchina.io/photos/{album_id}/NNNN.jpg``，
    缩略图用 ``img.xchina.io/photos2/{cdn_id}/NNNN_600x0.webp``。contentUrl
    指向的是原图（photos/），但这里两个前缀都接受，避免漏取。
    """
    sel = Selector(html)
    urls: list[str] = []
    for block in sel.css('script[type="application/ld+json"]::text').getall():
        text = block.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 兜底：块内嵌套了多余文本时，正则取 contentUrl
            for m in re.finditer(
                r'"contentUrl"\s*:\s*"(https:\\/\\/img\.xchina\.io\\/photos2?\\/[^\"]+)"',
                text,
            ):
                urls.append(json.loads(f'"{m.group(1)}"'))
            continue
        for obj in _iter_json_ld_objects(data):
            url = obj.get("contentUrl")
            if isinstance(url, str) and (
                "img.xchina.io/photos/" in url or "img.xchina.io/photos2/" in url
            ):
                urls.append(url)
    return urls


def _iter_json_ld_objects(data):
    """递归遍历 JSON-LD，产出所有 dict 节点。"""
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _iter_json_ld_objects(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_json_ld_objects(item)


def parse_xchina_original_url(html: str) -> str | None:
    """xchina photoShow 单图页：取当前展示的原图直链。

    优先级：
    1. ``<link rel="preload" as="image">``（首屏预加载，最稳）
    2. JSON-LD ``contentUrl``（结构化数据）
    3. 页面里任一 ``img.xchina.io/photos/`` 的 <img src>

    原图直链路径是 ``img.xchina.io/photos/{album_id}/NNNN.jpg``（注意是
    photos 不是 photos2；photos2 是缩略图 CDN）。
    """
    sel = Selector(html)

    preload = sel.css('link[rel="preload"][as="image"]::attr(href)').get()
    if preload:
        return preload

    json_ld_urls = _extract_json_ld_image_urls(html)
    if json_ld_urls:
        return json_ld_urls[0]

    img = sel.css('img[src*="img.xchina.io/photos/"]::attr(src)').get()
    return img
