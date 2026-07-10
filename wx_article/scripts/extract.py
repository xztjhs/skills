#!/usr/bin/env python3
"""
提取微信公众号文章内容并保存为 Markdown 文件。
增强版：重试机制、图片 base64 内嵌、视频/GIF 链接保留、可读性优化。

用法:
    python extract.py <微信文章URL> [--out-dir <目录>] [--no-base64]

输出:
    <输出目录>/YYYY-MM-DD_文章标题_公众号名称.md
"""

from typing import Optional, Tuple, Dict
import argparse
import base64
import mimetypes
import os
import re
import sys
import time
from datetime import datetime
from io import BytesIO
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup, Comment
from markdownify import markdownify as md
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ── 全局配置 ───────────────────────────────
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

WX_VERIFY_PATTERNS = [
    "请在微信客户端打开",
    "微信安全支付",
    "访问被拒绝",
    "Access Denied",
    "请在手机微信中查看",
    "需要登录",
    "此内容被投诉且经审核涉嫌侵权",
]

IMAGE_MAX_SIZE = 2 * 1024 * 1024  # 超过 2MB 的图片放弃 base64，改存链接


def build_session() -> requests.Session:
    """构建带重试策略的请求会话。"""
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_html(session: requests.Session, url: str) -> str:
    """获取微信文章 HTML，带重试和验证页检测。"""
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.SSLError as e:
        # 某些环境 SSL 握手失败，尝试不验证证书
        print(f"⚠️ SSL 错误，尝试不验证证书: {e}", file=sys.stderr)
        resp = session.get(url, timeout=30, verify=False)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 403:
            raise RuntimeError(
                f"HTTP 403: 微信可能已封禁该 IP 或需要验证。{e}"
            ) from e
        raise RuntimeError(f"HTTP {resp.status_code}: {e}") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"网络请求失败: {e}") from e

    html = resp.text

    # 检测微信客户端限制页面
    for pattern in WX_VERIFY_PATTERNS:
        if pattern in html:
            raise RuntimeError(
                f"微信验证页拦截：检测到「{pattern}」。"
                f"建议：a) 确认链接是否完整 b) 尝试从已登录微信的浏览器复制链接"
            )

    return html


def download_image(session: requests.Session, url: str) -> Optional[bytes]:
    """下载图片，返回二进制内容；失败返回 None。"""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        resp = session.get(url, timeout=15, stream=True)
        resp.raise_for_status()
        # 限制大小
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > IMAGE_MAX_SIZE:
            return None  # 太大放弃
        buf = BytesIO()
        for chunk in resp.iter_content(chunk_size=8192):
            buf.write(chunk)
            if buf.tell() > IMAGE_MAX_SIZE:
                return None
        return buf.getvalue()
    except Exception:
        return None


def image_to_base64(data: bytes, url: str) -> str:
    """将图片二进制转为 base64 data URI。"""
    mime, _ = mimetypes.guess_type(url)
    if not mime:
        mime = "image/jpeg"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def normalize_content_for_markdownify(content_div: BeautifulSoup):
    """在 markdownify 前预处理 HTML，提升可读性。"""
    # 1. 删除注释
    for comment in content_div.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # 2. 处理 section 标签：微信用 <section> 做卡片，拆成段落
    for section in content_div.find_all("section"):
        section.name = "div"

    # 3. 处理引用块：微信引用通常是 blockquote
    for bq in content_div.find_all("blockquote"):
        # 保留内部文本，blockquote 本身 markdownify 会正确转 > 
        pass

    # 4. 处理 strong/b 合并：去掉无意义的 span wrapper
    for span in content_div.find_all("span"):
        if not span.attrs:
            # 无属性的 span 通常无意义，unwrap
            span.unwrap()
        elif set(span.attrs.keys()) == {"style"} and "color" in span.get("style", ""):
            # 带颜色样式的 span，保留用于 markdownify 可能忽略 color，但保留结构
            pass

    # 5. 处理 p 标签内 br 造成的空段落：连续 br 可能导致 markdownify 产生空行
    for p in content_div.find_all("p"):
        if not p.get_text(strip=True) and not p.find(["img", "video", "iframe"]):
            p.decompose()

    # 6. 表格增强：微信表格常带 style，给表格加 thead/tbody 结构提示
    for table in content_div.find_all("table"):
        # 如果第一行全是 th，确认 thead 结构
        first_row = table.find("tr")
        if first_row:
            cells = first_row.find_all(["td", "th"])
            if cells and all(cell.name == "td" for cell in cells):
                # td 当 th 用的情况，微信常见
                for cell in cells:
                    cell.name = "th"

    # 7. 清理空的 div
    for div in content_div.find_all("div"):
        if not div.get_text(strip=True) and not div.find(["img", "video", "iframe", "table", "blockquote"]):
            div.decompose()


