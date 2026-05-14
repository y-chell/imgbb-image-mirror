import argparse
import logging
import os
import queue
import sys
import tempfile
import threading
from contextlib import nullcontext
from urllib.parse import urlparse

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn
from rich.logging import RichHandler

from .browser_bridge import BrowserBridge
from .config import load_config
from .client import ImgbbClient
from .downloader import (
    scrape_album_list, download_album, collect_image_pages, resolve_original_urls,
    is_direct_album_url, build_album_from_url,
)
from .metadata import create_metadata, load_metadata_file, mark_uploaded, save_metadata_file


def _configure_stdio():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


_configure_stdio()

console = Console()


def _nullcontext():
    return nullcontext(None)


def _build_upload_name(image_info: dict, resolved_url: str, index: int) -> str:
    if image_info.get("direct_url"):
        parsed = os.path.basename(urlparse(image_info["direct_url"]).path)
        if parsed:
            return parsed
    if resolved_url:
        parsed = os.path.basename(urlparse(resolved_url).path)
        if parsed:
            return parsed
    return f"{index + 1:04d}.jpg"


def _mirror_state_path(output_dir: str, album: dict) -> str:
    state_dir = os.path.join(output_dir, "_mirror_state")
    source = album.get("source", "site")
    return os.path.join(state_dir, f"{source}_{album['id']}.json")


def _prepare_mirror_metadata(state_path: str, album: dict, source_url: str,
                             image_pages: list[dict], original_urls: list[str]) -> tuple[dict, list[str]]:
    current_names = [_build_upload_name(image_pages[idx], original_urls[idx], idx)
                     for idx in range(len(image_pages))]
    previous = load_metadata_file(state_path) or {}
    previous_images = {
        img.get("index"): img
        for img in previous.get("images", [])
        if isinstance(img, dict)
    }

    metadata = create_metadata(album, source_url)
    metadata["state_version"] = 2
    metadata["storage"] = "mirror_resume"
    metadata["mirror_album_id"] = previous.get("mirror_album_id", "")
    metadata["mirror_album_url"] = previous.get("mirror_album_url", "")
    metadata["mirror_status"] = previous.get("mirror_status", "pending")
    metadata["images"] = []

    for idx, image in enumerate(image_pages):
        old = previous_images.get(idx, {})
        entry = {
            "index": idx,
            "original_url": original_urls[idx],
            "thumb_url": image.get("thumb", ""),
            "page_url": image.get("page_url", ""),
            "filename": current_names[idx],
            "alt": image.get("alt", ""),
            "size": old.get("size", 0),
            "status": "pending",
            "uploaded_url": "",
            "viewer_url": "",
            "uploaded_thumb": "",
        }
        if old.get("status") == "uploaded":
            entry["status"] = "uploaded"
            entry["uploaded_url"] = old.get("uploaded_url", "")
            entry["viewer_url"] = old.get("viewer_url", "")
            entry["uploaded_thumb"] = old.get("uploaded_thumb", "")
        metadata["images"].append(entry)

    save_metadata_file(state_path, metadata)
    return metadata, current_names


def _get_cached_original_urls(state_path: str, total: int) -> list[str] | None:
    previous = load_metadata_file(state_path) or {}
    if len(previous.get("images", [])) != total:
        return None
    ordered = sorted(
        (img for img in previous["images"] if isinstance(img, dict) and "index" in img),
        key=lambda img: img["index"],
    )
    if len(ordered) != total:
        return None
    original_urls = [img.get("original_url", "") for img in ordered]
    if any(not url for url in original_urls):
        return None
    return original_urls


def _count_uploaded(metadata: dict) -> int:
    return sum(1 for image in metadata.get("images", []) if image.get("status") == "uploaded")


def _save_mirror_album(metadata: dict, state_path: str, album_info: dict, completed: bool = False):
    metadata["mirror_album_id"] = album_info.get("id", metadata.get("mirror_album_id", ""))
    metadata["mirror_album_url"] = album_info.get("url", metadata.get("mirror_album_url", ""))
    metadata["mirror_status"] = "completed" if completed else "uploading"
    save_metadata_file(state_path, metadata)


def _download_to_temp(browser, album: dict, image_info: dict, upload_name: str) -> str:
    suffix = os.path.splitext(upload_name)[1] or ".jpg"
    tmp_path = ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
    try:
        if image_info.get("direct_url"):
            browser.download_xchina_direct(
                image_info["direct_url"],
                image_info.get("referer_url", album["url"]),
                tmp_path,
            )
        else:
            browser.download_xchina_image(image_info["page_url"], tmp_path)
        return tmp_path
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _upload_worker(uploader, album_id: str, upload_queue, result_queue):
    while True:
        task = upload_queue.get()
        try:
            if task is None:
                return

            result = None
            error = ""
            try:
                if task["type"] == "file":
                    result = uploader.upload_file(
                        task["file_path"],
                        album_id,
                        name=task["title"],
                        upload_filename=task["upload_name"],
                    )
                else:
                    result = uploader.upload_image(task["url"], album_id, name=task["title"])
                if not result:
                    error = "upload returned empty result"
            except Exception as exc:
                error = str(exc)
            finally:
                if task.get("file_path") and os.path.exists(task["file_path"]):
                    os.unlink(task["file_path"])

            result_queue.put({
                "index": task["index"],
                "result": result,
                "error": error,
            })
        finally:
            upload_queue.task_done()


