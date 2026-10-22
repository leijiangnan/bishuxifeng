package service

import (
	"article-analysis/pkg/logger"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/PuerkitoBio/goquery"
	"github.com/stretchr/testify/assert"
)

func parseHTML(html string) (*goquery.Document, error) {
	return goquery.NewDocumentFromReader(strings.NewReader(html))
}

func TestCrawlerService_ExtractTitle(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	tests := []struct {
		name     string
		html     string
		expected string
	}{
		{
			name:     "extract from h1",
			html:     `<html><body><h1>Test Article Title</h1></body></html>`,
			expected: "Test Article Title",
		},
		{
			name:     "extract from og:title",
			html:     `<html><head><meta property="og:title" content="OG Title"></head><body></body></html>`,
			expected: "OG Title",
		},
		{
			name:     "extract from title tag",
			html:     `<html><head><title>Page Title</title></head><body></body></html>`,
			expected: "Page Title",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			doc, err := parseHTML(tt.html)
			assert.NoError(t, err)
			title := svc.extractTitle(doc)
			assert.Equal(t, tt.expected, title)
		})
	}
}

func TestCrawlerService_CrawlWeChatAndSaveToFile(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)
	svc.SetMaxArticles(2000)

	defer svc.StopChrome()

	svc.SetMaxConcurrency(5)

	wechatURL := "https://mp.weixin.qq.com/s/Aj6q6kibtYmldE9nDWsaTA"

	outputPath := "/Users/bytedance/goproject/my/article_analysis/crawled_articles_6.txt"
	file, err := os.Create(outputPath)
	if err != nil {
		t.Fatalf("创建文件失败: %v", err)
	}
	defer file.Close()

	var mu sync.Mutex
	articleIndex := 0

	callback := func(article *CrawledArticle, index int) {
		mu.Lock()
		defer mu.Unlock()

		articleIndex++
		content := fmt.Sprintf("=== 文章 %d ===\n标题: %s\n作者: %s\n日期: %s\nURL: %s\n\n%s\n\n",
			articleIndex, article.Title, article.Author, article.Date, article.URL, article.Content)

		_, err := file.WriteString(content)
		if err != nil {
			t.Logf("写入文章 %d 失败: %v", articleIndex, err)
		}
		file.Sync()
		t.Logf("已写入第 %d 篇文章: %s", articleIndex, article.Title)
	}

	result, err := svc.CrawlArticlesWithCallback(wechatURL, 801, 870, callback)

	if err != nil {
		t.Fatalf("爬取出错: %v", err)
	}

	t.Logf("找到链接数: %d", result.TotalFound)
	t.Logf("爬取数量: %d", result.CrawledCount)
	t.Logf("已保存 %d 篇文章到: %s", articleIndex, outputPath)
}

func TestCrawlerService_ExtractAuthor(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	tests := []struct {
		name     string
		html     string
		expected string
	}{
		{
			name:     "extract from meta author",
			html:     `<html><head><meta name="author" content="John Doe"></head><body></body></html>`,
			expected: "John Doe",
		},
		{
			name:     "extract from class author",
			html:     `<html><body><span class="author">Jane Smith</span></body></html>`,
			expected: "Jane Smith",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			doc, err := parseHTML(tt.html)
			assert.NoError(t, err)
			author := svc.extractAuthor(doc)
			assert.Equal(t, tt.expected, author)
		})
	}
}

func TestCrawlerService_ExtractContent(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	html := `<html><body>
		<article>
			<p>This is paragraph 1.</p>
			<p>This is paragraph 2.</p>
			<p><a href="http://example.com">Link text</a></p>
			<script>console.log('should be removed')</script>
		</article>
	</body></html>`

	doc, err := parseHTML(html)
	assert.NoError(t, err)

	content := svc.extractContent(doc)
	assert.Contains(t, content, "paragraph 1")
	assert.Contains(t, content, "paragraph 2")
	assert.Contains(t, content, "Link text")
	assert.NotContains(t, content, "console.log")
}

func TestCrawlerService_ExtractArticleLinks(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`<html><body>
			<a href="/article/1">Article 1</a>
			<a href="/article/2">Article 2</a>
			<a href="https://example.com/article/3">Article 3</a>
			<a href="http://other.com/article/4">External Link</a>
			<a href="#">Skip</a>
			<a href="javascript:void(0)">Skip JS</a>
		</body></html>`))
	}))
	defer server.Close()

	doc, err := svc.fetchPage(server.URL)
	assert.NoError(t, err)

	links := svc.extractArticleLinks(doc, server.URL)

	assert.GreaterOrEqual(t, len(links), 2)
	for _, link := range links {
		assert.True(t, strings.HasPrefix(link, server.URL) || strings.HasPrefix(link, "https://example.com"))
	}
}

func TestCrawlerService_CrawlArticles_InvalidURL(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	_, err := svc.CrawlArticles("invalid-url", 1, 2)
	assert.Error(t, err)
}

func TestCrawlerService_CrawlArticles_InvalidCount(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	_, err := svc.CrawlArticles("http://example.com", 0, 1)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "起始位置必须大于等于1")
}

