#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.parse import urlunparse

import openpyxl
import requests
from bs4 import BeautifulSoup


LOGGER = logging.getLogger("article-library")
CSV_LOCK = threading.RLock()
WECHAT_HOSTS = {"mp.weixin.qq.com"}
DUAL_STORAGE_AUTHOR_IDS = {"maobidao"}
INDEX_FIELDS = [
    "author_id",
    "author_name",
    "account",
    "account_username",
    "title",
    "date",
    "url",
    "local_path",
    "source_file",
    "source_article_no",
    "content_chars",
    "sha256",
]
REPORT_FIELDS = INDEX_FIELDS
FAILED_FIELDS = [
    "author_id",
    "author_name",
    "account",
    "account_username",
    "title",
    "date",
    "url",
    "source_excel",
    "source_row",
    "error",
]
DOWNLOAD_STATUS_FIELDS = [
    "author_id",
    "author_name",
    "account",
    "account_username",
    "title",
    "date",
    "url",
    "status",
    "error",
    "updated_at",
]
WECHAT_CACHE_FIELDS = [
    "author_id",
    "author_name",
    "account",
    "account_username",
    "title",
    "date",
    "url",
    "summary",
    "source_file",
    "source_row",
]
CATALOG_FIELDS = [
    "author_id",
    "author_name",
    "account",
    "account_username",
    "title",
    "date",
    "url",
    "summary",
    "status",
    "local_path",
    "source_file",
    "source_row",
]
# 已在微信页面验证为付费内容源的公众号。MzY5MDQxMDY1Ng== 对应“西风的指南”。
PAID_SOURCE_BIZ_IDS = {"MzY5MDQxMDY1Ng=="}
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def normalize_date(value: Any) -> str:
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(value or ""))
    if not match:
        return str(value or "").strip()
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def safe_name(value: str, fallback: str = "未命名") -> str:
    value = (value or "").strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return value[:100] or fallback


def article_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def normalize_wechat_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.hostname not in WECHAT_HOSTS:
        return str(value or "").strip()
    return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def is_paid_source_url(
    value: str,
    paid_source_biz_ids: set[str] | frozenset[str] = frozenset(PAID_SOURCE_BIZ_IDS),
) -> bool:
    parsed = urlparse(normalize_wechat_url(value))
    if parsed.hostname not in WECHAT_HOSTS:
        return False
    biz_values = parse_qs(parsed.query).get("__biz", [])
    return any(unquote(value) in paid_source_biz_ids for value in biz_values)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv_atomic(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def upsert_csv(path: Path, fields: list[str], row: dict[str, Any], key: str = "url") -> None:
    rows = read_csv(path)
    for current in rows:
        if current.get(key) == row.get(key):
            current.update(row)
            break
    else:
        rows.append(row)
    write_csv_atomic(path, fields, rows)


def remove_csv_row(path: Path, fields: list[str], value: str, key: str = "url") -> None:
    if not path.exists():
        return
    rows = [row for row in read_csv(path) if row.get(key) != value]
    write_csv_atomic(path, fields, rows)


def extract_text(soup: BeautifulSoup, selector: str) -> str:
    node = soup.select_one(selector)
    return node.get_text("\n", strip=True) if node else ""


def article_content_node(soup: BeautifulSoup) -> Any:
    return soup.select_one("#js_content") or soup.select_one(".rich_media_content")


def normalize_content_images(node: Any) -> None:
    for image in node.select("img"):
        source = image.get("data-src") or image.get("src") or ""
        if source:
            image["src"] = html.unescape(source)
        if not image.get("alt"):
            image["alt"] = "图片"


def content_as_markdown(node: Any) -> str:
    document = BeautifulSoup(str(node), "html.parser")
    normalize_content_images(document)
    for image in document.select("img"):
        source = image.get("src", "").strip()
        alt = image.get("alt", "图片").strip() or "图片"
        image.replace_with(f"\n\n![{alt}]({source})\n\n" if source else "")
    for line_break in document.select("br"):
        line_break.replace_with("\n")
    text = document.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def content_as_html(node: Any) -> str:
    document = BeautifulSoup(str(node), "html.parser")
    normalize_content_images(document)
    return document.decode_contents().strip()


def markdown_document(
    item: "InventoryArticle",
    title: str,
    published_date: str,
    content: str,
) -> str:
    return "\n".join(
        [
            f"# {title.strip()}",
            "",
            f"- 作者: {item.author_name}",
            f"- 公众号: {item.account}",
            f"- 日期: {published_date}",
            f"- URL: {item.url}",
            f"- 来源文件: {item.source_file}",
            f"- 来源文章序号: {item.source_row}",
            "",
            content.strip(),
            "",
        ]
    )


def html_document(
    item: "InventoryArticle",
    title: str,
    published_date: str,
    content: str,
) -> str:
    escaped_title = html.escape(title.strip())
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{escaped_title}</title>",
            "  <style>",
            "    body{margin:0;background:#f7f7f7;color:#1f2329;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.75;}",
            "    main{max-width:760px;margin:0 auto;background:#fff;min-height:100vh;padding:32px 22px;box-sizing:border-box;}",
            "    h1{font-size:26px;line-height:1.35;margin:0 0 14px;}",
            "    .meta{color:#646a73;font-size:14px;margin-bottom:28px;}",
            "    .content img{max-width:100%;height:auto;display:block;margin:14px auto;}",
            "    .content p{margin:0 0 1em;}",
            "    .content #js_content{visibility:visible!important;opacity:1!important;}",
            "  </style>",
            "</head>",
            "<body>",
            "  <main>",
            f"    <h1>{escaped_title}</h1>",
            "    <div class=\"meta\">",
            f"      作者: {html.escape(item.author_name)}<br>",
            f"      公众号: {html.escape(item.account)}<br>",
            f"      日期: {html.escape(published_date)}<br>",
            f"      URL: <a href=\"{html.escape(item.url, quote=True)}\">{html.escape(item.url)}</a><br>",
            f"      来源文件: {html.escape(item.source_file)} / {html.escape(str(item.source_row))}",
            "    </div>",
            f"    <article class=\"content\">{content}</article>",
            "  </main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def original_article_url(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")
    node = soup.select_one("#js_share_source[data-url]")
    return html.unescape(node.get("data-url", "")).strip() if node else ""


