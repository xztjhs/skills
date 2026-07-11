#!/usr/bin/env python3
"""
wx_article — 微信公众号文章提取器（OpenClaw Skill 版本）

将微信公众号文章 URL 对应的正文提取为 Markdown 文件保存。
支持：重试机制、图片 base64 内嵌、视频/GIF 链接保留、可读性优化。

用法:
    python extract.py <URL> [--out-dir <目录>] [--no-base64] [--verbose]
    python -m wx_article.scripts.extract <URL> [选项]

环境变量:
    WX_ARTICLE_OUT_DIR   默认输出目录（覆盖 --out-dir 默认值）
    WX_ARTICLE_TIMEOUT   请求超时秒数（默认 30）
    WX_ARTICLE_RETRIES   重试次数（默认 3）
"""

from __future__ import annotations

import argparse
import base64
import logging
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup, Comment
from markdownify import markdownify as md
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ═══════════════════════════════════════════════
# 配置与常量
# ═══════════════════════════════════════════════

DEFAULT_TIMEOUT = int(os.getenv("WX_ARTICLE_TIMEOUT", "30"))
DEFAULT_RETRIES = int(os.getenv("WX_ARTICLE_RETRIES", "3"))
DEFAULT_OUT_DIR = os.getenv("WX_ARTICLE_OUT_DIR", str(Path.home() / "work" / "wx_articles"))

IMAGE_MAX_SIZE = 2 * 1024 * 1024  # 超过 2MB 放弃 base64

WX_VERIFY_PATTERNS: list[str] = [
    "请在微信客户端打开",
    "微信安全支付",
    "访问被拒绝",
    "Access Denied",
    "请在手机微信中查看",
    "需要登录",
    "此内容被投诉且经审核涉嫌侵权",
]

DEFAULT_HEADERS: dict[str, str] = {
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

logger = logging.getLogger("wx_article")


# ═══════════════════════════════════════════════
# 异常类
# ═══════════════════════════════════════════════

class WxArticleError(Exception):
    """提取器基础异常。"""
    pass


class NetworkError(WxArticleError):
    """网络请求失败。"""
    pass


class VerifyPageError(WxArticleError):
    """遇到微信验证拦截页。"""
    pass


class ParseError(WxArticleError):
    """HTML 解析失败。"""
    pass


class ContentNotFoundError(WxArticleError):
    """未找到文章正文。"""
    pass


# ═══════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════

@dataclass
class Article:
    """文章元数据与内容。"""
    title: str = "未命名文章"
    pub_time: str = ""
    nickname: str = ""
    cover: str = ""
    body_md: str = ""
    source_url: str = ""


@dataclass
class Stats:
    """转录统计。"""
    images_converted: int = 0
    images_failed: int = 0
    videos: int = 0
    gifs: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "图片 base64 转换": self.images_converted,
            "图片转换失败": self.images_failed,
            "视频链接保留": self.videos,
            "GIF 保留": self.gifs,
        }


@dataclass
class Config:
    """运行配置。"""
    out_dir: Path = field(default_factory=lambda: Path(DEFAULT_OUT_DIR))
    use_base64: bool = True
    verbose: bool = False
    timeout: int = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    image_max_size: int = IMAGE_MAX_SIZE

    @classmethod
    def from_env(cls) -> Config:
        """从环境变量构建配置。"""
        return cls(
            out_dir=Path(os.getenv("WX_ARTICLE_OUT_DIR", DEFAULT_OUT_DIR)),
            timeout=int(os.getenv("WX_ARTICLE_TIMEOUT", str(DEFAULT_TIMEOUT))),
            retries=int(os.getenv("WX_ARTICLE_RETRIES", str(DEFAULT_RETRIES))),
        )


# ═══════════════════════════════════════════════
# 网络层
# ═══════════════════════════════════════════════

