"""CLI 入口：参数解析、配置加载、日志配置，然后委托给 orchestrator。

历史上这里把上传/下载编排、队列 worker、续传状态都揉在一起（500+ 行）。
重构后那些逻辑移到 ``orchestrator.py``，本文件只负责：
1. argparse 解析命令行
2. 加载 config.toml 并用 CLI 参数覆盖
3. 决定是否需要浏览器会话
4. 调用 ``orchestrator.download_albums`` 或 ``orchestrator.mirror_albums``
"""

import argparse
import logging
import os
import sys
from contextlib import suppress

from rich.logging import RichHandler

from .browser_bridge import BrowserBridge
from .client import ImgbbClient
from .config import load_config
from .downloader import (
    build_album_from_url,
    is_direct_album_url,
    scrape_album_list,
)
from .orchestrator import (
    console,
    download_albums,
    mirror_albums,
    nullcontext_bridge,
)


def _configure_stdio():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            with suppress(Exception):
                stream.reconfigure(errors="replace")


_configure_stdio()


def setup_logging(quiet: bool = False, log_file: str = "imgbb_image_mirror.log"):
    handlers: list[logging.Handler] = []
    if not quiet:
        rich_handler = RichHandler(console=console, show_path=False, show_time=False)
        rich_handler.setLevel(logging.INFO)
        handlers.append(rich_handler)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers.append(file_handler)

    logging.basicConfig(level=logging.DEBUG, handlers=handlers)
    # 压制第三方库的 debug 日志
    for name in ("httpcore", "httpx", "hpack", "urllib3", "curl_cffi"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="把支持的网站相册镜像到 imgbb")
    parser.add_argument("url", help="用户相册列表页 URL")
    parser.add_argument("--output", "-o", help="下载目录")
    parser.add_argument("--album-id", help="只下载指定相册 ID")
    parser.add_argument("--workers", "-w", type=int, help="并发下载线程数")
    parser.add_argument("--delay", type=float, help="请求间隔(秒)")
    parser.add_argument("--max-pages", type=int, help="最大翻页数")
    parser.add_argument("--dry-run", action="store_true", help="只列出相册不下载")
    parser.add_argument(
        "--mirror", action="store_true", help="镜像模式: 爬取后上传到自己的 imgbb 账号"
    )
    parser.add_argument("--imgbb-cookie", help="imgbb 登录 cookie")
    parser.add_argument("--imgbb-token", help="imgbb auth_token (可选，自动获取)")
    parser.add_argument("--quiet", "-q", action="store_true", help="安静模式")
    parser.add_argument("--config", "-c", help="配置文件路径")
    args = parser.parse_args()

    # 加载配置：TOML 文件 → CLI 参数覆盖
    cfg = load_config(args.config or "config.toml")
    if args.output:
        cfg.output = args.output
    if args.workers:
        cfg.workers = args.workers
    if args.delay is not None:
        cfg.delay = args.delay
    if args.max_pages:
        cfg.max_pages = args.max_pages
    if args.mirror:
        cfg.imgbb.enabled = True
    if args.imgbb_cookie:
        cfg.imgbb.cookie = args.imgbb_cookie
    if args.imgbb_token:
        cfg.imgbb.auth_token = args.imgbb_token

    setup_logging(quiet=args.quiet)
    os.makedirs(cfg.output, exist_ok=True)

    browser_needed = (
        (args.url and "xchina.co" in args.url)
        or (args.album_id and "xchina.co" in args.album_id)
        or (cfg.imgbb.enabled and not cfg.imgbb.cookie)
    )

    with (
        ImgbbClient(workers=cfg.workers, delay=cfg.delay) as client,
        BrowserBridge() if browser_needed else nullcontext_bridge() as browser,
    ):
        if cfg.imgbb.enabled and not cfg.imgbb.cookie and browser:
            cfg.imgbb.cookie, cfg.imgbb.auth_token = browser.get_imgbb_session()

        if args.album_id:
            album = build_album_from_url(
                client, f"https://ibb.co/album/{args.album_id}", browser=browser
            )
            if cfg.imgbb.enabled:
                mirror_albums(client, [album], cfg, args, browser=browser)
            else:
                download_albums(client, [album], cfg, args, browser=browser)
            return

        if is_direct_album_url(args.url):
            albums = [build_album_from_url(client, args.url, browser=browser)]
        else:
            albums = scrape_album_list(client, args.url, cfg.max_pages, browser=browser)
        console.print(f"\n共找到 [bold]{len(albums)}[/bold] 个相册")

        if args.dry_run:
            for i, a in enumerate(albums):
                console.print(f"  [{i + 1}] {a['name']}  ({a['url']})")
            return

        if cfg.imgbb.enabled:
            mirror_albums(client, albums, cfg, args, browser=browser)
        else:
            download_albums(client, albums, cfg, args, browser=browser)


if __name__ == "__main__":
    main()
