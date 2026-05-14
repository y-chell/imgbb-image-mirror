# imgbb-image-mirror

把指定图片站的相册整理、下载，并镜像到自己的 imgbb 账号。

它不是通用爬虫，而是一个面向固定站点的图片相册镜像工具。它会解析相册列表、找到原图地址、保存进度，并把图片上传到 imgbb 新相册。中断后可以继续跑，不会重复上传已经成功的图片。

目前支持：

- imgbb 相册
- xchina 相册/模特页
- taotu 相册页

## Demo

公开示例相册：

- <https://u61945.imgbb.com/albums>

可以先看镜像后的展示效果，再决定是否自己部署。

## 主要功能

- 批量解析相册列表和相册内图片
- 下载原图到本地目录
- 把源站相册镜像到自己的 imgbb 账号
- 断点续传：上传状态保存在 `_mirror_state`
- 跳过已上传图片，避免重复创建相册或重复上传
- 支持并发、请求间隔、最大翻页数配置
- 支持 `config.toml`，也支持命令行参数覆盖
- xchina 这类需要浏览器上下文的网站，可以复用本机 Chrome 登录态

## 为什么这么写

这个项目不是简单的 `requests.get()` 批量下载。实际跑图片站时会遇到这些问题：

- 源站可能有 Cloudflare，直接用 `httpx` 经常拿到 403 或验证页。
- 复制浏览器 cookie 给 Python 不一定有用，因为 TLS 指纹也可能被拦。
- xchina 的 model 页不是稳定相册列表，稳定入口是 `photos/model-xxx.html`。
- xchina 原图通常可以从首张图推导出整套 `0001.jpg ~ 00NN.jpg`，比逐张点开更稳。
- imgbb 上传用的是网页内部接口 `POST https://imgbb.com/json`，不是官方公开 API。

所以项目里保留了两套方式：

- 普通站点：直接用 Python 解析、下载、上传。
- 麻烦站点：接管你已经登录/通过验证的 Chrome，再在浏览器上下文里取图。

## 安装

需要 Python 3.11+。

```bash
pip install -e .
playwright install chromium
```

安装后可用命令：

- `imgbb-image-mirror`
- `imgbb-scraper`（旧命令，保留兼容）

## 配置

复制示例配置：

```bash
cp config.example.toml config.toml
```

示例：

```toml
[scraper]
output = "./downloads"
workers = 4
delay = 0.5
max_pages = 100

[imgbb]
enabled = false
cookie = ""
auth_token = ""
```

字段说明：

- `output`：下载目录，也是镜像状态目录
- `workers`：并发数
- `delay`：请求间隔，单位秒
- `max_pages`：列表页最多翻页数
- `imgbb.enabled`：是否启用镜像上传
- `imgbb.cookie`：imgbb 登录 cookie
- `imgbb.auth_token`：imgbb 页面里的 `auth_token`，不填时会尝试自动获取

`config.toml` 可能包含登录信息，已经写进 `.gitignore`，不要提交到 GitHub。

## 使用

只列出相册，不下载：

```bash
imgbb-image-mirror "https://example.com/albums" --dry-run
```

下载相册图片到本地：

```bash
imgbb-image-mirror "https://example.com/albums" --output ./downloads
```

镜像上传到 imgbb：

```bash
imgbb-image-mirror "https://example.com/albums" --mirror --imgbb-cookie "LID=...; PHPSESSID=..."
```

只处理指定 imgbb 相册：

```bash
imgbb-image-mirror "https://ibb.co/album/xxxx" --album-id xxxx
```

## Chrome 远程调试

如果目标站点需要浏览器登录态，先用远程调试模式启动 Chrome，再运行工具。

Windows 示例：

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data"
```

工具会从 Chrome 的 `DevToolsActivePort` 读取连接信息，并复用当前浏览器会话。

## bridge server 备用方案

如果出现这些情况：

- Python 直连源站图片经常 403
- Python TLS 握手失败，但浏览器或 curl 正常
- 需要从浏览器上下文取图，再转发给 imgbb

可以启动 `bridge_server.py` 做本地中转：

```powershell
$env:IMGBB_COOKIE="LID=...; PHPSESSID=..."
$env:IMGBB_TOKEN="your_auth_token"
python .\bridge_server.py
```

流程是：

1. 浏览器侧拿到图片数据
2. POST 到 `http://127.0.0.1:18888`
3. 本地脚本调用 `curl` 上传到 imgbb

这是备用方案。默认上传路径仍然是 `imgbb_image_mirror/uploader.py`。

## 输出文件

默认输出在 `downloads/`：

- 图片文件：普通下载模式保存的原图
- `_mirror_state/*.json`：镜像上传状态
- `metadata.json`：本地下载相册的元信息
- `imgbb_image_mirror.log`：运行日志

这些都是运行产物，不建议提交。

## 开发

运行测试：

```bash
python -m pytest
```

项目结构：

```text
imgbb_image_mirror/
  __main__.py       # CLI 入口
  downloader.py     # 相册解析、下载、原图地址收集
  parser.py         # 各站点 HTML 解析
  uploader.py       # imgbb 上传客户端
  browser_bridge.py # 连接本机 Chrome 会话
  metadata.py       # 下载/上传状态文件
tests/
  test_xchina_support.py
```

## 说明

- 这个工具只面向已适配的网站，不保证所有图片站都能用。
- 如果网站页面结构变了，需要更新 `parser.py` 里的解析规则。
- imgbb 的网页内部接口可能变化，如果上传失败，先检查 `uploader.py` 里的参数。