def setup_logging(quiet: bool = False, log_file: str = "imgbb_image_mirror.log"):
    handlers = []
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
    for name in ("httpcore", "httpx", "hpack", "urllib3"):
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
    parser.add_argument("--mirror", action="store_true", help="镜像模式: 爬取后上传到自己的 imgbb 账号")
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
    logger = logging.getLogger("imgbb_image_mirror")
    os.makedirs(cfg.output, exist_ok=True)

    browser_needed = (
        (args.url and "xchina.co" in args.url)
        or (args.album_id and "xchina.co" in args.album_id)
        or (cfg.imgbb.enabled and not cfg.imgbb.cookie)
    )

    with ImgbbClient(workers=cfg.workers, delay=cfg.delay) as client, \
            BrowserBridge() if browser_needed else _nullcontext() as browser:
        if cfg.imgbb.enabled and not cfg.imgbb.cookie and browser:
            cfg.imgbb.cookie, cfg.imgbb.auth_token = browser.get_imgbb_session()

        if args.album_id:
            album = build_album_from_url(client, f"https://ibb.co/album/{args.album_id}", browser=browser)
            if cfg.imgbb.enabled:
                _mirror_with_progress(client, [album], cfg, args, browser=browser)
            else:
                _download_with_progress(client, [album], cfg, args, browser=browser)
            return

        if is_direct_album_url(args.url):
            albums = [build_album_from_url(client, args.url, browser=browser)]
        else:
            albums = scrape_album_list(client, args.url, cfg.max_pages, browser=browser)
        console.print(f"\n共找到 [bold]{len(albums)}[/bold] 个相册")

        if args.dry_run:
            for i, a in enumerate(albums):
                console.print(f"  [{i+1}] {a['name']}  ({a['url']})")
            return

        if cfg.imgbb.enabled:
            _mirror_with_progress(client, albums, cfg, args, browser=browser)
        else:
            _download_with_progress(client, albums, cfg, args, browser=browser)


def _download_with_progress(client, albums, cfg, args, browser=None):
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        disable=args.quiet,
    ) as progress:
        album_task = progress.add_task("相册总进度", total=len(albums))
        img_task = progress.add_task("当前相册", total=0, visible=False)

        for i, album in enumerate(albums):
            progress.update(album_task, description=f"[{i+1}/{len(albums)}] {album['name'][:40]}")
            progress.update(img_task, completed=0, total=0, visible=False)

            def on_progress(event, **kwargs):
                if event == "album_start":
                    progress.update(img_task, total=kwargs["total"], completed=0, visible=True,
                                    description=f"下载: {kwargs['name'][:30]}")
                elif event == "image_done":
                    progress.update(img_task, completed=kwargs["success"] + kwargs["failed"])
                elif event == "album_done":
                    progress.update(img_task, visible=False)

            result = download_album(client, album, cfg.output,
                                    source_url=args.url, workers=cfg.workers,
                                    progress_cb=on_progress, browser=browser)
            results.append(result)
            progress.advance(album_task)

    total_imgs = sum(r["total"] for r in results)
    total_ok = sum(r["success"] for r in results)
    total_fail = sum(r["failed"] for r in results)
    console.print(f"\n[bold green]完成[/bold green] 相册: {len(results)}, "
                  f"图片: {total_imgs}, 成功: {total_ok}, 失败: {total_fail}")


