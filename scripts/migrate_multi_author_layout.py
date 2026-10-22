#!/usr/bin/env python3
from __future__ import annotations

import csv
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
AUTHOR_ID = "jiyichengzai"
AUTHOR_NAME = "记忆承载"
AUTHOR_DIR = DATA / "authors" / AUTHOR_ID
ACCOUNT_USERNAMES = {
    "记忆承载": "gh_e4d226f07619",
    "记忆承载3": "gh_0a7d5b8bdb94",
}


def move_if_present(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if destination.exists():
        raise FileExistsError(f"目标已存在，停止迁移: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def enrich_csv(path: Path, fields: list[str]) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    for row in rows:
        row["author_id"] = AUTHOR_ID
        row["author_name"] = AUTHOR_NAME
        row["account_username"] = ACCOUNT_USERNAMES.get(row.get("account", ""), "")
        local_path = row.get("local_path", "")
        if local_path.startswith("data/articles/记忆承载/"):
            row["local_path"] = local_path.replace(
                "data/articles/记忆承载/",
                "data/authors/jiyichengzai/articles/",
                1,
            )
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main() -> None:
    (DATA / "authors").mkdir(exist_ok=True)
    (DATA / "catalog").mkdir(exist_ok=True)
    for author_id in ("jiyichengzai", "maobidao"):
        base = DATA / "authors" / author_id
        for relative in ("articles", "index", "reports", "sources/imports", "legacy"):
            (base / relative).mkdir(parents=True, exist_ok=True)

    destination_articles = AUTHOR_DIR / "articles"
    if destination_articles.exists() and not any(destination_articles.iterdir()):
        destination_articles.rmdir()
    move_if_present(DATA / "articles" / "记忆承载", destination_articles)
    old_articles_root = DATA / "articles"
    if old_articles_root.exists() and not any(old_articles_root.iterdir()):
        old_articles_root.rmdir()

    move_if_present(DATA / "articles_index.csv", AUTHOR_DIR / "index" / "articles.csv")
    move_if_present(
        DATA / "article_download_status.csv",
        AUTHOR_DIR / "index" / "download_status.csv",
    )
    move_if_present(
        DATA / "wechat_articles.csv",
        AUTHOR_DIR / "sources" / "wechat_articles.csv",
    )

    for source in sorted(DATA.glob("*.xlsx")):
        move_if_present(source, AUTHOR_DIR / "sources" / "imports" / source.name)

    report_patterns = (
        "download_report_*.csv",
        "download_failed_*.csv",
        "download_summary_*.csv",
        "download_summary_*.md",
    )
    for pattern in report_patterns:
        for source in sorted(DATA.glob(pattern)):
            move_if_present(source, AUTHOR_DIR / "reports" / source.name)

    legacy_patterns = (
        "crawled_articles*.txt",
        "downloaded_articles_parsed.csv",
        "missing_articles*.csv",
        "missing_articles*.md",
        "txt_titles_not_in_excel.csv",
    )
    for pattern in legacy_patterns:
        for source in sorted(DATA.glob(pattern)):
            move_if_present(source, AUTHOR_DIR / "legacy" / source.name)

    index_fields = [
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
    status_fields = [
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
    source_fields = [
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
    failure_fields = [
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

    enrich_csv(AUTHOR_DIR / "index" / "articles.csv", index_fields)
    enrich_csv(AUTHOR_DIR / "index" / "download_status.csv", status_fields)
    enrich_csv(AUTHOR_DIR / "sources" / "wechat_articles.csv", source_fields)
    for path in (AUTHOR_DIR / "reports").glob("download_report_*.csv"):
        enrich_csv(path, index_fields)
    for path in (AUTHOR_DIR / "reports").glob("download_failed_*.csv"):
        enrich_csv(path, failure_fields)

    print(f"迁移完成: {AUTHOR_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
