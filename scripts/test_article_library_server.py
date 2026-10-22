#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import Workbook

from scripts.article_library_server import (
    ArticleLibrary,
    PaidArticleError,
    article_id,
    is_paid_article,
    is_paid_source_url,
    normalize_wechat_url,
    safe_name,
)


class ArticleLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data").mkdir()
        (self.root / "web").mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "authors.json").write_text(
            json.dumps(
                {
                    "authors": [
                        {
                            "id": "jiyichengzai",
                            "name": "记忆承载",
                            "accounts": [
                                {
                                    "id": "jiyichengzai",
                                    "name": "记忆承载",
                                    "username": "gh_jiyi",
                                    "biz": "biz_jiyi",
                                },
                                {
                                    "id": "jiyichengzai3",
                                    "name": "记忆承载3",
                                    "username": "gh_jiyi3",
                                    "biz": "biz_jiyi3",
                                },
                            ],
                            "paid_source_biz_ids": ["MzY5MDQxMDY1Ng=="],
                        },
                        {
                            "id": "maobidao",
                            "name": "猫笔刀",
                            "accounts": [
                                {
                                    "id": "maobidao",
                                    "name": "猫笔刀",
                                    "username": "gh_maobidao",
                                    "biz": "biz_maobidao",
                                }
                            ],
                            "paid_source_biz_ids": [],
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for author_id in ("jiyichengzai", "maobidao"):
            base = self.root / "data" / "authors" / author_id
            for relative in ("articles", "index", "reports", "sources/imports"):
                (base / relative).mkdir(parents=True)
        self._write_inventory()
        self.library = ArticleLibrary(self.root, refresh_seconds=60)
        self.library.load_inventory()

    def tearDown(self) -> None:
        self.library.close()
        self.temporary.cleanup()

    def _write_inventory(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["公众号", "标题", "摘要", "发布时间", "链接"])
        sheet.append(
            [
                "记忆承载",
                "测试文章一",
                "第一篇摘要",
                "2026-10-20 08:00:00",
                "https://mp.weixin.qq.com/s/article-one",
            ]
        )
        sheet.append(
            [
                "记忆承载",
                "不安全链接",
                "",
                "2026-10-19 08:00:00",
                "https://example.com/not-wechat",
            ]
        )
        workbook.save(
            self.root
            / "data"
            / "authors"
            / "jiyichengzai"
            / "sources"
            / "imports"
            / "记忆承载.xlsx"
        )

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["公众号", "标题", "摘要", "发布时间", "链接"])
        sheet.append(
            [
                "记忆承载3",
                "测试文章二",
                "第二篇摘要",
                "2025-02-03 09:00:00",
                "https://mp.weixin.qq.com/s/article-two",
            ]
        )
        workbook.save(
            self.root
            / "data"
            / "authors"
            / "jiyichengzai"
            / "sources"
            / "imports"
            / "记忆承载3.xlsx"
        )

    def _write_index(self) -> None:
        article_path = (
            self.root
            / "data"
            / "authors"
            / "jiyichengzai"
            / "articles"
            / "2026"
            / "article.txt"
        )
        article_path.parent.mkdir(parents=True)
        article_path.write_text("正文", encoding="utf-8")
        with (
            self.root / "data" / "authors" / "jiyichengzai" / "index" / "articles.csv"
        ).open(
            "w", encoding="utf-8-sig", newline=""
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
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
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "author_id": "jiyichengzai",
                    "author_name": "记忆承载",
                    "account": "记忆承载",
                    "account_username": "gh_jiyi",
                    "title": "测试文章一",
                    "date": "2026-10-20",
                    "url": "https://mp.weixin.qq.com/s/article-one",
                    "local_path": str(article_path.relative_to(self.root)),
                    "source_file": "记忆承载.xlsx",
                    "source_article_no": "2",
                    "content_chars": "2",
                    "sha256": "test",
                }
            )

    def test_loads_both_accounts_and_ignores_non_wechat_links(self) -> None:
        stats = self.library.stats()

        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["accounts"], {"记忆承载": 1, "记忆承载3": 1})
        self.assertEqual(stats["years"], ["2026", "2025"])

    def test_filters_articles_by_account_year_and_keyword(self) -> None:
        result = self.library.list_articles(
            {
                "account": ["记忆承载3"],
                "year": ["2025"],
                "q": ["第二篇"],
                "status": ["all"],
            }
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "测试文章二")
        self.assertEqual(result["items"][0]["status"], "missing")

    def test_filters_articles_by_author(self) -> None:
        cache = (
            self.root
            / "data"
            / "authors"
            / "maobidao"
            / "sources"
            / "wechat_articles.csv"
        )
        with cache.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
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
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "author_id": "maobidao",
                    "author_name": "猫笔刀",
                    "account": "猫笔刀",
                    "account_username": "gh_maobidao",
                    "title": "猫笔刀测试文章",
                    "date": "2026-10-21",
                    "url": "https://mp.weixin.qq.com/s/maobidao-one",
                    "summary": "摘要",
                    "source_file": "本机微信",
                    "source_row": "1792530000",
                }
            )
        self.library.load_inventory()

        result = self.library.list_articles({"author_id": ["maobidao"]})
        stats = self.library.stats("maobidao")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["author_name"], "猫笔刀")
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["accounts"], {"猫笔刀": 1})

    def test_reports_downloaded_status_and_returns_only_archive_files(self) -> None:
        self._write_index()

        item_id = article_id("https://mp.weixin.qq.com/s/article-one")
        result = self.library.list_articles({"status": ["downloaded"]})
        local_file = self.library.local_file(item_id)

        self.assertEqual(result["total"], 1)
        self.assertIsNotNone(local_file)
        self.assertTrue(local_file.is_file())

    def test_rejects_empty_download_job(self) -> None:
        with self.assertRaisesRegex(ValueError, "没有可下载的文章"):
            self.library.submit_download(["unknown"])

    def test_paid_and_failed_statuses_are_exposed_and_not_resubmitted(self) -> None:
        with (
            self.root
            / "data"
            / "authors"
            / "jiyichengzai"
            / "index"
            / "download_status.csv"
        ).open(
            "w", encoding="utf-8-sig", newline=""
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
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
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "author_id": "jiyichengzai",
                    "author_name": "记忆承载",
                    "account": "记忆承载",
                    "account_username": "gh_jiyi",
                    "title": "测试文章一",
                    "date": "2026-10-20",
                    "url": "https://mp.weixin.qq.com/s/article-one",
                    "status": "paid",
                    "error": "付费文章",
                    "updated_at": "2026-10-22T12:00:00+08:00",
                }
            )

        paid = self.library.list_articles({"status": ["paid"]})
        with patch.object(self.library._executor, "submit"):
            job = self.library.submit_download(all_missing=True)

        self.assertEqual(paid["total"], 1)
        self.assertEqual(job["total"], 1)

    @patch("scripts.article_library_server.shutil.which", return_value="/opt/homebrew/bin/wx")
    @patch("scripts.article_library_server.subprocess.run")
    def test_syncs_target_articles_from_local_wechat(self, run, _which) -> None:
        payload = """
        {
          "articles": [
            {
              "account": "记忆承载",
              "account_username": "gh_jiyi",
              "title": "微信新增文章",
              "url": "https://mp.weixin.qq.com/s/new-from-wechat",
              "digest": "来自本机微信数据库",
              "timestamp": 1792454400
            },
            {
              "account": "其他公众号",
              "title": "不应收录",
              "url": "https://mp.weixin.qq.com/s/other",
              "timestamp": 1792454400
            }
          ],
          "meta": {"status": "ok"}
        }
        """
        empty_payload = "[]"

        def fake_run(command, **_kwargs):
            result = Mock(returncode=0, stderr="")
            result.stdout = payload if "gh_jiyi" in command else empty_payload
            return result

        run.side_effect = fake_run

        result = self.library.refresh_inventory(include_wechat=True)
        articles = self.library.list_articles({"q": ["微信新增文章"]})

        self.assertTrue(result["wechat"]["available"])
        self.assertEqual(result["wechat"]["synced"], 1)
        self.assertEqual(articles["total"], 1)
        self.assertEqual(articles["items"][0]["source_file"], "本机微信")
        self.assertTrue(
            (
                self.root
                / "data"
                / "authors"
                / "jiyichengzai"
                / "sources"
                / "wechat_articles.csv"
            ).is_file()
        )

    @patch("scripts.article_library_server.shutil.which", return_value="/opt/homebrew/bin/wx")
    @patch("scripts.article_library_server.subprocess.run")
    def test_wechat_permission_error_keeps_history_available(self, run, _which) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "错误: Operation not permitted (os error 1)"

        result = self.library.refresh_inventory(include_wechat=True)

        self.assertFalse(result["wechat"]["available"])
        self.assertIn("完全磁盘访问权限", result["wechat"]["warning"])
        self.assertEqual(result["total"], 2)

    def test_safe_name_removes_path_characters(self) -> None:
        self.assertEqual(safe_name('a/b:c*"d?'), "a_b_c_d_")

    def test_normalizes_wechat_url_for_downloading(self) -> None:
        self.assertEqual(
            normalize_wechat_url("http://mp.weixin.qq.com/s?x=1#rd"),
            "https://mp.weixin.qq.com/s?x=1",
        )

    def test_detects_paid_article_from_runtime_flag_or_visible_marker(self) -> None:
        self.assertTrue(is_paid_article("var is_pay_subscribe = '1' * 1;"))
        self.assertTrue(is_paid_article("<div>付费后阅读全文</div>"))
        self.assertFalse(is_paid_article("var is_pay_subscribe = '0' * 1;"))

    def test_detects_verified_paid_source_account(self) -> None:
        self.assertTrue(
            is_paid_source_url(
                "http://mp.weixin.qq.com/s?__biz=MzY5MDQxMDY1Ng%3D%3D&mid=2247483704"
            )
        )
        self.assertFalse(
            is_paid_source_url(
                "https://mp.weixin.qq.com/s?__biz=MzU0MjYwNDU2Mw%3D%3D&mid=1"
            )
        )

    @patch("scripts.article_library_server.requests.get")
    def test_stops_before_requesting_verified_paid_source(self, get) -> None:
        wrapper = Mock()
        wrapper.text = (
            '<div id="js_content"></div>'
            '<a id="js_share_source" '
            'data-url="http://mp.weixin.qq.com/s?__biz=MzY5MDQxMDY1Ng%3D%3D&amp;mid=1">'
            "阅读全文</a>"
        )
        wrapper.url = "https://mp.weixin.qq.com/s/article-two"
        wrapper.raise_for_status.return_value = None
        get.return_value = wrapper
        item = self.library.list_articles({"q": ["测试文章二"]})["items"][0]

        with self.assertRaises(PaidArticleError):
            self.library._download_one(self.library.get_article(item["id"]))

        self.assertEqual(get.call_count, 1)

    @patch("scripts.article_library_server.requests.get")
    def test_follows_http_read_more_link_as_https(self, get) -> None:
        wrapper = Mock()
        wrapper.text = (
            '<div id="js_content"></div>'
            '<a id="js_share_source" '
            'data-url="http://mp.weixin.qq.com/s/source-article#wechat_redirect"></a>'
        )
        wrapper.url = "https://mp.weixin.qq.com/s/article-two"
        wrapper.raise_for_status.return_value = None
        source = Mock()
        source.text = (
            '<h1 id="activity-name">测试文章二</h1>'
            '<em id="publish_time">2025-02-03</em>'
            '<div id="js_content">完整正文</div>'
        )
        source.url = "https://mp.weixin.qq.com/s/source-article"
        source.raise_for_status.return_value = None
        get.side_effect = [wrapper, source]

        item = self.library.list_articles({"q": ["测试文章二"]})["items"][0]
        path = self.library._download_one(self.library.get_article(item["id"]))

        self.assertTrue(path.is_file())
        self.assertIn("完整正文", path.read_text(encoding="utf-8"))
        self.assertEqual(get.call_args_list[1].args[0], "https://mp.weixin.qq.com/s/source-article")

    @patch("scripts.article_library_server.requests.get")
    def test_saves_maobidao_articles_as_markdown_and_html(self, get) -> None:
        cache = (
            self.root
            / "data"
            / "authors"
            / "maobidao"
            / "sources"
            / "wechat_articles.csv"
        )
        with cache.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
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
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "author_id": "maobidao",
                    "author_name": "猫笔刀",
                    "account": "猫笔刀",
                    "account_username": "gh_maobidao",
                    "title": "猫笔刀配图文章",
                    "date": "2026-10-21",
                    "url": "https://mp.weixin.qq.com/s/maobidao-image",
                    "summary": "",
                    "source_file": "本机微信",
                    "source_row": "1792592421",
                }
            )
        self.library.load_inventory()
        response = Mock()
        response.text = (
            '<h1 id="activity-name">猫笔刀配图文章</h1>'
            '<em id="publish_time">2026-10-21</em>'
            '<div id="js_content"><p>正文第一段</p>'
            '<img data-src="https://mmbiz.qpic.cn/example.jpg"></div>'
        )
        response.url = "https://mp.weixin.qq.com/s/maobidao-image"
        response.raise_for_status.return_value = None
        get.return_value = response

        item = self.library.list_articles({"q": ["猫笔刀配图文章"]})["items"][0]
        path = self.library._download_one(self.library.get_article(item["id"]))
        html_path = (
            self.root
            / "data"
            / "authors"
            / "maobidao"
            / "html"
            / "2026"
            / "2026-10-21_猫笔刀配图文章.html"
        )

        self.assertEqual(path.suffix, ".md")
        self.assertTrue(html_path.is_file())
        self.assertIn(
            "![图片](https://mmbiz.qpic.cn/example.jpg)",
            path.read_text(encoding="utf-8"),
        )
        self.assertIn(
            'src="https://mmbiz.qpic.cn/example.jpg"',
            html_path.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