def _mirror_with_progress(client, albums, cfg, args, browser=None):
    from .uploader import ImgbbUploader

    logger = logging.getLogger("imgbb_image_mirror.mirror")

    if not cfg.imgbb.cookie:
        console.print("[red]镜像模式需要 imgbb cookie (--imgbb-cookie 或配置文件)[/red]")
        return

    results = []

    with ImgbbUploader(cookie=cfg.imgbb.cookie, auth_token=cfg.imgbb.auth_token,
                       delay=cfg.delay) as uploader:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=console,
            disable=args.quiet,
        ) as progress:
            album_task = progress.add_task("镜像总进度", total=len(albums))
            img_task = progress.add_task("当前相册", total=0, visible=False)

            for i, album in enumerate(albums):
                album_name = album["name"]
                progress.update(album_task,
                                description=f"[{i+1}/{len(albums)}] {album_name[:40]}")

                # 收集原图链接
                image_pages = collect_image_pages(client, album["url"], browser=browser)
                total = len(image_pages)
                if not image_pages:
                    progress.advance(album_task)
                    results.append({"name": album_name, "total": 0,
                                    "success": 0, "failed": 0})
                    continue

                progress.update(img_task, total=total + 1, completed=0, visible=True,
                                description=f"获取原图: {album_name[:30]}")

                state_path = _mirror_state_path(cfg.output, album)
                original_urls = _get_cached_original_urls(state_path, total)
                if original_urls:
                    logger.info(f"复用缓存原图链接: {album_name} ({total} 张)")
                else:
                    original_urls = resolve_original_urls(client, image_pages,
                                                          workers=cfg.workers, browser=browser)
                metadata, upload_names = _prepare_mirror_metadata(
                    state_path, album, args.url, image_pages, original_urls
                )
                uploaded_before = _count_uploaded(metadata)

                # 在用户 imgbb 创建相册
                try:
                    if metadata.get("mirror_album_id"):
                        new_album = {
                            "id": metadata["mirror_album_id"],
                            "url": metadata.get("mirror_album_url", ""),
                            "name": album_name,
                        }
                        new_album_id = new_album["id"]
                        logger.info(f"继续上传: {album_name} -> {new_album.get('url', '')}")
                    else:
                        new_album = uploader.create_album(album_name)
                        new_album_id = new_album["id"]
                        _save_mirror_album(metadata, state_path, new_album)
                        logger.info(f"创建相册: {album_name} -> {new_album['url']}")
                except Exception as e:
                    logger.error(f"创建相册失败 ({album_name}): {e}")
                    progress.advance(album_task)
                    results.append({"name": album_name, "total": total,
                                    "success": 0, "failed": total})
                    continue

                progress.update(
                    img_task,
                    total=total,
                    completed=uploaded_before,
                    description=f"上传: {album_name[:30]}",
                )

                success = uploaded_before
                failed = 0

                if uploaded_before == total:
                    _save_mirror_album(metadata, state_path, new_album, completed=True)
                else:
                    upload_queue = queue.Queue(maxsize=max(1, min(cfg.workers, 3)))
                    result_queue = queue.Queue()
                    worker = threading.Thread(
                        target=_upload_worker,
                        args=(uploader, new_album_id, upload_queue, result_queue),
                        daemon=True,
                    )
                    worker.start()

                    def _collect_results(block: bool = False):
                        nonlocal success, failed
                        while True:
                            try:
                                item = result_queue.get(timeout=0.1 if block else 0)
                            except queue.Empty:
                                break
                            if item["result"]:
                                success += 1
                                mark_uploaded(metadata, item["index"], item["result"])
                            else:
                                failed += 1
                                mark_uploaded(metadata, item["index"], None)
                                logger.warning(f"上传失败: {album_name} #{item['index'] + 1}: {item['error']}")
                            save_metadata_file(state_path, metadata)
                            progress.update(img_task, completed=success + failed)

                    pending_indices = [
                        idx for idx in range(total)
                        if metadata["images"][idx].get("status") != "uploaded"
                    ]

                    for idx in pending_indices:
                        upload_name = upload_names[idx]
                        title = os.path.splitext(upload_name)[0]
                        try:
                            if image_pages[idx].get("source") == "xchina":
                                if not browser:
                                    raise RuntimeError("xchina 上传缺少浏览器会话")
                                task = {
                                    "type": "file",
                                    "index": idx,
                                    "title": title,
                                    "upload_name": upload_name,
                                    "file_path": _download_to_temp(browser, album, image_pages[idx], upload_name),
                                }
                            else:
                                task = {
                                    "type": "url",
                                    "index": idx,
                                    "title": title,
                                    "upload_name": upload_name,
                                    "url": original_urls[idx],
                                }
                            upload_queue.put(task)
                            _collect_results()
                        except Exception as e:
                            failed += 1
                            mark_uploaded(metadata, idx, None)
                            save_metadata_file(state_path, metadata)
                            logger.warning(f"下载失败: {album_name} #{idx + 1}: {e}")
                            progress.update(img_task, completed=success + failed)

                    upload_queue.put(None)
                    upload_queue.join()
                    worker.join()
                    _collect_results(block=True)

                    if success == total:
                        _save_mirror_album(metadata, state_path, new_album, completed=True)
                    else:
                        _save_mirror_album(metadata, state_path, new_album, completed=False)

                progress.update(img_task, visible=False)
                progress.advance(album_task)

                logger.info(f"  镜像完成: {album_name} "
                            f"({success}/{total} 成功, {failed} 失败)")
                results.append({
                    "name": album_name,
                    "total": total,
                    "success": success,
                    "failed": failed,
                    "album_url": new_album.get("url", ""),
                })

    total_imgs = sum(r["total"] for r in results)
    total_ok = sum(r["success"] for r in results)
    total_fail = sum(r["failed"] for r in results)
    console.print(f"\n[bold green]镜像完成[/bold green] 相册: {len(results)}, "
                  f"图片: {total_imgs}, 成功: {total_ok}, 失败: {total_fail}")
    for r in results:
        if r.get("album_url"):
            console.print(f"  {r['name']}: {r['album_url']}")


if __name__ == "__main__":
    main()