func TestCrawlerService_MaxArticlesLimit(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)
	svc.SetMaxArticles(2)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusOK)
		if r.URL.Path == "/" {
			w.Write([]byte(`<html><body>
				<a href="/article/1">Article 1</a>
				<a href="/article/2">Article 2</a>
				<a href="/article/3">Article 3</a>
			</body></html>`))
		} else {
			w.Write([]byte(`<html><body>
				<h1>Test Article</h1>
				<div class="content"><p>Test content</p></div>
			</body></html>`))
		}
	}))
	defer server.Close()

	result, err := svc.CrawlArticles(server.URL, 1, 10)
	assert.NoError(t, err)
	assert.LessOrEqual(t, result.CrawledCount, 2)
}

func TestCrawlerService_FetchPage(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assert.Contains(t, r.Header.Get("User-Agent"), "Mozilla")
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`<html><body><h1>Test</h1></body></html>`))
	}))
	defer server.Close()

	doc, err := svc.fetchPage(server.URL)
	assert.NoError(t, err)
	assert.NotNil(t, doc)
	assert.Equal(t, "Test", strings.TrimSpace(doc.Find("h1").Text()))
}

func TestCrawlerService_FetchPage_Error(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	_, err := svc.fetchPage("http://localhost:99999/nonexistent")
	assert.Error(t, err)
}

func TestCrawlerService_CrawlWeChatArticle(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)
	svc.SetMaxArticles(10)

	wechatURL := "https://mp.weixin.qq.com/s/Aj6q6kibtYmldE9nDWsaTA"

	result, err := svc.CrawlArticles(wechatURL, 1, 1)

	if err != nil {
		t.Logf("爬取出错: %v", err)
		return
	}

	t.Logf("找到链接数: %d", result.TotalFound)
	t.Logf("爬取数量: %d", result.CrawledCount)

	for i, article := range result.Articles {
		t.Logf("=== 文章 %d ===", i+1)
		t.Logf("标题: %s", article.Title)
		t.Logf("作者: %s", article.Author)
		t.Logf("日期: %s", article.Date)
		t.Logf("URL: %s", article.URL)
		t.Logf("内容长度: %d 字符", len(article.Content))
		t.Logf("===== 正文开始 =====")
		t.Log(article.Content)
		t.Logf("===== 正文结束 =====")
	}
}

func TestCrawlerService_FetchWeChatPage(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)

	// 测试结束后关闭浏览器
	defer svc.StopChrome()

	wechatURL := "https://mp.weixin.qq.com/s?__biz=MzU3NDc5Nzc0NQ==&mid=2247530369&idx=1&sn=7f6c06582003e1834f03d1c53a53ab5c&scene=21#wechat_redirect"

	t.Logf("开始获取页面: %s", wechatURL)

	article, err := svc.crawlArticle(wechatURL, "")
	if err != nil {
		t.Fatalf("解析文章失败: %v", err)
	}

	t.Logf("=== 文章信息 ===")
	t.Logf("标题: %s", article.Title)
	t.Logf("作者: %s", article.Author)
	t.Logf("日期: %s", article.Date)
	t.Logf("URL: %s", article.URL)
	t.Logf("内容长度: %d 字符", len(article.Content))

	if len(article.Content) > 0 {
		t.Logf("===== 正文前500字符 =====")
		t.Log(article.Content[:min(500, len(article.Content))])
		t.Logf("===== 正文结束 =====")
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func TestCrawlerService_CrawlArticles_Range(t *testing.T) {
	log := logger.NewLogger("info")
	svc := NewCrawlerService(nil, log)
	svc.SetMaxArticles(100) // Ensure max articles is high enough

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusOK)
		if r.URL.Path == "/" {
			w.Write([]byte(`<html><body>
				<a href="/article/1">Article 1</a>
				<a href="/article/2">Article 2</a>
				<a href="/article/3">Article 3</a>
				<a href="/article/4">Article 4</a>
				<a href="/article/5">Article 5</a>
			</body></html>`))
		} else {
			w.Write([]byte(`<html><body>
				<h1>Article</h1>
				<div class="content"><p>Content</p></div>
			</body></html>`))
		}
	}))
	defer server.Close()

	// Test case 1: Crawl 2 to 4 (Total 3 articles)
	result, err := svc.CrawlArticles(server.URL, 2, 4)
	assert.NoError(t, err)
	assert.Equal(t, 5, result.TotalFound)
	assert.Equal(t, 3, result.CrawledCount)
	assert.Equal(t, 3, len(result.Articles))

	// Verify we got Article 2, 3, 4
	titles := make(map[string]bool)
	urls := make(map[string]bool)
	for _, a := range result.Articles {
		titles[a.Title] = true
		urls[a.URL] = true
	}
	assert.True(t, urls[server.URL+"/article/2"])
	assert.True(t, urls[server.URL+"/article/3"])
	assert.True(t, urls[server.URL+"/article/4"])
	assert.False(t, urls[server.URL+"/article/1"])
	assert.False(t, urls[server.URL+"/article/5"])

	// Test case 2: Start out of range
	result, err = svc.CrawlArticles(server.URL, 6, 10)
	assert.NoError(t, err)
	assert.Equal(t, 5, result.TotalFound)
	assert.Equal(t, 0, result.CrawledCount)

	// Test case 3: End out of range
	result, err = svc.CrawlArticles(server.URL, 4, 10)
	assert.NoError(t, err)
	assert.Equal(t, 5, result.TotalFound)
	assert.Equal(t, 2, result.CrawledCount) // 4 and 5

	// Verify we got Article 4, 5
	urls = make(map[string]bool)
	for _, a := range result.Articles {
		urls[a.URL] = true
	}
	assert.True(t, urls[server.URL+"/article/4"])
	assert.True(t, urls[server.URL+"/article/5"])
}