def is_paid_article(page_html: str, soup: BeautifulSoup | None = None) -> bool:
    paid_values = (
        r"\bis_pay_subscribe\s*[:=]\s*['\"]?([0-9]+)",
        r"\bpay_subscribe\s*[:=]\s*['\"]?([0-9]+)",
        r"\bis_paid\s*[:=]\s*['\"]?([0-9]+)",
    )
    for pattern in paid_values:
        if any(int(value) > 0 for value in re.findall(pattern, page_html, re.IGNORECASE)):
            return True

    document = soup or BeautifulSoup(page_html, "html.parser")
    visible_text = document.get_text(" ", strip=True)
    paid_markers = (
        "付费后阅读全文",
        "购买后阅读全文",
        "本文为付费内容",
        "支付后阅读全文",
        "使用微信豆购买",
    )
    return any(marker in visible_text for marker in paid_markers)


class PaidArticleError(Exception):
    pass


@dataclass(frozen=True)
class InventoryArticle:
    id: str
    author_id: str
    author_name: str
    account: str
    account_username: str
    title: str
    date: str
    url: str
    summary: str
    source_file: str
    source_row: int


@dataclass(frozen=True)
class AuthorConfig:
    id: str
    name: str
    sync_since: str
    accounts: tuple[dict[str, str], ...]
    paid_source_biz_ids: frozenset[str]

    @property
    def account_names(self) -> set[str]:
        return {account["name"] for account in self.accounts}

    @property
    def account_usernames(self) -> set[str]:
        return {account["username"] for account in self.accounts}

    def account_by_name(self, name: str) -> dict[str, str] | None:
        return next((account for account in self.accounts if account["name"] == name), None)

    def account_by_username(self, username: str) -> dict[str, str] | None:
        return next(
            (account for account in self.accounts if account["username"] == username),
            None,
        )


