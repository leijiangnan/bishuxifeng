# 记忆承载文章库

本项目保存“记忆承载”和“记忆承载3”两个公众号的文章清单、正文与本地索引。

## 文章管理页

管理页将两份 Excel 作为历史基线，并通过本机微信 4.x 的公众号消息数据库持续发现新文章。
它提供文章检索、原文跳转、归档状态筛选和本地下载。

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

1. 通过 `wx biz-articles` 读取本机微信已经收到的公众号文章推送。
2. 仅保留“记忆承载”和“记忆承载3”，并写入 `data/wechat_articles.csv`。
3. 将微信增量与历史 Excel 去重合并后展示在页面中。

这种方式不需要公众号后台权限，数据直接来自你的本机微信。它只能同步微信已经接收并落入
本地数据库的推送；如果微信长期未登录、取消关注或本地记录已被清理，对应文章不会出现。

可通过环境变量调整：

```bash
ARTICLE_LIBRARY_PORT=8766 \
ARTICLE_LIBRARY_REFRESH_SECONDS=3600 \
python3 scripts/article_library_server.py
```

下载成功后会同步更新：

- `data/articles/记忆承载/<年份>/`
- `data/articles_index.csv`
- `data/download_report_<年份>.csv`

下载失败会写入 `data/download_failed_<年份>.csv`，不会静默忽略。

批量下载状态会写入 `data/article_download_status.csv`：

- `paid`：微信页面明确标识为付费内容，或外层引导页指向已验证的付费源文章；只在页面标记，不下载正文。
- `failed`：文章已删除、页面失效、触发微信验证或正文不可公开访问。

页面可按“已归档 / 待下载 / 付费 / 失败”筛选。批量下载只处理“待下载”，不会反复请求
已经标记为付费或失败的文章。

当微信数据读取权限不足时，服务仍会使用 Excel 和已缓存的
`data/wechat_articles.csv` 正常展示历史文章，并在页面顶部提示“微信待授权”。

## 测试

```bash
python3 -m unittest scripts/test_article_library_server.py -v
```