class HttpClient:
    """带重试和日志的 HTTP 客户端。"""

    def __init__(self, config: Config):
        self.config = config
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        retry = Retry(
            total=self.config.retries,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=10,
            pool_maxsize=10,
        )
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def fetch(self, url: str) -> str:
        """获取页面 HTML，自动处理 SSL 降级。"""
        logger.info("正在获取: %s", url)
        try:
            resp = self.session.get(url, timeout=self.config.timeout)
            resp.raise_for_status()
        except requests.exceptions.SSLError as e:
            logger.warning("SSL 错误，尝试不验证证书: %s", e)
            resp = self.session.get(url, timeout=self.config.timeout, verify=False)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if hasattr(e.response, "status_code") and e.response.status_code == 403:
                raise NetworkError(f"HTTP 403: 微信可能已封禁该 IP 或需要验证。{e}") from e
            raise NetworkError(f"HTTP 错误: {e}") from e
        except requests.exceptions.RequestException as e:
            raise NetworkError(f"网络请求失败: {e}") from e

        html = resp.text
        self._check_verify_page(html)
        return html

    def download_image(self, url: str) -> Optional[bytes]:
        """下载图片，返回二进制内容；失败或超限返回 None。"""
        if not url or not url.startswith(("http://", "https://")):
            return None

        try:
            resp = self.session.get(url, timeout=15, stream=True)
            resp.raise_for_status()

            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > self.config.image_max_size:
                logger.debug("图片 %s 超过大小限制，放弃", url[:60])
                return None

            buf = BytesIO()
            for chunk in resp.iter_content(chunk_size=8192):
                buf.write(chunk)
                if buf.tell() > self.config.image_max_size:
                    logger.debug("图片 %s 下载中超限，放弃", url[:60])
                    return None
            return buf.getvalue()
        except Exception as e:
            logger.debug("下载图片失败 %s: %s", url[:60], e)
            return None

    @staticmethod
    def _check_verify_page(html: str) -> None:
        """检测微信验证拦截页。"""
        for pattern in WX_VERIFY_PATTERNS:
            if pattern in html:
                raise VerifyPageError(
                    f"微信验证页拦截：检测到「{pattern}」。"
                    "建议：a) 确认链接是否完整 "
                    "b) 尝试从已登录微信的浏览器复制链接"
                )


# ═══════════════════════════════════════════════
# 解析与处理层
# ═══════════════════════════════════════════════

class HtmlParser:
    """HTML 解析与 Markdown 转换器。"""

    def __init__(self, client: HttpClient, config: Config):
        self.client = client
        self.config = config

    def parse(self, html: str, source_url: str) -> tuple[Article, Stats]:
        """解析 HTML，返回 (Article, Stats)。"""
        soup = BeautifulSoup(html, "html.parser")

        article = Article(
            title=self._extract_title(soup, html),
            pub_time=self._extract_pub_time(soup, html),
            nickname=self._extract_nickname(soup, html),
            cover=self._extract_cover(soup),
            source_url=source_url,
        )

        content_div = soup.find("div", {"id": "js_content"})
        if not content_div:
            raise ContentNotFoundError(
                "未找到文章正文（js_content），可能页面结构已变更或访问受限。"
            )

        # 清理 script/style
        for tag in content_div.find_all(["script", "style"]):
            tag.decompose()

        # 处理媒体 → 统计
        stats = self._process_media(content_div)

        # 可读性预处理
        self._normalize_content(content_div)

        # HTML → Markdown
        body_md = md(
            str(content_div),
            heading_style="ATX",
            strip=["script", "style"],
        )

        article.body_md = self._clean_markdown(body_md)
        return article, stats

    # ── 元数据提取 ───────────────────────────

    def _extract_title(self, soup: BeautifulSoup, html: str) -> str:
        title_tag = soup.find("h2", {"id": "activity_name"})
        title = title_tag.get_text(strip=True) if title_tag else ""
        if not title:
            og = soup.find("meta", {"property": "og:title"})
            title = og.get("content", "").strip() if og else ""
        return title or "未命名文章"

    def _extract_pub_time(self, soup: BeautifulSoup, html: str) -> str:
        time_tag = soup.find("em", {"id": "publish_time"})
        pub_time = time_tag.get_text(strip=True) if time_tag else ""
        if not pub_time:
            m = re.search(r'var\s+publish_time\s*=\s*["\']([^"\']+)["\'];', html)
            pub_time = m.group(1).strip() if m else ""
        if not pub_time:
            m = re.search(r'(20[0-9]{2}-[0-9]{1,2}-[0-9]{1,2})', html[:50000])
            pub_time = m.group(1) if m else ""
        return pub_time

    def _extract_nickname(self, soup: BeautifulSoup, html: str) -> str:
        nick_tag = soup.find("a", {"id": "js_name"})
        nickname = nick_tag.get_text(strip=True) if nick_tag else ""
        if not nickname:
            profile = soup.find("span", {"class": "profile_nickname"})
            nickname = profile.get_text(strip=True) if profile else ""
        if not nickname:
            m = re.search(r'var\s+nickname\s*=\s*["\']([^"\']+)["\'];', html)
            nickname = m.group(1).strip() if m else ""
        if not nickname:
            og = soup.find("meta", {"property": "og:description"})
            nickname = og.get("content", "").strip() if og else ""
        return nickname

    def _extract_cover(self, soup: BeautifulSoup) -> str:
        og = soup.find("meta", {"property": "og:image"})
        return og.get("content", "").strip() if og else ""

    # ── 媒体处理 ─────────────────────────────

    def _process_media(self, content_div: BeautifulSoup) -> Stats:
        stats = Stats()

        # 图片
        for img in content_div.find_all("img"):
            src = img.get("data-src") or img.get("src") or ""
            if not src:
                img.decompose()
                continue

            # 清理多余属性
            for key in list(img.attrs.keys()):
                if key not in ("src", "alt", "title"):
                    del img.attrs[key]

            # base64 转换
            if self.config.use_base64 and src.startswith("http"):
                data = self.client.download_image(src)
                if data:
                    img["src"] = self._bytes_to_base64(data, src)
                    stats.images_converted += 1
                    logger.debug("图片转 base64 成功: %s", src[:60])
                else:
                    stats.images_failed += 1
                    img["src"] = src
                    logger.debug("图片转 base64 失败，保留链接: %s", src[:60])
            else:
                img["src"] = src

            if not img.get("alt"):
                img["alt"] = "图片"

            if img.get("src", "").lower().endswith(".gif"):
                stats.gifs += 1

        # 视频（iframe / video）
        for tag_name in ("iframe", "video"):
            for tag in content_div.find_all(tag_name):
                src = tag.get("data-src") or tag.get("src") or ""
                if src:
                    placeholder = content_div.new_tag("p")
                    placeholder.string = f"[视频] {src}"
                    tag.replace_with(placeholder)
                    stats.videos += 1
                else:
                    tag.decompose()

        return stats

    @staticmethod
    def _bytes_to_base64(data: bytes, url: str) -> str:
        mime, _ = mimetypes.guess_type(url)
        mime = mime or "image/jpeg"
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"

    # ── 可读性预处理 ─────────────────────────

    @staticmethod
    def _normalize_content(content_div: BeautifulSoup) -> None:
        # 删除注释
        for comment in content_div.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        # section → div
        for section in content_div.find_all("section"):
            section.name = "div"

        # 无意义 span unwrap
        for span in content_div.find_all("span"):
            if not span.attrs:
                span.unwrap()

        # 空段落清理
        for p in content_div.find_all("p"):
            if not p.get_text(strip=True) and not p.find(["img", "iframe", "table", "blockquote"]):
                p.decompose()

        # 表格首行 td → th
        for table in content_div.find_all("table"):
            first_row = table.find("tr")
            if first_row:
                cells = first_row.find_all("td")
                for cell in cells:
                    cell.name = "th"

        # 空 div 清理
        for div in list(content_div.find_all("div")):
            if not div.get_text(strip=True) and not div.find(["img", "iframe", "table", "blockquote"]):
                div.decompose()

    # ── Markdown 后处理 ──────────────────────

    @staticmethod
    def _clean_markdown(md_text: str) -> str:
        md_text = md_text.replace("\\*", "*").replace("\\_", "_")
        md_text = re.sub(r"\n{4,}", "\n\n\n", md_text)
        md_text = re.sub(r"^(?![ ]{4}|```)( )+", "", md_text, flags=re.MULTILINE)
        return md_text