class ArticleLibrary:
    def __init__(self, project_root: Path, refresh_seconds: int = 1800) -> None:
        self.project_root = project_root.resolve()
        self.data_dir = self.project_root / "data"
        self.web_dir = self.project_root / "web"
        self.config_path = self.project_root / "config" / "authors.json"
        self.catalog_path = self.data_dir / "catalog" / "articles.csv"
        self.authors = self._load_authors()
        self.authors_by_id = {author.id: author for author in self.authors}
        self.author_by_username = {
            account["username"]: author
            for author in self.authors
            for account in author.accounts
        }
        self.author_by_account_name = {
            account["name"]: author
            for author in self.authors
            for account in author.accounts
        }
        self.paid_source_biz_ids = frozenset(
            biz for author in self.authors for biz in author.paid_source_biz_ids
        )
        self.refresh_seconds = max(refresh_seconds, 60)
        self._lock = threading.RLock()
        self._articles: list[InventoryArticle] = []
        self._by_id: dict[str, InventoryArticle] = {}
        self._last_refresh = ""
        self._wechat_sync: dict[str, Any] = {
            "available": False,
            "synced": 0,
            "last_sync": "",
            "warning": "尚未读取本机微信数据",
        }
        self._jobs: dict[str, dict[str, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="article-download")
        self._stop_event = threading.Event()

    def _load_authors(self) -> tuple[AuthorConfig, ...]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"作者配置不存在: {self.config_path}")
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        authors: list[AuthorConfig] = []
        for raw in payload.get("authors", []):
            author_id = str(raw.get("id", "")).strip()
            name = str(raw.get("name", "")).strip()
            accounts = tuple(raw.get("accounts", []))
            if not author_id or not name or not accounts:
                raise ValueError(f"作者配置不完整: {raw}")
            authors.append(
                AuthorConfig(
                    id=author_id,
                    name=name,
                    sync_since=normalize_date(raw.get("sync_since", "")),
                    accounts=accounts,
                    paid_source_biz_ids=frozenset(raw.get("paid_source_biz_ids", [])),
                )
            )
        if not authors:
            raise ValueError("authors.json 中没有作者配置")
        return tuple(authors)

    def _author_dir(self, author_id: str) -> Path:
        return self.data_dir / "authors" / author_id

    def _index_path(self, author_id: str) -> Path:
        return self._author_dir(author_id) / "index" / "articles.csv"

    def _status_path(self, author_id: str) -> Path:
        return self._author_dir(author_id) / "index" / "download_status.csv"

    def _wechat_cache_path(self, author_id: str) -> Path:
        return self._author_dir(author_id) / "sources" / "wechat_articles.csv"

    def _articles_dir(self, author_id: str) -> Path:
        return self._author_dir(author_id) / "articles"

    def _html_dir(self, author_id: str) -> Path:
        return self._author_dir(author_id) / "html"

    def _reports_dir(self, author_id: str) -> Path:
        return self._author_dir(author_id) / "reports"

    def load_inventory(self) -> dict[str, Any]:
        articles: list[InventoryArticle] = []
        seen_urls: set[str] = set()
        seen_titles: set[tuple[str, str, str]] = set()
        source_counts: dict[str, int] = {}
        author_counts: dict[str, int] = {}

        def append_article(current: InventoryArticle) -> None:
            title_key = (
                current.author_id,
                current.account,
                normalize_title(current.title),
            )
            if current.url in seen_urls or title_key in seen_titles:
                return
            articles.append(current)
            seen_urls.add(current.url)
            seen_titles.add(title_key)
            source_counts[current.account] = source_counts.get(current.account, 0) + 1
            author_counts[current.author_id] = author_counts.get(current.author_id, 0) + 1

        for author in self.authors:
            imports_dir = self._author_dir(author.id) / "sources" / "imports"
            for excel_path in sorted(imports_dir.glob("*.xlsx")):
                workbook = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
                try:
                    for sheet in workbook.worksheets:
                        rows = sheet.iter_rows(values_only=True)
                        try:
                            header = next(rows)
                        except StopIteration:
                            continue
                        columns = {
                            str(value).strip(): index
                            for index, value in enumerate(header)
                            if value is not None
                        }
                        if not {"公众号", "标题", "发布时间", "链接"}.issubset(columns):
                            continue
                        for row_number, row in enumerate(rows, 2):
                            def cell(name: str) -> Any:
                                index = columns.get(name, -1)
                                return row[index] if 0 <= index < len(row) else ""

                            title = str(cell("标题") or "").strip()
                            url = normalize_wechat_url(str(cell("链接") or "").strip())
                            account_name = str(cell("公众号") or excel_path.stem).strip()
                            account = author.account_by_name(account_name)
                            if (
                                not title
                                or not account
                                or urlparse(url).hostname not in WECHAT_HOSTS
                            ):
                                continue
                            append_article(
                                InventoryArticle(
                                    id=article_id(url),
                                    author_id=author.id,
                                    author_name=author.name,
                                    account=account["name"],
                                    account_username=account["username"],
                                    title=title,
                                    date=normalize_date(cell("发布时间")),
                                    url=url,
                                    summary=str(cell("摘要") or "").strip(),
                                    source_file=excel_path.name,
                                    source_row=row_number,
                                )
                            )
                finally:
                    workbook.close()

            cache_path = self._wechat_cache_path(author.id)
            for row_number, row in enumerate(read_csv(cache_path), 2):
                title = str(row.get("title", "")).strip()
                url = normalize_wechat_url(str(row.get("url", "")).strip())
                account_name = str(row.get("account", "")).strip()
                account_username = str(row.get("account_username", "")).strip()
                account = (
                    author.account_by_username(account_username)
                    or author.account_by_name(account_name)
                )
                if not title or not account or urlparse(url).hostname not in WECHAT_HOSTS:
                    continue
                append_article(
                    InventoryArticle(
                        id=article_id(url),
                        author_id=author.id,
                        author_name=author.name,
                        account=account["name"],
                        account_username=account["username"],
                        title=title,
                        date=normalize_date(row.get("date", "")),
                        url=url,
                        summary=str(row.get("summary", "")).strip(),
                        source_file=str(row.get("source_file", "")).strip() or "本机微信",
                        source_row=int(row.get("source_row", 0) or row_number),
                    )
                )

        articles.sort(key=lambda item: (item.date, item.source_row), reverse=True)
        refreshed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._articles = articles
            self._by_id = {item.id: item for item in articles}
            self._last_refresh = refreshed_at
        self._write_catalog(articles)

        LOGGER.info(
            "文章目录同步完成, total:%d, authors:%s, accounts:%s, refreshed_at:%s",
            len(articles),
            author_counts,
            source_counts,
            refreshed_at,
        )
        return {
            "total": len(articles),
            "authors": author_counts,
            "accounts": source_counts,
            "refreshed_at": refreshed_at,
        }

    def sync_from_wechat(self) -> dict[str, Any]:
        wx_path = shutil.which("wx")
        synced_at = datetime.now().astimezone().isoformat(timespec="seconds")
        if not wx_path:
            result = {
                "available": False,
                "synced": 0,
                "last_sync": synced_at,
                "warning": "未安装 wx-cli，无法读取本机微信公众号文章",
            }
            with self._lock:
                self._wechat_sync = result
            LOGGER.warning("微信文章同步跳过, reason:%s", result["warning"])
            return result

        LOGGER.info(
            "开始同步本机微信公众号文章, authors:%s",
            [author.id for author in self.authors],
        )
        raw_items: list[dict[str, Any]] = []
        try:
            for author in self.authors:
                for account in author.accounts:
                    command = [
                        wx_path,
                        "biz-articles",
                        "--account",
                        account["username"],
                        "-n",
                        "10000",
                        "--json",
                    ]
                    completed = subprocess.run(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    if completed.returncode != 0:
                        detail = (
                            completed.stderr or completed.stdout or "未知错误"
                        ).strip().splitlines()[-1]
                        if "Operation not permitted" in detail:
                            detail = "macOS 未授权当前应用读取微信数据，请授予完全磁盘访问权限"
                        raise RuntimeError(detail)
                    payload = self._parse_wx_json(completed.stdout)
                    raw_items.extend(self._wx_items(payload))
        except (
            OSError,
            subprocess.TimeoutExpired,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            result = {
                "available": False,
                "synced": 0,
                "last_sync": synced_at,
                "warning": f"读取本机微信失败：{exc}",
            }
            with self._lock:
                self._wechat_sync = result
            LOGGER.warning("微信文章同步失败, error:%s", exc)
            return result

        caches = {
            author.id: read_csv(self._wechat_cache_path(author.id))
            for author in self.authors
        }
        by_url = {
            author_id: {
                row.get("url", ""): row
                for row in rows
                if row.get("url")
            }
            for author_id, rows in caches.items()
        }
        matched_by_author = {author.id: 0 for author in self.authors}
        for item in raw_items:
            account_name = str(item.get("account", "")).strip()
            account_username = str(item.get("account_username", "")).strip()
            author = self.author_by_username.get(account_username)
            if not author:
                author = self.author_by_account_name.get(account_name)
            if not author:
                continue
            account = (
                author.account_by_username(account_username)
                or author.account_by_name(account_name)
            )
            if not account:
                continue
            title = str(item.get("title", "")).strip()
            url = normalize_wechat_url(str(item.get("url", "")).strip())
            if not title or urlparse(url).hostname not in WECHAT_HOSTS:
                continue
            timestamp = item.get("timestamp") or item.get("time") or ""
            if isinstance(timestamp, (int, float)) or str(timestamp).isdigit():
                date = datetime.fromtimestamp(int(timestamp)).astimezone().strftime("%Y-%m-%d")
                source_row = str(int(timestamp))
            else:
                date = normalize_date(timestamp)
                source_row = "0"
            if author.sync_since and date and date < author.sync_since:
                continue
            row = {
                "author_id": author.id,
                "author_name": author.name,
                "account": account["name"],
                "account_username": account["username"],
                "title": title,
                "date": date,
                "url": url,
                "summary": str(item.get("digest", "")).strip(),
                "source_file": "本机微信",
                "source_row": source_row,
            }
            if url in by_url[author.id]:
                by_url[author.id][url].update(row)
            else:
                caches[author.id].append(row)
                by_url[author.id][url] = row
            matched_by_author[author.id] += 1

        cached_counts: dict[str, int] = {}
        with CSV_LOCK:
            for author in self.authors:
                caches[author.id].sort(
                    key=lambda row: (row.get("date", ""), row.get("source_row", "")),
                    reverse=True,
                )
                write_csv_atomic(
                    self._wechat_cache_path(author.id),
                    WECHAT_CACHE_FIELDS,
                    caches[author.id],
                )
                cached_counts[author.id] = len(caches[author.id])
        result = {
            "available": True,
            "synced": sum(matched_by_author.values()),
            "synced_by_author": matched_by_author,
            "cached": sum(cached_counts.values()),
            "cached_by_author": cached_counts,
            "last_sync": synced_at,
            "warning": "",
        }
        with self._lock:
            self._wechat_sync = result
        LOGGER.info(
            "本机微信公众号文章同步完成, matched:%s, cached:%s",
            matched_by_author,
            cached_counts,
        )
        return result

    @staticmethod
    def _parse_wx_json(output: str) -> Any:
        content = output.strip()
        starts = [index for index in (content.find("["), content.find("{")) if index >= 0]
        if not starts:
            raise ValueError("未找到 JSON 内容")
        return json.loads(content[min(starts) :])

    @staticmethod
    def _wx_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            raise ValueError("JSON 顶层结构不是数组或对象")
        for key in ("articles", "messages", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        raise ValueError("JSON 中没有公众号文章数组")

    def refresh_inventory(self, include_wechat: bool = True) -> dict[str, Any]:
        wechat = self.sync_from_wechat() if include_wechat else dict(self._wechat_sync)
        inventory = self.load_inventory()
        inventory["wechat"] = wechat
        return inventory

    def start_scheduler(self) -> None:
        def refresh_loop() -> None:
            while not self._stop_event.wait(self.refresh_seconds):
                try:
                    self.refresh_inventory(include_wechat=True)
                except Exception:
                    LOGGER.exception("定时同步文章目录失败")

        threading.Thread(target=refresh_loop, name="inventory-refresh", daemon=True).start()
        LOGGER.info("文章目录定时同步已启动, interval_seconds:%d", self.refresh_seconds)

    def close(self) -> None:
        self._stop_event.set()
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _downloaded_maps(
        self, author_id: str
    ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
        rows = read_csv(self._index_path(author_id))
        return (
            {row.get("url", ""): row for row in rows if row.get("url")},
            {
                normalize_title(row.get("title", "")): row
                for row in rows
                if row.get("title")
            },
        )

    def _download_status_maps(
        self, author_id: str
    ) -> tuple[dict[str, dict[str, str]], dict[tuple[str, str], dict[str, str]]]:
        rows = read_csv(self._status_path(author_id))
        return (
            {row.get("url", ""): row for row in rows if row.get("url")},
            {
                (row.get("account", ""), normalize_title(row.get("title", ""))): row
                for row in rows
                if row.get("title")
            },
        )

    def _status_for(
        self,
        item: InventoryArticle,
        by_url: dict[str, dict[str, str]],
        by_title: dict[str, dict[str, str]],
        status_by_url: dict[str, dict[str, str]] | None = None,
        status_by_title: dict[tuple[str, str], dict[str, str]] | None = None,
    ) -> tuple[str, str]:
        indexed = by_url.get(item.url) or by_title.get(normalize_title(item.title))
        if indexed:
            local_path = indexed.get("local_path", "")
            if local_path and (self.project_root / local_path).is_file():
                return "downloaded", local_path

        status_row = (status_by_url or {}).get(item.url) or (status_by_title or {}).get(
            (item.account, normalize_title(item.title))
        )
        if status_row and status_row.get("status") in {"paid", "failed"}:
            return status_row["status"], ""
        return "missing", ""

    def _status_context(
        self,
    ) -> dict[
        str,
        tuple[
            dict[str, dict[str, str]],
            dict[str, dict[str, str]],
            dict[str, dict[str, str]],
            dict[tuple[str, str], dict[str, str]],
        ],
    ]:
        return {
            author.id: (
                *self._downloaded_maps(author.id),
                *self._download_status_maps(author.id),
            )
            for author in self.authors
        }

    def _write_catalog(self, articles: list[InventoryArticle]) -> None:
        context = self._status_context()
        rows: list[dict[str, Any]] = []
        for item in articles:
            by_url, by_title, status_by_url, status_by_title = context[item.author_id]
            status, local_path = self._status_for(
                item,
                by_url,
                by_title,
                status_by_url,
                status_by_title,
            )
            row = asdict(item)
            row.update({"status": status, "local_path": local_path})
            rows.append(row)
        with CSV_LOCK:
            write_csv_atomic(self.catalog_path, CATALOG_FIELDS, rows)

    def stats(self, author_id: str = "all") -> dict[str, Any]:
        with self._lock:
            articles = [
                item
                for item in self._articles
                if author_id == "all" or item.author_id == author_id
            ]
            refreshed_at = self._last_refresh
        context = self._status_context()
        status_counts = {"downloaded": 0, "missing": 0, "paid": 0, "failed": 0}
        account_counts: dict[str, int] = {}
        author_counts: dict[str, int] = {}
        years: set[str] = set()
        for item in articles:
            by_url, by_title, status_by_url, status_by_title = context[item.author_id]
            item_status, _ = self._status_for(
                item,
                by_url,
                by_title,
                status_by_url,
                status_by_title,
            )
            status_counts[item_status] += 1
            account_counts[item.account] = account_counts.get(item.account, 0) + 1
            author_counts[item.author_id] = author_counts.get(item.author_id, 0) + 1
            if len(item.date) >= 4:
                years.add(item.date[:4])
        return {
            "total": len(articles),
            **status_counts,
            "author_id": author_id,
            "authors": [
                {
                    "id": author.id,
                    "name": author.name,
                    "count": author_counts.get(author.id, 0),
                }
                for author in self.authors
            ],
            "accounts": account_counts,
            "years": sorted(years, reverse=True),
            "last_refresh": refreshed_at,
            "refresh_seconds": self.refresh_seconds,
            "wechat": dict(self._wechat_sync),
        }

    def list_articles(self, query: dict[str, list[str]]) -> dict[str, Any]:
        keyword = query.get("q", [""])[0].strip().lower()
        author_id = query.get("author_id", ["all"])[0]
        account = query.get("account", ["all"])[0]
        year = query.get("year", ["all"])[0]
        status = query.get("status", ["all"])[0]
        try:
            page = max(int(query.get("page", ["1"])[0]), 1)
            page_size = min(max(int(query.get("page_size", ["30"])[0]), 1), 100)
        except ValueError:
            page, page_size = 1, 30

        with self._lock:
            articles = list(self._articles)
        context = self._status_context()
        result: list[dict[str, Any]] = []
        for item in articles:
            by_url, by_title, status_by_url, status_by_title = context[item.author_id]
            item_status, local_path = self._status_for(
                item,
                by_url,
                by_title,
                status_by_url,
                status_by_title,
            )
            if keyword and keyword not in f"{item.title} {item.summary} {item.account}".lower():
                continue
            if author_id != "all" and item.author_id != author_id:
                continue
            if account != "all" and item.account != account:
                continue
            if year != "all" and not item.date.startswith(f"{year}-"):
                continue
            if status != "all" and item_status != status:
                continue
            payload = asdict(item)
            payload.update({"status": item_status, "local_path": local_path})
            result.append(payload)

        start = (page - 1) * page_size
        return {
            "items": result[start : start + page_size],
            "total": len(result),
            "page": page,
            "page_size": page_size,
            "pages": max((len(result) + page_size - 1) // page_size, 1),
        }

    def get_article(self, item_id: str) -> InventoryArticle | None:
        with self._lock:
            return self._by_id.get(item_id)

    def local_file(self, item_id: str) -> Path | None:
        item = self.get_article(item_id)
        if not item:
            return None
        by_url, by_title = self._downloaded_maps(item.author_id)
        status, local_path = self._status_for(item, by_url, by_title)
        if status != "downloaded":
            return None
        path = (self.project_root / local_path).resolve()
        try:
            path.relative_to(self._articles_dir(item.author_id).resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    def submit_download(
        self,
        ids: list[str] | None = None,
        all_missing: bool = False,
        author_id: str = "all",
    ) -> dict[str, Any]:
        context = self._status_context()
        if all_missing:
            with self._lock:
                candidates = [
                    item
                    for item in self._articles
                    if author_id == "all" or item.author_id == author_id
                ]
        else:
            candidates = [
                item
                for item_id in dict.fromkeys(ids or [])
                if (item := self.get_article(item_id)) is not None
            ]
        valid_ids = [
            item.id
            for item in candidates
            if self._status_for(item, *context[item.author_id])[0]
            == "missing"
        ]
        if not valid_ids:
            raise ValueError("没有可下载的文章")
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "queued",
            "total": len(valid_ids),
            "completed": 0,
            "succeeded": 0,
            "paid": 0,
            "failed": 0,
            "current": "",
            "errors": [],
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with self._lock:
            self._jobs[job_id] = job
        self._executor.submit(self._run_download_job, job_id, valid_ids)
        LOGGER.info("下载任务已提交, job_id:%s, total:%d", job_id, len(valid_ids))
        return dict(job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def _update_job(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _run_download_job(self, job_id: str, ids: list[str]) -> None:
        self._update_job(job_id, status="running")
        for index, item_id in enumerate(ids, 1):
            item = self.get_article(item_id)
            if not item:
                continue
            self._update_job(job_id, current=item.title)
            try:
                self._download_one(item)
                with self._lock:
                    self._jobs[job_id]["succeeded"] += 1
            except PaidArticleError as exc:
                self._record_download_status(item, "paid", str(exc))
                LOGGER.info(
                    "跳过付费文章, job_id:%s, article_id:%s, title:%s",
                    job_id,
                    item.id,
                    item.title,
                )
                with self._lock:
                    self._jobs[job_id]["paid"] += 1
            except Exception as exc:
                LOGGER.exception(
                    "文章下载失败, job_id:%s, article_id:%s, title:%s",
                    job_id,
                    item.id,
                    item.title,
                )
                self._record_failure(item, str(exc))
                self._record_download_status(item, "failed", str(exc))
                with self._lock:
                    self._jobs[job_id]["failed"] += 1
                    self._jobs[job_id]["errors"].append({"title": item.title, "error": str(exc)})
                    self._jobs[job_id]["errors"] = self._jobs[job_id]["errors"][-20:]
            self._update_job(job_id, completed=index)
            if index < len(ids):
                time.sleep(0.8)
        self._update_job(
            job_id,
            status="finished",
            current="",
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        with self._lock:
            self._write_catalog(list(self._articles))
        job = self.get_job(job_id) or {}
        LOGGER.info(
            "下载任务完成, job_id:%s, succeeded:%d, paid:%d, failed:%d",
            job_id,
            job.get("succeeded", 0),
            job.get("paid", 0),
            job.get("failed", 0),
        )

    def _download_one(self, item: InventoryArticle) -> Path:
        parsed = urlparse(item.url)
        if parsed.scheme != "https" or parsed.hostname not in WECHAT_HOSTS:
            raise ValueError("文章链接不是受支持的微信链接")

        LOGGER.info("开始下载文章, article_id:%s, account:%s, title:%s", item.id, item.account, item.title)
        response = requests.get(item.url, headers=REQUEST_HEADERS, timeout=30)
        response.raise_for_status()
        page_html = response.text
        soup = BeautifulSoup(page_html, "html.parser")
        if is_paid_article(page_html, soup):
            raise PaidArticleError("付费文章，不下载正文")
        if "wappoc_appmsgcaptcha" in response.url or "环境异常" in soup.get_text(" ", strip=True):
            raise ValueError("微信访问触发验证，稍后重试")
        content_node = article_content_node(soup)
        content = content_node.get_text("\n", strip=True) if content_node else ""
        source_url = normalize_wechat_url(original_article_url(page_html))
        if not content and source_url:
            if is_paid_source_url(source_url, self.paid_source_biz_ids):
                raise PaidArticleError("源文章为“西风的指南”付费内容，不下载正文")
            source = urlparse(source_url)
            if source.scheme == "https" and source.hostname in WECHAT_HOSTS:
                LOGGER.info(
                    "跟随阅读全文源链接, article_id:%s, source_url:%s",
                    item.id,
                    source_url,
                )
                source_response = requests.get(source_url, headers=REQUEST_HEADERS, timeout=30)
                source_response.raise_for_status()
                page_html = source_response.text
                soup = BeautifulSoup(source_response.text, "html.parser")
                if is_paid_article(source_response.text, soup):
                    raise PaidArticleError("付费文章，不下载正文")
                content_node = article_content_node(soup)
                content = content_node.get_text("\n", strip=True) if content_node else ""

        if not content or "此内容发送失败无法查看" in page_html[:1500]:
            raise ValueError("微信页面正文为空或已不可查看")

        title = extract_text(soup, "#activity-name") or extract_text(soup, ".rich_media_title") or item.title
        published_date = normalize_date(extract_text(soup, "#publish_time")) or item.date
        year = published_date[:4] if published_date[:4].isdigit() else item.date[:4]
        if not year.isdigit():
            year = "未知年份"
        body = "\n".join(
            [
                f"标题: {title.strip()}",
                f"作者: {item.author_name}",
                f"公众号: {item.account}",
                f"日期: {published_date}",
                f"URL: {item.url}",
                f"来源文件: {item.source_file}",
                f"来源文章序号: {item.source_row}",
                "",
                content.strip(),
                "",
            ]
        )
        dual_storage = item.author_id in DUAL_STORAGE_AUTHOR_IDS and content_node is not None
        if dual_storage:
            markdown_body = markdown_document(
                item,
                title,
                published_date,
                content_as_markdown(content_node),
            )
            html_body = html_document(
                item,
                title,
                published_date,
                content_as_html(content_node),
            )
        destination_dir = self._articles_dir(item.author_id) / year
        destination_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".md" if dual_storage else ".txt"
        destination = destination_dir / f"{safe_name(published_date)}_{safe_name(title)}{suffix}"

        with CSV_LOCK:
            existing = self._existing_index_row(item)
            old_path: Path | None = None
            if existing and existing.get("local_path"):
                old_path = (self.project_root / existing["local_path"]).resolve()
                if old_path.is_file() and not dual_storage:
                    destination = old_path
            if dual_storage:
                html_dir = self._html_dir(item.author_id) / year
                html_dir.mkdir(parents=True, exist_ok=True)
                html_destination = html_dir / f"{safe_name(published_date)}_{safe_name(title)}.html"
                destination.write_text(markdown_body, encoding="utf-8")
                html_destination.write_text(html_body, encoding="utf-8")
                if (
                    old_path
                    and old_path.suffix == ".txt"
                    and old_path.is_file()
                    and old_path.parent.resolve() == destination_dir.resolve()
                ):
                    old_path.unlink()
            else:
                destination.write_text(body, encoding="utf-8")
            stored_body = markdown_body if dual_storage else body
            index_row = {
                "author_id": item.author_id,
                "author_name": item.author_name,
                "account": item.account,
                "account_username": item.account_username,
                "title": title.strip(),
                "date": published_date,
                "url": item.url,
                "local_path": str(destination.relative_to(self.project_root)),
                "source_file": item.source_file,
                "source_article_no": str(item.source_row),
                "content_chars": str(len(content.strip())),
                "sha256": hashlib.sha256(stored_body.encode("utf-8")).hexdigest(),
            }
            upsert_csv(self._index_path(item.author_id), INDEX_FIELDS, index_row)
            upsert_csv(
                self._reports_dir(item.author_id) / f"download_report_{year}.csv",
                REPORT_FIELDS,
                index_row,
            )
            remove_csv_row(
                self._reports_dir(item.author_id) / f"download_failed_{year}.csv",
                FAILED_FIELDS,
                item.url,
            )
            remove_csv_row(
                self._status_path(item.author_id),
                DOWNLOAD_STATUS_FIELDS,
                item.url,
            )

        LOGGER.info(
            "文章下载成功, article_id:%s, account:%s, local_path:%s",
            item.id,
            item.account,
            destination.relative_to(self.project_root),
        )
        return destination

    def _existing_index_row(self, item: InventoryArticle) -> dict[str, str] | None:
        for row in read_csv(self._index_path(item.author_id)):
            if row.get("url") == item.url or (
                normalize_title(row.get("title", "")) == normalize_title(item.title)
            ):
                return row
        return None

    def _record_failure(self, item: InventoryArticle, error: str) -> None:
        year = item.date[:4] if item.date[:4].isdigit() else "unknown"
        row = {
            "author_id": item.author_id,
            "author_name": item.author_name,
            "account": item.account,
            "account_username": item.account_username,
            "title": item.title,
            "date": item.date,
            "url": item.url,
            "source_excel": item.source_file,
            "source_row": str(item.source_row),
            "error": error,
        }
        with CSV_LOCK:
            upsert_csv(
                self._reports_dir(item.author_id) / f"download_failed_{year}.csv",
                FAILED_FIELDS,
                row,
            )

    def _record_download_status(self, item: InventoryArticle, status: str, error: str) -> None:
        row = {
            "author_id": item.author_id,
            "author_name": item.author_name,
            "account": item.account,
            "account_username": item.account_username,
            "title": item.title,
            "date": item.date,
            "url": item.url,
            "status": status,
            "error": error,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with CSV_LOCK:
            upsert_csv(self._status_path(item.author_id), DOWNLOAD_STATUS_FIELDS, row)


class ArticleLibraryHandler(BaseHTTPRequestHandler):
    server_version = "ArticleLibrary/1.0"

    @property
    def library(self) -> ArticleLibrary:
        return self.server.library  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args: Any) -> None:
        LOGGER.info("HTTP %s, client:%s", format_string % args, self.client_address[0])

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > 1_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/api/stats":
                author_id = query.get("author_id", ["all"])[0]
                self._json(
                    HTTPStatus.OK,
                    {"success": True, "data": self.library.stats(author_id)},
                )
                return
            if path == "/api/authors":
                self._json(
                    HTTPStatus.OK,
                    {
                        "success": True,
                        "data": [
                            {"id": author.id, "name": author.name}
                            for author in self.library.authors
                        ],
                    },
                )
                return
            if path == "/api/articles":
                self._json(HTTPStatus.OK, {"success": True, "data": self.library.list_articles(query)})
                return
            if path.startswith("/api/jobs/"):
                job = self.library.get_job(path.rsplit("/", 1)[-1])
                if not job:
                    self._json(HTTPStatus.NOT_FOUND, {"success": False, "message": "下载任务不存在"})
                    return
                self._json(HTTPStatus.OK, {"success": True, "data": job})
                return
            if path.startswith("/api/articles/") and path.endswith("/file"):
                item_id = path.split("/")[3]
                file_path = self.library.local_file(item_id)
                if not file_path:
                    self._json(HTTPStatus.NOT_FOUND, {"success": False, "message": "本地文章文件不存在"})
                    return
                self._send_file(file_path, attachment=True)
                return
            self._serve_static(path)
        except Exception as exc:
            LOGGER.exception("处理 GET 请求失败, path:%s", path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"success": False, "message": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/sync":
                result = self.library.refresh_inventory(include_wechat=True)
                warning = result.get("wechat", {}).get("warning", "")
                message = "微信文章同步成功" if not warning else f"历史目录已刷新；微信同步未完成：{warning}"
                self._json(HTTPStatus.OK, {"success": True, "message": message, "data": result})
                return
            if path == "/api/downloads":
                payload = self._read_json()
                ids = payload.get("ids", [])
                if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                    raise ValueError("ids 必须是文章 ID 数组")
                job = self.library.submit_download(
                    ids,
                    all_missing=payload.get("all_missing") is True,
                    author_id=str(payload.get("author_id", "all")),
                )
                self._json(
                    HTTPStatus.ACCEPTED,
                    {"success": True, "message": "下载任务创建成功", "data": job},
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"success": False, "message": "接口不存在"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"success": False, "message": str(exc)})
        except Exception as exc:
            LOGGER.exception("处理 POST 请求失败, path:%s", path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"success": False, "message": str(exc)})

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        requested = (self.library.web_dir / relative).resolve()
        try:
            requested.relative_to(self.library.web_dir.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not requested.is_file():
            requested = self.library.web_dir / "index.html"
        self._send_file(requested)

    def _send_file(self, path: Path, attachment: bool = False) -> None:
        content = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        if attachment:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{requests.utils.quote(path.name)}")
        self.end_headers()
        self.wfile.write(content)


class LibraryHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], library: ArticleLibrary) -> None:
        super().__init__(address, ArticleLibraryHandler)
        self.library = library


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="记忆承载文章管理页")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--host", default=os.getenv("ARTICLE_LIBRARY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ARTICLE_LIBRARY_PORT", "8765")))
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=int(os.getenv("ARTICLE_LIBRARY_REFRESH_SECONDS", "1800")),
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args()
    library = ArticleLibrary(Path(args.project_root), refresh_seconds=args.refresh_seconds)
    library.refresh_inventory(include_wechat=True)
    library.start_scheduler()
    server = LibraryHTTPServer((args.host, args.port), library)
    LOGGER.info(
        "文章管理页启动成功, url:http://%s:%d, project_root:%s",
        args.host,
        args.port,
        library.project_root,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("收到退出信号，正在停止文章管理页")
    finally:
        server.server_close()
        library.close()


if __name__ == "__main__":
    main()
