from __future__ import annotations

import csv
import hashlib
import argparse
import html
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List

import openpyxl
import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
ARTICLES_DIR = DATA_DIR / "articles" / "记忆承载"
INDEX_PATH = DATA_DIR / "articles_index.csv"


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}


def norm_title(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def normalize_date(value: str) -> str:
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(value or ""))
    if not match:
        return str(value or "").strip()
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def safe_name(value: str, fallback: str = "未命名") -> str:
    value = (value or "").strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.rstrip(". ")
    return value[:80] or fallback


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 2
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def existing_articles() -> tuple[set[str], Dict[str, Path]]:
    titles: set[str] = set()
    url_to_path: Dict[str, Path] = {}
    for path in ARTICLES_DIR.glob("**/*.txt"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        title_match = re.search(r"(?m)^标题[:：]\s*(.+)$", text)
        url_match = re.search(r"(?m)^URL[:：]\s*(.+)$", text)
        if title_match:
            titles.add(norm_title(title_match.group(1)))
        if url_match:
            url_to_path[url_match.group(1).strip()] = path
    return titles, url_to_path


def excel_rows_for_year(target_year: str) -> List[dict]:
    rows: List[dict] = []
    for excel_path in sorted(DATA_DIR.glob("*.xlsx")):
        workbook = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
        for sheet in workbook.worksheets:
            iterator = sheet.iter_rows(values_only=True)
            header = next(iterator)
            idx = {str(value).strip(): i for i, value in enumerate(header) if value is not None}
            for row_number, row in enumerate(iterator, 2):
                if "标题" not in idx or "发布时间" not in idx or "链接" not in idx:
                    continue
                title = row[idx["标题"]] if idx["标题"] < len(row) else None
                published_at = row[idx["发布时间"]] if idx["发布时间"] < len(row) else None
                link = row[idx["链接"]] if idx["链接"] < len(row) else None
                if not title or not link:
                    continue
                date = normalize_date(str(published_at or ""))
                if not date.startswith(f"{target_year}-"):
                    continue
                rows.append(
                    {
                        "account": "记忆承载",
                        "title": str(title).strip(),
                        "date": date,
                        "url": str(link).strip(),
                        "source_excel": excel_path.name,
                        "source_row": row_number,
                    }
                )
    return rows


def text_for(soup: BeautifulSoup, selector: str) -> str:
    node = soup.select_one(selector)
    if not node:
        return ""
    return node.get_text("\n", strip=True)


def parse_article(html: str, fallback: dict) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = text_for(soup, "#activity-name") or text_for(soup, ".rich_media_title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    date = text_for(soup, "#publish_time")
    content = text_for(soup, "#js_content") or text_for(soup, ".rich_media_content")
    return {
        "title": title.strip() or fallback["title"],
        "author": "记忆承载",
        "date": normalize_date(date) or fallback["date"],
        "url": fallback["url"],
        "content": content.strip(),
    }


def original_article_url(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")
    node = soup.select_one("#js_share_source[data-url]")
    if not node:
        return ""
    return html.unescape(node.get("data-url", "")).strip()


def article_content(article: dict, source: dict) -> str:
    return "\n".join(
        [
            f"标题: {article['title']}",
            f"作者: {article['author']}",
            f"日期: {article['date']}",
            f"URL: {article['url']}",
            f"来源文件: {source['source_excel']}",
            f"来源文章序号: {source['source_row']}",
            "",
            article["content"],
            "",
        ]
    )


def read_index() -> tuple[List[dict], List[str]]:
    if not INDEX_PATH.exists():
        return [], [
            "account",
            "title",
            "date",
            "url",
            "local_path",
            "source_file",
            "source_article_no",
            "content_chars",
            "sha256",
        ]
    with INDEX_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return list(reader), list(reader.fieldnames or [])


def write_index(rows: Iterable[dict], fieldnames: List[str]) -> None:
    with INDEX_PATH.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="按年份从 Excel 链接补下载微信文章")
    parser.add_argument("--year", default="2024", help="要下载的年份，例如 2025")
    args = parser.parse_args()
    target_year = args.year
    failed_path = DATA_DIR / f"download_failed_{target_year}.csv"
    report_path = DATA_DIR / f"download_report_{target_year}.csv"

    ARTICLES_DIR.joinpath(target_year).mkdir(parents=True, exist_ok=True)
    existing_titles, url_to_path = existing_articles()
    targets = [row for row in excel_rows_for_year(target_year) if norm_title(row["title"]) not in existing_titles]

    index_rows, fieldnames = read_index()
    by_url = {row.get("url", ""): row for row in index_rows if row.get("url")}
    downloaded: List[dict] = []
    failed: List[dict] = []
    session = requests.Session()
    session.headers.update(HEADERS)

    print(f"{target_year} 待下载: {len(targets)}", flush=True)
    for idx, target in enumerate(targets, 1):
        try:
            time.sleep(random.uniform(0.15, 0.45))
            response = session.get(target["url"], timeout=25)
            response.raise_for_status()
            article = parse_article(response.text, target)
            if not article["content"]:
                source_url = original_article_url(response.text)
                if source_url:
                    time.sleep(random.uniform(0.15, 0.45))
                    source_response = session.get(source_url, timeout=25)
                    source_response.raise_for_status()
                    article = parse_article(source_response.text, target)
            if not article["content"] or "此内容发送失败无法查看" in response.text[:1000]:
                raise ValueError(f"正文为空或不可查看，长度={len(article['content'])}")

            content = article_content(article, target)
            old_placeholder = url_to_path.get(target["url"])
            if old_placeholder and old_placeholder.exists():
                destination = old_placeholder.with_name(f"{safe_name(article['date'])}_{safe_name(article['title'])}.txt")
                destination = unique_path(destination)
                old_placeholder.unlink()
            else:
                destination = unique_path(
                    ARTICLES_DIR / target_year / f"{safe_name(article['date'])}_{safe_name(article['title'])}.txt"
                )
            destination.write_text(content, encoding="utf-8")

            row = {
                "account": "记忆承载",
                "title": article["title"],
                "date": article["date"],
                "url": article["url"],
                "local_path": str(destination.relative_to(ROOT)),
                "source_file": target["source_excel"],
                "source_article_no": str(target["source_row"]),
                "content_chars": str(len(article["content"])),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
            if target["url"] in by_url:
                by_url[target["url"]].update(row)
            else:
                index_rows.append(row)
                by_url[target["url"]] = row
            downloaded.append(row)
            existing_titles.add(norm_title(article["title"]))
            if idx % 20 == 0 or idx == len(targets):
                print(f"进度: {idx}/{len(targets)}，成功 {len(downloaded)}，失败 {len(failed)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            fail = dict(target)
            fail["error"] = str(exc)
            failed.append(fail)
            print(f"失败: {target['date']} {target['title']} | {exc}", flush=True)

    write_index(index_rows, fieldnames)

    with failed_path.open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["account", "title", "date", "url", "source_excel", "source_row", "error"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(failed)

    with report_path.open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["account", "title", "date", "url", "local_path", "source_file", "source_article_no", "content_chars", "sha256"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(downloaded)

    print(f"下载成功: {len(downloaded)}", flush=True)
    print(f"下载失败: {len(failed)}", flush=True)
    print(f"失败清单: {failed_path.relative_to(ROOT)}", flush=True)
    print(f"下载报告: {report_path.relative_to(ROOT)}", flush=True)
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
