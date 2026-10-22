# 微信公众号文章库

本项目保存多个微信作者的文章清单、正文与本地索引。当前维护“记忆承载”和“猫笔刀”两位作者。

## 文章管理页

管理页通过 `config/authors.json` 管理作者与公众号映射，将各作者数据隔离保存，并通过本机微信 4.x 的公众号消息数据库持续发现新文章。页面提供作者、公众号、年份和归档状态筛选。

核心目录：

```text
config/authors.json
data/
  authors/
    jiyichengzai/
      articles/
      index/
      reports/
      sources/
    maobidao/
      articles/
      html/
      index/
      reports/
      sources/
  catalog/articles.csv
```

安装 Python 依赖：

```bash
python3 -m pip install openpyxl requests beautifulsoup4
```

安装 `wx-cli`。公开 npm 的 `0.3.0` 已停止维护，使用
[`jackwener/wx-cli-again`](https://github.com/jackwener/wx-cli-again) 源码构建：

```bash
brew install rust
git clone https://github.com/jackwener/wx-cli-again.git ~/.local/src/wx-cli-again
cd ~/.local/src/wx-cli-again
cargo test --locked
cargo build --release --locked
mkdir -p ~/.local/bin
cp target/release/wx ~/.local/bin/wx
wx --version
```

后续升级：

```bash
cd ~/.local/src/wx-cli-again
git pull
cargo test --locked
cargo build --release --locked
cp target/release/wx ~/.local/bin/wx
```

首次使用 `wx-cli` 时执行 `sudo wx init`。当前运行服务的终端或 IDE 需要在
macOS「系统设置 → 隐私与安全性 → 完全磁盘访问权限」中获准读取微信容器目录。

启动服务：

```bash
python3 scripts/article_library_server.py
```

浏览器访问：

```text
http://127.0.0.1:8765
```

服务启动时、运行期间每 30 分钟以及点击“同步目录”时，都会执行：

1. 按 `config/authors.json` 中的公众号 `gh_*` 标识读取本机微信推送。
2. 将增量分别写入 `data/authors/<author_id>/sources/wechat_articles.csv`。
3. 将各作者的微信增量与历史导入数据去重合并。
4. 生成全局汇总索引 `data/catalog/articles.csv`，供页面跨作者查询。

这种方式不需要公众号后台权限，数据直接来自你的本机微信。它只能同步微信已经接收并落入
本地数据库的推送；如果微信长期未登录、取消关注或本地记录已被清理，对应文章不会出现。

可通过环境变量调整：

```bash
ARTICLE_LIBRARY_PORT=8766 \
ARTICLE_LIBRARY_REFRESH_SECONDS=3600 \
python3 scripts/article_library_server.py
```

每个作者的下载结果独立保存：

- 正文：`data/authors/<author_id>/articles/<年份>/`
- HTML 快照：`data/authors/<author_id>/html/<年份>/`
- 索引：`data/authors/<author_id>/index/articles.csv`
- 状态：`data/authors/<author_id>/index/download_status.csv`
- 报告：`data/authors/<author_id>/reports/`

猫笔刀文章包含较多配图，下载时采用双存储：`articles/<年份>/` 保存 Markdown
正文，`html/<年份>/` 保存带原始图文结构的 HTML 快照。记忆承载历史文章仍沿用
TXT 正文，避免大规模改写既有语料。

下载状态包括：

- `paid`：微信页面明确标识为付费内容，或外层引导页指向已验证的付费源文章；只在页面标记，不下载正文。
- `failed`：文章已删除、页面失效、触发微信验证或正文不可公开访问。

页面可按“已归档 / 待下载 / 付费 / 失败”筛选。批量下载只处理“待下载”，不会反复请求
已经标记为付费或失败的文章。

当微信数据读取权限不足时，服务仍会使用各作者已缓存的 `sources/wechat_articles.csv` 正常展示历史文章，并在页面顶部提示“微信待授权”。

当前可获取范围：

- 记忆承载：4,601 条目录记录。
- 猫笔刀：仅保留 2026-10-07 及之后的 15 条目录记录；其中 13 篇已归档为 Markdown 和 HTML，2 篇已隐藏或不可公开查看。

## 测试

```bash
python3 -m unittest scripts/test_article_library_server.py -v
```