# ═══════════════════════════════════════════════
# 输出层
# ═══════════════════════════════════════════════

class MarkdownWriter:
    """Markdown 文件写入器。"""

    def __init__(self, config: Config):
        self.config = config

    def write(self, article: Article, stats: Stats) -> Path:
        """生成并保存 Markdown 文件，返回保存路径。"""
        pub_time = article.pub_time
        if pub_time:
            try:
                dt = datetime.strptime(pub_time, "%Y-%m-%d")
                date_prefix = dt.strftime("%Y-%m-%d")
            except ValueError:
                date_prefix = datetime.now().strftime("%Y-%m-%d")
        else:
            date_prefix = datetime.now().strftime("%Y-%m-%d")

        safe_title = self._sanitize(article.title)
        safe_nickname = self._sanitize(article.nickname)

        filename = (
            f"{date_prefix}_{safe_title}_{safe_nickname}.md"
            if safe_nickname
            else f"{date_prefix}_{safe_title}.md"
        )

        out_path = self.config.out_dir / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)

        lines = self._build_lines(article, stats, date_prefix)
        out_path.write_text("\n".join(lines), encoding="utf-8")

        logger.info("已保存: %s", out_path)
        return out_path

    def _build_lines(
        self, article: Article, stats: Stats, fallback_date: str
    ) -> list[str]:
        lines: list[str] = [
            "---",
            f"title: {article.title}",
            f"author: {article.nickname}",
            f"date: {article.pub_time or fallback_date}",
            f"source: {article.source_url}",
            "---",
            "",
            f"# {article.title}",
            "",
        ]

        if article.nickname:
            lines.append(f"> 来源：{article.nickname}")
            lines.append(">")

        lines.append(f"> 时间：{article.pub_time or fallback_date}")
        lines.append(f"> 原文：[链接]({article.source_url})")

        if article.cover:
            lines.append(f"> 封面：[查看]({article.cover})")
            lines.append("")
            lines.append(f"![封面]({article.cover})")

        lines.append("")
        lines.append(article.body_md)
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("### 转录统计")
        lines.append("")

        for key, val in stats.as_dict().items():
            lines.append(f"- {key}：{val}")

        lines.append("")
        return lines

    @staticmethod
    def _sanitize(name: str) -> str:
        name = name.strip().replace(" ", "_")
        name = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9_\-]", "_", name)
        name = re.sub(r"_+", "_", name).strip("_")
        return name or "article"