def process_media(session: requests.Session, content_div: BeautifulSoup, use_base64: bool):
    """处理图片、视频、GIF。"""
    stats = {"images_converted": 0, "images_failed": 0, "videos": 0, "gifs": 0}

    # ── 图片处理 ──
    for img in content_div.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src:
            img.decompose()
            continue

        # 清理属性
        for key in list(img.attrs.keys()):
            if key not in ("src", "alt", "title"):
                del img.attrs[key]

        # base64 转换
        if use_base64 and src.startswith("http"):
            data = download_image(session, src)
            if data:
                img["src"] = image_to_base64(data, src)
                stats["images_converted"] += 1
            else:
                stats["images_failed"] += 1
                # 失败保留原链接
                img["src"] = src
        else:
            img["src"] = src

        if not img.get("alt"):
            img["alt"] = "图片"

    # ── 视频处理（iframe / video 标签）─
    for iframe in content_div.find_all("iframe"):
        src = iframe.get("data-src") or iframe.get("src") or ""
        if src:
            placeholder = content_div.new_tag("p")
            placeholder.string = f"[视频] {src}"
            iframe.replace_with(placeholder)
            stats["videos"] += 1
        else:
            iframe.decompose()

    for video in content_div.find_all("video"):
        src = video.get("data-src") or video.get("src") or ""
        if src:
            placeholder = content_div.new_tag("p")
            placeholder.string = f"[视频] {src}"
            video.replace_with(placeholder)
            stats["videos"] += 1
        else:
            video.decompose()

    # ── GIF 处理（通常是 img，但可能带 data-type="gif"）─
    for img in content_div.find_all("img"):
        # 已经处理过 src，检查是否是 gif
        if img.get("src", "").lower().endswith(".gif"):
            stats["gifs"] += 1

    return stats


def extract_article(session: requests.Session, html: str, source_url: str, use_base64: bool) -> Tuple[dict, dict]:
    """从 HTML 中提取文章元数据和正文，返回 (article_dict, stats)。"""
    soup = BeautifulSoup(html, "html.parser")

    # ── 标题 ──
    title_tag = soup.find("h2", {"id": "activity_name"})
    title = title_tag.get_text(strip=True) if title_tag and title_tag.get_text(strip=True) else ""
    if not title:
        og_title = soup.find("meta", {"property": "og:title"})
        if og_title:
            title = og_title.get("content", "").strip()
    if not title:
        title = "未命名文章"

    # ── 封面图 ──
    cover = ""
    og_image = soup.find("meta", {"property": "og:image"})
    if og_image:
        cover = og_image.get("content", "").strip()

    # ── 发布时间 ──
    pub_time = ""
    time_tag = soup.find("em", {"id": "publish_time"})
    if time_tag:
        pub_time = time_tag.get_text(strip=True)
    if not pub_time:
        m = re.search(r'var\s+publish_time\s*=\s*["\']([^"\']+)["\'];', html)
        if m:
            pub_time = m.group(1).strip()
    if not pub_time:
        m = re.search(r'(20[0-9]{2}-[0-9]{1,2}-[0-9]{1,2})', html[:50000])
        if m:
            pub_time = m.group(1)

    # ── 公众号名称 ──
    nickname = ""
    nick_tag = soup.find("a", {"id": "js_name"})
    if nick_tag:
        nickname = nick_tag.get_text(strip=True)
    if not nickname:
        profile = soup.find("span", {"class": "profile_nickname"})
        if profile:
            nickname = profile.get_text(strip=True)
    if not nickname:
        # 尝试从微信文章 script 变量提取
        m = re.search(r'var\s+nickname\s*=\s*["\']([^"\']+)["\'];', html)
        if m:
            nickname = m.group(1).strip()
    if not nickname:
        og_desc = soup.find("meta", {"property": "og:description"})
        if og_desc:
            nickname = og_desc.get("content", "").strip()

    # ── 正文容器 ──
    content_div = soup.find("div", {"id": "js_content"})
    if not content_div:
        raise RuntimeError(
            "未找到文章正文（js_content），可能页面结构已变更或访问受限。"
        )

    # 清理 script/style
    for tag in content_div.find_all(["script", "style"]):
        tag.decompose()

    # 处理媒体
    stats = process_media(session, content_div, use_base64)

    # 可读性预处理
    normalize_content_for_markdownify(content_div)

    # HTML → Markdown
    body_md = md(
        str(content_div),
        heading_style="ATX",
        strip=["script", "style"],
    )

    # 清理 markdownify 产生的过度转义和格式问题
    body_md = body_md.replace("\\*", "*").replace("\\_", "_")
    # 清理连续的换行（超过 2 个换行保留 2 个）
    body_md = re.sub(r"\n{4,}", "\n\n\n", body_md)
    # 清理行首多余空格（影响代码块以外的渲染）
    body_md = re.sub(r"^(?![ ]{4}|```)( )+", "", body_md, flags=re.MULTILINE)

    article = {
        "title": title,
        "pub_time": pub_time,
        "nickname": nickname,
        "cover": cover,
        "body_md": body_md,
        "source_url": source_url,
    }
    return article, stats


