import os
import json
import re
from html import unescape
from urllib.parse import urljoin, urlparse


def parse_albums_from_page(html: str) -> list[dict]:
    items = re.findall(
        r'data-type="album"\s+data-id="([^"]+)"\s+data-name="([^"]+)".*?'
        r'href="([^"]+)".*?<img\s+src="([^"]+)"',
        html, re.DOTALL
    )
    return [{"id": aid, "name": name, "url": href, "thumb": thumb}
            for aid, name, href, thumb in items]


def parse_image_pages_from_album(html: str) -> list[dict]:
    pairs = re.findall(
        r'<a\s+href="(https://ibb\.co/[A-Za-z0-9]+)"\s+class="image-container[^"]*">'
        r'<img\s+src="(https://i\.ibb\.co/[^"]+)"[^>]*alt="([^"]*)"',
        html
    )
    seen = set()
    results = []
    for page_url, thumb, alt in pairs:
        if page_url not in seen:
            seen.add(page_url)
            results.append({"page_url": page_url, "thumb": thumb, "alt": alt})
    return results


def parse_original_url(html: str) -> str | None:
    match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
    return match.group(1) if match else None


def parse_next_page_url(html: str, base_url: str) -> str | None:
    match = re.search(r'data-pagination="next"[^>]*href="([^"]+)"', html)
    if not match:
        match = re.search(r'href="([^"]+)"[^>]*data-pagination="next"', html)
    if not match:
        match = re.search(r'<a[^>]*class="[^"]*\bnext\b[^"]*"[^>]*href="([^"]+)"', html)
    if not match:
        match = re.search(r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*\bnext\b[^"]*"', html)
    if match:
        url = match.group(1)
        if url.startswith("/"):
            parsed = urlparse(base_url)
            url = f"{parsed.scheme}://{parsed.netloc}{url}"
        return url
    return None


def parse_album_name(html: str) -> str | None:
    match = re.search(r'data-name="([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'og:title.*?content="([^"]+)"', html)
    if match:
        return match.group(1)
    return None


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen = set()
    results = []
    for value in values:
        if value not in seen:
            seen.add(value)
            results.append(value)
    return results


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


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
    meta_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    if meta_match:
        title = _clean_text(meta_match.group(1))
    else:
        title_match = re.search(r"<title>([^<]+)</title>", html)
        if not title_match:
            return None
        title = _clean_text(title_match.group(1))
    return re.sub(r"\s*-\s*套图\s*$", "", title).strip()


def parse_xchina_albums_from_page(html: str, base_url: str) -> list[dict]:
    pattern = re.compile(
        r'<div class="item photo">.*?'
        r'<a href="(?P<href>/photo/id-[^"]+\.html)"\s+title="(?P<title>[^"]+)">.*?'
        r"background-image:url\('(?P<thumb>https://img\.xchina\.io/[^']+)'\).*?"
        r'<div class="tags"><div>(?P<meta>[^<]+)</div>',
        re.DOTALL,
    )
    albums = []
    for match in pattern.finditer(html):
        href = match.group("href")
        album_id_match = re.search(r"/photo/id-([^.]+)\.html", href)
        if not album_id_match:
            continue
        count_match = re.search(r"(\d+)P", match.group("meta"))
        albums.append({
            "id": album_id_match.group(1),
            "name": _clean_text(match.group("title")),
            "url": urljoin(base_url, href),
            "thumb": match.group("thumb"),
            "count": int(count_match.group(1)) if count_match else 0,
            "source": "xchina",
        })
    return albums


def parse_xchina_photo_pages(html: str, base_url: str) -> list[dict]:
    raw_links = re.findall(r'/photoShow\.html\?id=[^"\']+', html)
    links = _dedupe_keep_order(raw_links)
    return [{
        "page_url": urljoin(base_url, href),
        "thumb": "",
        "alt": "",
        "source": "xchina",
    } for href in links]


def parse_taotu_image_pages(html: str, base_url: str) -> list[dict]:
    pattern = re.compile(
        r'<a[^>]+href="(?P<href>https://res\.taotu\.org/[^"]+\.(?:jpg|jpeg|png|webp))"[^>]*>\s*'
        r'<img[^>]+src="(?P<thumb>https://res\.taotu\.org/[^"]+/thumbnail/[^"]+\.(?:jpg|jpeg|png|webp))"[^>]*'
        r'alt="(?P<alt>[^"]*)"',
        re.IGNORECASE | re.DOTALL,
    )
    seen = set()
    pages = []
    for match in pattern.finditer(html):
        href = match.group("href")
        if href in seen:
            continue
        seen.add(href)
        alt = _clean_text(match.group("alt"))
        filename = os.path.basename(urlparse(href).path)
        pages.append({
            "page_url": href,
            "thumb": match.group("thumb"),
            "alt": filename if filename else alt,
            "source": "taotu",
            "direct_url": href,
            "referer_url": base_url,
        })
    return pages


def parse_xchina_album_name(html: str) -> str | None:
    meta_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    if meta_match:
        return _clean_xchina_album_title(meta_match.group(1))

    title_match = re.search(r"<title>([^<]+)</title>", html)
    if title_match:
        return _clean_xchina_album_title(title_match.group(1))
    return None


def parse_xchina_original_url(html: str) -> str | None:
    preload_match = re.search(
        r'<link\s+rel="preload"\s+as="image"\s+href="(https://img\.xchina\.io/photos2/[^"]+)"',
        html,
    )
    if preload_match:
        return preload_match.group(1)

    json_ld_match = re.search(
        r'"contentUrl"\s*:\s*"(?P<url>https:\\/\\/img\.xchina\.io\\/photos2\\/[^\"]+)"',
        html,
    )
    if json_ld_match:
        return json.loads(f'"{json_ld_match.group("url")}"')

    img_match = re.search(r'<img[^>]+src="(https://img\.xchina\.io/photos2/[^"]+)"', html)
    if img_match:
        return img_match.group(1)
    return None