# ═══════════════════════════════════════════════
# 主控层
# ═══════════════════════════════════════════════

class WxArticleExtractor:
    """微信文章提取器主控类。"""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config.from_env()
        self.client = HttpClient(self.config)
        self.parser = HtmlParser(self.client, self.config)
        self.writer = MarkdownWriter(self.config)

    def extract(self, url: str) -> Path:
        """执行完整提取流程，返回输出文件路径。"""
        logger.info("开始提取: %s", url)
        t0 = time.perf_counter()

        html = self.client.fetch(url)
        article, stats = self.parser.parse(html, url)
        out_path = self.writer.write(article, stats)

        elapsed = time.perf_counter() - t0
        logger.info(
            "完成: title=%s nickname=%s images=%d/%d videos=%d time=%.2fs",
            article.title,
            article.nickname or "未知",
            stats.images_converted,
            stats.images_failed,
            stats.videos,
            elapsed,
        )
        return out_path

    def extract_sync(self, url: str) -> dict:
        """同步提取，返回结构化结果（供 Agent 调用）。"""
        html = self.client.fetch(url)
        article, stats = self.parser.parse(html, url)
        out_path = self.writer.write(article, stats)
        return {
            "success": True,
            "filepath": str(out_path),
            "title": article.title,
            "nickname": article.nickname,
            "pub_time": article.pub_time,
            "stats": stats.as_dict(),
        }


# ═══════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════

def setup_logging(verbose: bool = False) -> None:
    """配置日志输出。"""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="wx_article",
        description="提取微信公众号文章为 Markdown（OpenClaw Skill 版本）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
环境变量:
  WX_ARTICLE_OUT_DIR    默认输出目录（默认: ~/work/wx_articles）
  WX_ARTICLE_TIMEOUT    请求超时秒数（默认: 30）
  WX_ARTICLE_RETRIES    重试次数（默认: 3）

示例:
  python extract.py "https://mp.weixin.qq.com/s/xxxxx"
  python extract.py "https://mp.weixin.qq.com/s/xxxxx" --out-dir ./articles --no-base64
  WX_ARTICLE_OUT_DIR=/tmp/articles python extract.py "https://mp.weixin.qq.com/s/xxxxx"
        """,
    )
    parser.add_argument("url", help="微信公众号文章链接（mp.weixin.qq.com）")
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help=f"输出目录（默认: {DEFAULT_OUT_DIR}）",
    )
    parser.add_argument(
        "--no-base64",
        action="store_true",
        help="禁用图片 base64 内嵌，保留原始链接",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="输出详细日志",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"请求超时秒数（默认: {DEFAULT_TIMEOUT}）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"重试次数（默认: {DEFAULT_RETRIES}）",
    )

    args = parser.parse_args()

    if not args.url.startswith(("http://", "https://")):
        print("错误: URL 必须以 http:// 或 https:// 开头", file=sys.stderr)
        return 1

    setup_logging(args.verbose)

    config = Config(
        out_dir=Path(args.out_dir),
        use_base64=not args.no_base64,
        verbose=args.verbose,
        timeout=args.timeout,
        retries=args.retries,
    )

    extractor = WxArticleExtractor(config)

    try:
        out_path = extractor.extract(args.url)
        print(f"✅ 已保存: {out_path}")
        return 0
    except VerifyPageError as e:
        logger.error("验证页拦截: %s", e)
        print(f"❌ {e}", file=sys.stderr)
        return 2
    except NetworkError as e:
        logger.error("网络错误: %s", e)
        print(f"❌ {e}", file=sys.stderr)
        return 3
    except (ParseError, ContentNotFoundError) as e:
        logger.error("解析错误: %s", e)
        print(f"❌ {e}", file=sys.stderr)
        return 4
    except Exception as e:
        logger.exception("未知错误: %s", e)
        print(f"❌ 未知错误: {e}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