def sanitize_filename(name: str) -> str:
    """将标题转为安全的文件名。"""
    name = name.strip().replace(" ", "_")
    # 保留中英文、数字、下划线、连字符，其余替换
    name = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9_\-]", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name or "article"


def save_markdown(article: dict, out_dir: str, stats: dict) -> str:
    """生成并保存 Markdown 文件。"""
    pub_time = article["pub_time"]
    if pub_time:
        try:
            dt = datetime.strptime(pub_time, "%Y-%m-%d")
            date_prefix = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_prefix = datetime.now().strftime("%Y-%m-%d")
    else:
        date_prefix = datetime.now().strftime("%Y-%m-%d")

    safe_title = sanitize_filename(article["title"])
    safe_nickname = sanitize_filename(article["nickname"])
    filename = f"{date_prefix}_{safe_title}_{safe_nickname}.md" if safe_nickname else f"{date_prefix}_{safe_title}.md"
    filepath = os.path.join(out_dir, filename)
    os.makedirs(out_dir, exist_ok=True)

    # 封面图（如有）
    cover_md = f"\n![封面]({article['cover']})\n" if article.get("cover") else ""

    # 构建正文
    lines = [
        "---",
        f"title: {article['title']}",
        f"author: {article['nickname']}",
        f"date: {article['pub_time']}",
        f"source: {article['source_url']}",
        "---",
        "",
        f"# {article['title']}",
        "",
    ]

    if article["nickname"]:
        lines.append(f"> 来源：{article['nickname']}")
        lines.append(">")
    lines.append(f"> 时间：{article['pub_time'] or date_prefix}")
    lines.append(f"> 原文：[链接]({article['source_url']})")
    if article.get("cover"):
        lines.append(f"> 封面：[查看]({article['cover']})")
    lines.append("")
    lines.append(cover_md.strip())
    if cover_md:
        lines.append("")
    lines.append(article["body_md"])
    lines.append("")

    # 转录统计
    lines.append("---")
    lines.append("")
    lines.append("### 转录统计")
    lines.append("")
    lines.append(f"- 图片 base64 转换：{stats['images_converted']} 张")
    lines.append(f"- 图片转换失败：{stats['images_failed']} 张")
    lines.append(f"- 视频链接保留：{stats['videos']} 个")
    lines.append(f"- GIF 保留：{stats['gifs']} 个")
    lines.append("")

    md_content = "\n".join(lines)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(md_content)

    return filepath


def main():
    parser = argparse.ArgumentParser(
        description="提取微信公众号文章为 Markdown（增强版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python extract.py "https://mp.weixin.qq.com/s/xxxxx"
  python extract.py "https://mp.weixin.qq.com/s/xxxxx" --out-dir ./articles --no-base64
        """,
    )
    parser.add_argument("url", help="微信公众号文章链接")
    parser.add_argument(
        "--out-dir",
        default=os.path.expanduser("~/work/wx_articles"),
        help="输出目录（默认: ~/work/wx_articles）",
    )
    parser.add_argument(
        "--no-base64",
        action="store_true",
        help="禁用图片 base64 内嵌，保留原始链接",
    )
    args = parser.parse_args()

    if not args.url.startswith(("http://", "https://")):
        print("错误: URL 必须以 http:// 或 https:// 开头", file=sys.stderr)
        sys.exit(1)

    session = build_session()
    use_base64 = not args.no_base64

    t0 = time.perf_counter()
    print(f"🌐 正在获取: {args.url}")

    try:
        html = fetch_html(session, args.url)
    except RuntimeError as e:
        print(f"❌ 获取失败: {e}", file=sys.stderr)
        sys.exit(2)

    print("📄 正在解析文章...")
    try:
        article, stats = extract_article(session, html, args.url, use_base64)
    except RuntimeError as e:
        print(f"❌ 解析失败: {e}", file=sys.stderr)
        sys.exit(3)

    print(f"   标题: {article['title']}")
    print(f"   公众号: {article['nickname'] or '未知'}")
    print(f"   发布时间: {article['pub_time'] or '未知'}")
    if article.get("cover"):
        print(f"   封面: {article['cover'][:80]}...")
    print(f"   图片 base64 转换: {stats['images_converted']} / 失败: {stats['images_failed']}")
    print(f"   视频保留: {stats['videos']} | GIF: {stats['gifs']}")

    filepath = save_markdown(article, args.out_dir, stats)
    elapsed = time.perf_counter() - t0
    print(f"✅ 已保存: {filepath}  (耗时 {elapsed:.2f}s)")


if __name__ == "__main__":
    main()
