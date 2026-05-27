#!/usr/bin/env python3
"""
提取微信公众号文章内容并保存为 Markdown 文件。

用法:
    python extract.py <微信文章URL> [--out-dir <输出目录>]

输出:
    <输出目录>/YYYY-MM-DD_文章名称.md
"""

import argparse
import os
import re
import sys
from datetime import datetime
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md


def fetch_html(url: str) -> str:
    """获取微信文章 HTML，模拟浏览器绕过反爬。"""
    headers = {
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
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_article(html: str, source_url: str) -> dict:
    """从 HTML 中提取文章元数据和正文。"""
    soup = BeautifulSoup(html, "html.parser")

    # 标题：优先 activity_name，fallback 到 og:title
    title_tag = soup.find("h2", {"id": "activity_name"})
    title = title_tag.get_text(strip=True) if title_tag and title_tag.get_text(strip=True) else ""
    if not title:
        og_title = soup.find("meta", {"property": "og:title"})
        if og_title:
            title = og_title.get("content", "").strip()
    if not title:
        title = "未命名文章"

    # 发布时间
    time_tag = soup.find("em", {"id": "publish_time"})
    pub_time = time_tag.get_text(strip=True) if time_tag else ""
    if not pub_time:
        # 尝试从 script 变量提取
        m = re.search(r'var\s+publish_time\s*=\s*["\']([^"\']+)["\'];', html)
        if m:
            pub_time = m.group(1).strip()
    if not pub_time:
        # 尝试从正文里找 20xx-xx-xx 格式日期
        m = re.search(r'(20[0-9]{2}-[0-9]{1,2}-[0-9]{1,2})', html[:50000])
        if m:
            pub_time = m.group(1)

    # 公众号名称
    nick_tag = soup.find("a", {"id": "js_name"})
    nickname = nick_tag.get_text(strip=True) if nick_tag else ""
    if not nickname:
        profile = soup.find("span", {"class": "profile_nickname"})
        nickname = profile.get_text(strip=True) if profile else ""
    if not nickname:
        # 从 og 或 页面其他位置尝试
        og_desc = soup.find("meta", {"property": "og:description"})
        if og_desc:
            nickname = og_desc.get("content", "").strip()

    # 正文容器
    content_div = soup.find("div", {"id": "js_content"})
    if not content_div:
        raise RuntimeError("未找到文章正文（js_content），可能页面结构已变更或访问受限。")

    # 清理脚本和样式标签
    for tag in content_div.find_all(["script", "style"]):
        tag.decompose()

    # 处理图片：保留 data-src 或 src
    for img in content_div.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        img["src"] = src
        img.attrs.pop("data-src", None)
        img.attrs.pop("data-ratio", None)
        img.attrs.pop("data-w", None)
        img.attrs.pop("data-type", None)
        if not img.get("alt"):
            img["alt"] = ""

    # 用 markdownify 转换正文 HTML → Markdown
    body_md = md(str(content_div), heading_style="ATX", strip=["script", "style"])

    # 清理 markdownify 产生的过度转义
    body_md = body_md.replace("\\*", "*").replace("\\_", "_")

    return {
        "title": title,
        "pub_time": pub_time,
        "nickname": nickname,
        "body_md": body_md,
        "source_url": source_url,
    }


def sanitize_filename(name: str) -> str:
    """将标题转为安全的文件名。"""
    name = name.strip().replace(" ", "_")
    name = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9_\-]", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name or "article"


def save_markdown(article: dict, out_dir: str) -> str:
    """生成并保存 Markdown 文件。"""
    pub_time = article["pub_time"]
    if pub_time:
        try:
            # 微信常见格式: 2024-05-27
            dt = datetime.strptime(pub_time, "%Y-%m-%d")
            date_prefix = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_prefix = datetime.now().strftime("%Y-%m-%d")
    else:
        date_prefix = datetime.now().strftime("%Y-%m-%d")

    safe_title = sanitize_filename(article["title"])
    safe_nickname = sanitize_filename(article["nickname"])
    if safe_nickname:
        filename = f"{date_prefix}_{safe_nickname}_{safe_title}.md"
    else:
        filename = f"{date_prefix}_{safe_title}.md"
    filepath = os.path.join(out_dir, filename)

    lines = [
        f"---",
        f"title: {article['title']}",
        f"author: {article['nickname']}",
        f"date: {article['pub_time']}",
        f"source: {article['source_url']}",
        f"---",
        f"",
        f"# {article['title']}",
        f"",
    ]
    if article["nickname"]:
        lines.append(f"> 来源：{article['nickname']}")
        lines.append(f">")
    lines.append(f"> 时间：{article['pub_time'] or date_prefix}")
    lines.append(f"> 原文：[链接]({article['source_url']})")
    lines.append(f"")
    lines.append(article["body_md"])
    lines.append(f"")

    md_content = "\n".join(lines)

    os.makedirs(out_dir, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(md_content)

    return filepath


def main():
    parser = argparse.ArgumentParser(description="提取微信公众号文章为 Markdown")
    parser.add_argument("url", help="微信公众号文章链接")
    parser.add_argument(
        "--out-dir",
        default="/root/work/article",
        help="输出目录（默认: /root/work/article）",
    )
    args = parser.parse_args()

    if not args.url.startswith(("http://", "https://")):
        print("错误: URL 必须以 http:// 或 https:// 开头", file=sys.stderr)
        sys.exit(1)

    print(f"正在获取: {args.url}")
    html = fetch_html(args.url)
    print("正在解析文章...")
    article = extract_article(html, args.url)
    print(f"标题: {article['title']}")
    print(f"公众号: {article['nickname']}")
    print(f"发布时间: {article['pub_time']}")

    filepath = save_markdown(article, args.out_dir)
    print(f"已保存: {filepath}")


if __name__ == "__main__":
    main()
