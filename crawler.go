package service

import (
	"article-analysis/internal/model"
	"article-analysis/internal/repository"
	"article-analysis/pkg/logger"
	"context"
	"errors"
	"math/rand"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/PuerkitoBio/goquery"
	"github.com/chromedp/chromedp"
	"go.uber.org/zap"
)

type CrawlerService struct {
	articleRepo     *repository.ArticleRepository
	log             *logger.Logger
	client          *http.Client
	maxArticles     int
	maxConcurrency  int
	allocatorCtx    context.Context
	allocatorCancel context.CancelFunc
	chromeMu        sync.Mutex
}

func NewCrawlerService(repo *repository.ArticleRepository, log *logger.Logger) *CrawlerService {
	return &CrawlerService{
		articleRepo: repo,
		log:         log,
		client: &http.Client{
			Timeout: 30 * time.Second,
		},
		maxArticles:    2,
		maxConcurrency: 5,
	}
}

type CrawledArticle struct {
	Title   string
	Author  string
	Date    string
	Content string
	URL     string
}

type CrawlResult struct {
	TotalFound   int
	CrawledCount int
	Articles     []CrawledArticle
	Errors       []string
}

func (s *CrawlerService) CrawlArticles(startURL string, start, end int) (*CrawlResult, error) {
	return s.CrawlArticlesWithCallback(startURL, start, end, nil)
}

type ArticleCallback func(article *CrawledArticle, index int)

func (s *CrawlerService) CrawlArticlesWithCallback(startURL string, start, end int, callback ArticleCallback) (*CrawlResult, error) {
	if start < 1 {
		return nil, errors.New("起始位置必须大于等于1")
	}
	if end < start {
		return nil, errors.New("结束位置必须大于等于起始位置")
	}

	requestedCount := end - start + 1
	if requestedCount > s.maxArticles {
		s.log.Info("限制爬取数量", zap.Int("requested", requestedCount), zap.Int("max", s.maxArticles))
		requestedCount = s.maxArticles
		end = start + requestedCount - 1
	}

	parsedURL, err := url.Parse(startURL)
	if err != nil {
		return nil, errors.New("无效的URL")
	}
	baseURL := parsedURL.Scheme + "://" + parsedURL.Host

	s.log.Info("开始爬取文章",
		zap.String("url", startURL),
		zap.Int("start", start),
		zap.Int("end", end))

	doc, err := s.fetchPage(startURL)
	if err != nil {
		return nil, err
	}

	links := s.extractArticleLinksWithDates(doc, baseURL)
	totalFound := len(links)
	s.log.Info("找到文章链接", zap.Int("count", totalFound))

	// Adjust start/end based on actual links found
	if start > totalFound {
		return &CrawlResult{
			TotalFound:   totalFound,
			CrawledCount: 0,
			Articles:     make([]CrawledArticle, 0),
			Errors:       make([]string, 0),
		}, nil
	}

	if end > totalFound {
		end = totalFound
	}

	// Calculate slice indices (0-based)
	startIndex := start - 1
	endIndex := end
	linksToCrawl := links[startIndex:endIndex]
	targetCount := len(linksToCrawl)

	result := &CrawlResult{
		TotalFound: totalFound,
		Articles:   make([]CrawledArticle, 0),
		Errors:     make([]string, 0),
	}

	// 限制并发数
	concurrency := s.maxConcurrency
	if concurrency <= 0 {
		concurrency = 1
	}
	if concurrency > targetCount {
		concurrency = targetCount
	}

	sem := make(chan struct{}, concurrency)
	var wg sync.WaitGroup
	var mu sync.Mutex

	for i, linkInfo := range linksToCrawl {
		mu.Lock()
		if len(result.Articles) >= targetCount {
			mu.Unlock()
			break
		}
		mu.Unlock()

		sem <- struct{}{}
		wg.Add(1)

		// Calculate global index (1-based)
		globalIndex := start + i

		go func(index int, info ArticleLinkInfo) {
			defer wg.Done()
			defer func() { <-sem }()

			mu.Lock()
			if len(result.Articles) >= targetCount {
				mu.Unlock()
				return
			}
			mu.Unlock()

			s.log.Info("正在爬取文章", zap.Int("index", index), zap.String("url", info.URL))

			article, err := s.crawlArticle(info.URL, info.Title)

			mu.Lock()
			defer mu.Unlock()

			if len(result.Articles) >= targetCount {
				return
			}

			if err != nil {
				s.log.Warn("爬取文章失败", zap.String("url", info.URL), zap.Error(err))
				result.Errors = append(result.Errors, info.URL+": "+err.Error())
				return
			}

			if article.Date == "" && info.Date != "" {
				article.Date = info.Date
			}

			result.Articles = append(result.Articles, *article)

			if callback != nil {
				callback(article, len(result.Articles))
			}

			s.log.Info("文章爬取成功", zap.String("title", article.Title))
		}(globalIndex, linkInfo)
	}

	wg.Wait()

	result.CrawledCount = len(result.Articles)
	return result, nil
}

func (s *CrawlerService) SaveCrawledArticles(articles []CrawledArticle) ([]model.Article, []error) {
	savedArticles := make([]model.Article, 0)
	errs := make([]error, 0)

	for _, article := range articles {
		title := strings.TrimSpace(article.Title)
		if title == "" {
			title = "无标题"
		}

		exists, err := s.articleRepo.ExistsByTitle(title)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		if exists {
			s.log.Warn("文章已存在，跳过", zap.String("title", title))
			continue
		}

		author := strings.TrimSpace(article.Author)
		if author == "" {
			author = "未知作者"
		}

		content := strings.TrimSpace(article.Content)
		if content == "" {
			s.log.Warn("文章内容为空，跳过", zap.String("title", title))
			continue
		}

		dbArticle := &model.Article{
			Title:    title,
			Author:   author,
			Content:  content,
			FilePath: "crawled:" + article.URL,
			FileSize: int64(len(content)),
		}

		if err := s.articleRepo.Create(dbArticle); err != nil {
			errs = append(errs, err)
			continue
		}

		savedArticles = append(savedArticles, *dbArticle)
		s.log.Info("文章保存成功", zap.String("title", title), zap.Uint64("id", dbArticle.ID))
	}

	return savedArticles, errs
}

func (s *CrawlerService) fetchPage(urlStr string) (*goquery.Document, error) {
	// 对于微信 URL，使用无头浏览器获取
	if strings.Contains(urlStr, "mp.weixin.qq.com") {
		s.log.Info("检测到微信 URL，使用无头浏览器获取", zap.String("url", urlStr))
		return s.fetchPageWithChrome(urlStr)
	}

	// 普通 URL 使用 HTTP 客户端
	req, err := http.NewRequest("GET", urlStr, nil)
	if err != nil {
		return nil, err
	}

	req.Header.Set("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.7,application/signed-exchange;v=b3;q=0.7")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7")
	req.Header.Set("Priority", "u=0, i")
	req.Header.Set("Sec-Ch-Ua", `"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"`)
	req.Header.Set("Sec-Ch-Ua-Mobile", "?0")
	req.Header.Set("Sec-Ch-Ua-Platform", `"macOS"`)
	req.Header.Set("Sec-Fetch-Dest", "document")
	req.Header.Set("Sec-Fetch-Mode", "navigate")
	req.Header.Set("Sec-Fetch-Site", "same-origin")
	req.Header.Set("Upgrade-Insecure-Requests", "1")

	resp, err := s.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("HTTP请求失败: " + resp.Status)
	}

	return goquery.NewDocumentFromReader(resp.Body)
}

func (s *CrawlerService) extractArticleLinks(doc *goquery.Document, baseURL string) []string {
	links := make([]string, 0)
	seen := make(map[string]bool)

	doc.Find("a[href]").Each(func(i int, sel *goquery.Selection) {
		href, exists := sel.Attr("href")
		if !exists || href == "" {
			return
		}

		href = strings.TrimSpace(href)
		if href == "#" || href == "javascript:void(0)" {
			return
		}

		if strings.HasPrefix(href, "/") {
			href = baseURL + href
		} else if !strings.HasPrefix(href, "http://") && !strings.HasPrefix(href, "https://") {
			return
		}

		parsedHref, err := url.Parse(href)
		if err != nil {
			return
		}

		parsedBase, _ := url.Parse(baseURL)
		if parsedHref.Host != parsedBase.Host {
			return
		}

		ext := strings.ToLower(href)
		if strings.HasSuffix(ext, ".jpg") || strings.HasSuffix(ext, ".png") ||
			strings.HasSuffix(ext, ".gif") || strings.HasSuffix(ext, ".pdf") ||
			strings.HasSuffix(ext, ".zip") || strings.HasSuffix(ext, ".doc") ||
			strings.HasSuffix(ext, ".docx") {
			return
		}

		if !seen[href] {
			seen[href] = true
			links = append(links, href)
		}
	})

	return links
}

type ArticleLinkInfo struct {
	URL   string
	Date  string
	Title string
}

func (s *CrawlerService) extractArticleLinksWithDates(doc *goquery.Document, baseURL string) []ArticleLinkInfo {
	links := make([]ArticleLinkInfo, 0)
	seen := make(map[string]bool)

	// 获取整个页面的文本，用于提取日期
	// 微信文章列表页的格式：序号 标题作者 日期
	fullText := doc.Find("#js_content").Text()
	if fullText == "" {
		fullText = doc.Text()
	}

	doc.Find("a[href]").Each(func(i int, sel *goquery.Selection) {
		href, exists := sel.Attr("href")
		if !exists || href == "" {
			return
		}

		href = strings.TrimSpace(href)
		if href == "#" || href == "javascript:void(0)" {
			return
		}

		if strings.HasPrefix(href, "/") {
			href = baseURL + href
		} else if !strings.HasPrefix(href, "http://") && !strings.HasPrefix(href, "https://") {
			return
		}

		parsedHref, err := url.Parse(href)
		if err != nil {
			return
		}

		parsedBase, _ := url.Parse(baseURL)
		if parsedHref.Host != parsedBase.Host {
			return
		}

		ext := strings.ToLower(href)
		if strings.HasSuffix(ext, ".jpg") || strings.HasSuffix(ext, ".png") ||
			strings.HasSuffix(ext, ".gif") || strings.HasSuffix(ext, ".pdf") ||
			strings.HasSuffix(ext, ".zip") || strings.HasSuffix(ext, ".doc") ||
			strings.HasSuffix(ext, ".docx") {
			return
		}

		if !seen[href] {
			seen[href] = true
			title := strings.TrimSpace(sel.Text())
			date := s.extractDateForTitle(fullText, title)
			links = append(links, ArticleLinkInfo{
				URL:   href,
				Date:  date,
				Title: title,
			})
		}
	})

	return links
}

// extractDateForTitle 从页面文本中提取指定标题对应的日期
func (s *CrawlerService) extractDateForTitle(fullText, title string) string {
	if title == "" || fullText == "" {
		return ""
	}

	// 在文本中查找标题位置
	idx := strings.Index(fullText, title)
	if idx == -1 {
		return ""
	}

	// 从标题位置开始，向后查找100个字符内的日期
	endIdx := idx + len(title) + 100
	if endIdx > len(fullText) {
		endIdx = len(fullText)
	}
	subText := fullText[idx:endIdx]

	// 提取日期
	dateMatch := regexp.MustCompile(`(\d{4}-\d{1,2}-\d{1,2})`).FindString(subText)
	return dateMatch
}

func (s *CrawlerService) isCaptchaPage(doc *goquery.Document) bool {
	html, _ := doc.Html()
	text := doc.Text()
	return strings.Contains(text, "验证") || strings.Contains(text, "验证码") ||
		strings.Contains(text, "环境异常") || strings.Contains(text, "尝试太多了") ||
		len(strings.TrimSpace(text)) < 100 || !strings.Contains(html, "js_content")
}

func (s *CrawlerService) ensureChksmParam(urlStr string) string {
	parsedURL, err := url.Parse(urlStr)
	if err != nil {
		return urlStr
	}

	rawQuery := parsedURL.RawQuery

	if strings.Contains(rawQuery, "chksm=") {
		return urlStr
	}

	if !strings.Contains(rawQuery, "sn=") {
		return urlStr
	}

	chksm := generateRandomChksm()

	snIndex := strings.Index(rawQuery, "sn=")
	afterSn := rawQuery[snIndex+3:]
	nextAmp := strings.Index(afterSn, "&")

	var snValue string
	if nextAmp == -1 {
		snValue = afterSn
	} else {
		snValue = afterSn[:nextAmp]
	}

	insertPos := snIndex + 3 + len(snValue)
	newRawQuery := rawQuery[:insertPos] + "&chksm=" + chksm + rawQuery[insertPos:]

	parsedURL.RawQuery = newRawQuery
	return parsedURL.String()
}

func generateRandomChksm() string {
	const hexChars = "0123456789abcdef"
	result := make([]byte, 48)
	for i := range result {
		result[i] = hexChars[rand.Intn(len(hexChars))]
	}
	return string(result)
}

func (s *CrawlerService) crawlArticle(urlStr string, preExtractedTitle string) (*CrawledArticle, error) {
	urlStr = s.ensureChksmParam(urlStr)

	// fetchPage 会自动判断：微信 URL 使用无头浏览器，其他 URL 使用 HTTP 客户端
	doc, err := s.fetchPage(urlStr)
	if err != nil {
		return nil, err
	}

	article := &CrawledArticle{
		URL: urlStr,
	}

	article.Title = s.extractTitle(doc)
	article.Author = s.extractAuthor(doc)
	article.Date = s.extractDate(doc)
	article.Content = s.extractContent(doc)

	// 如果内容为空，可能是验证码页面，记录警告
	if article.Content == "" || len(article.Content) < 100 {
		s.log.Warn("文章内容异常", zap.String("url", urlStr), zap.Int("contentLength", len(article.Content)))
	}

	return article, nil
}

func (s *CrawlerService) extractTitle(doc *goquery.Document) string {
	selectors := []string{
		"#activity-name",
		".rich_media_title",
		"h1",
		"meta[property='og:title']",
		"meta[name='title']",
		"title",
	}

	for _, sel := range selectors {
		if sel == "h1" || sel == "#activity-name" || sel == ".rich_media_title" {
			text := strings.TrimSpace(doc.Find(sel).First().Text())
			if text != "" && len(text) < 200 {
				return text
			}
		} else if strings.HasPrefix(sel, "meta") {
			if content, exists := doc.Find(sel).Attr("content"); exists && content != "" {
				return strings.TrimSpace(content)
			}
		} else {
			text := strings.TrimSpace(doc.Find(sel).Text())
			if text != "" {
				if len(text) > 200 {
					text = text[:200]
				}
				return text
			}
		}
	}

	return ""
}

func (s *CrawlerService) extractAuthor(doc *goquery.Document) string {
	selectors := []string{
		"#js_name",
		".rich_media_meta_nickname",
		"meta[name='author']",
		"meta[property='article:author']",
		"[class*='author']",
		"[class*='Author']",
		"[class*='writer']",
		"[class*='source']",
	}

	for _, sel := range selectors {
		if strings.HasPrefix(sel, "meta") {
			if content, exists := doc.Find(sel).Attr("content"); exists && content != "" {
				return strings.TrimSpace(content)
			}
		} else {
			text := strings.TrimSpace(doc.Find(sel).First().Text())
			if text != "" && len(text) < 100 {
				text = regexp.MustCompile(`作者[：:]\s*`).ReplaceAllString(text, "")
				text = regexp.MustCompile(`\s+`).ReplaceAllString(text, " ")
				return strings.TrimSpace(text)
			}
		}
	}

	return ""
}

func (s *CrawlerService) extractDate(doc *goquery.Document) string {
	dateSelectors := []string{
		"#publish_time",
		"meta[property='article:published_time']",
		"meta[name='publishdate']",
		"meta[name='date']",
		"time",
		"[class*='date']",
		"[class*='time']",
	}

	for _, sel := range dateSelectors {
		if sel == "#publish_time" {
			text := strings.TrimSpace(doc.Find(sel).Text())
			if text != "" && len(text) < 50 {
				text = strings.ReplaceAll(text, "年", "-")
				text = strings.ReplaceAll(text, "月", "-")
				text = strings.ReplaceAll(text, "日", "")
				text = regexp.MustCompile(`\s*-\s*`).ReplaceAllString(text, "-")
				text = regexp.MustCompile(`^[^0-9]*([0-9-]+).*`).ReplaceAllString(text, "$1")
				if regexp.MustCompile(`^\d{4}-\d{1,2}-\d{1,2}$`).MatchString(text) {
					return text
				}
				return strings.TrimSpace(text)
			}
		} else if strings.HasPrefix(sel, "meta") {
			if content, exists := doc.Find(sel).Attr("content"); exists && content != "" {
				dateStr := regexp.MustCompile(`^(\d{4}-\d{2}-\d{2})`).FindString(content)
				if dateStr != "" {
					return dateStr
				}
				return strings.TrimSpace(content)
			}
		} else {
			text := strings.TrimSpace(doc.Find(sel).First().Text())
			if text != "" && len(text) < 50 && !strings.Contains(text, "作者") {
				return text
			}
		}
	}

	// 尝试从文章内容开头匹配日期（微信文章日期通常是动态加载的）
	content := doc.Find("#js_content").Text()
	if content == "" {
		content = doc.Find("body").Text()
	}
	// 从内容前500字符中匹配日期
	if len(content) > 500 {
		content = content[:500]
	}
	datePatterns := []string{
		`(\d{4})年(\d{1,2})月(\d{1,2})日`,
		`(\d{4})-(\d{1,2})-(\d{1,2})`,
		`(\d{4})/(\d{1,2})/(\d{1,2})`,
	}
	for _, pattern := range datePatterns {
		matches := regexp.MustCompile(pattern).FindStringSubmatch(content)
		if len(matches) == 4 {
			year := matches[1]
			month := matches[2]
			day := matches[3]
			if len(month) == 1 {
				month = "0" + month
			}
			if len(day) == 1 {
				day = "0" + day
			}
			return year + "-" + month + "-" + day
		}
	}

	return ""
}

func (s *CrawlerService) extractContent(doc *goquery.Document) string {
	contentSelectors := []string{
		"#js_content",
		".rich_media_content",
		".js_name",
		"article",
		"[class*='content']",
		"[class*='article-body']",
		"[class*='post-content']",
		"[class*='entry-content']",
		"[class*='main-content']",
		".article",
		"#article",
	}

	var contentNode *goquery.Selection

	for _, sel := range contentSelectors {
		node := doc.Find(sel).First()
		if node.Length() > 0 {
			contentNode = node
			break
		}
	}

	if contentNode == nil {
		contentNode = doc.Find("body")
	}

	contentNode.Find("script, style, nav, header, footer, aside, .sidebar, .comment, .advertisement, .ad").Remove()

	var paragraphs []string
	contentNode.Find("p, br, div, section").Each(func(i int, sel *goquery.Selection) {
		sel.Find("a").Each(func(j int, aSel *goquery.Selection) {
			text := aSel.Text()
			aSel.ReplaceWithHtml(text)
		})

		text := sel.Text()
		text = strings.TrimSpace(text)
		if text != "" {
			paragraphs = append(paragraphs, text)
		}
	})

	if len(paragraphs) == 0 {
		text := contentNode.Text()
		text = regexp.MustCompile(`\s+`).ReplaceAllString(text, " ")
		return strings.TrimSpace(text)
	}

	var result []string
	for _, p := range paragraphs {
		p = regexp.MustCompile(`\s+`).ReplaceAllString(p, " ")
		p = strings.TrimSpace(p)
		if p != "" {
			result = append(result, p)
		}
	}

	return strings.Join(result, "\n")
}

func (s *CrawlerService) SetMaxArticles(max int) {
	if max > 0 {
		s.maxArticles = max
	}
}

func (s *CrawlerService) SetMaxConcurrency(max int) {
	if max > 0 {
		s.maxConcurrency = max
	}
}

// StartChrome 启动无头浏览器
func (s *CrawlerService) StartChrome() error {
	s.chromeMu.Lock()
	defer s.chromeMu.Unlock()

	if s.allocatorCtx != nil {
		return nil
	}

	// 使用独立的用户数据目录，避免冲突
	// userDataDir := "/tmp/chromedp-user-data-dir"

	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		// chromedp.UserDataDir(userDataDir),
		chromedp.Flag("headless", true),
		chromedp.Flag("disable-gpu", true),
		chromedp.Flag("no-sandbox", true),
		chromedp.Flag("disable-dev-shm-usage", true),
		// 反检测选项
		chromedp.Flag("enable-automation", false),
		chromedp.Flag("disable-blink-features", "AutomationControlled"),
		chromedp.WindowSize(1920, 1080),
		chromedp.UserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
	)

	allocCtx, allocCancel := chromedp.NewExecAllocator(context.Background(), opts...)

	// 创建一个浏览器实例
	ctx, cancel := chromedp.NewContext(allocCtx)

	// 确保浏览器启动
	if err := chromedp.Run(ctx, chromedp.Evaluate("1", nil)); err != nil {
		cancel()
		allocCancel()
		return err
	}

	s.allocatorCtx = ctx
	s.allocatorCancel = func() {
		cancel()
		allocCancel()
	}

	s.log.Info("无头浏览器已启动")
	return nil
}

// StopChrome 关闭无头浏览器
func (s *CrawlerService) StopChrome() {
	s.chromeMu.Lock()
	defer s.chromeMu.Unlock()

	if s.allocatorCancel != nil {
		s.allocatorCancel()
		s.allocatorCtx = nil
		s.allocatorCancel = nil
		s.log.Info("无头浏览器已关闭")
	}
}

// fetchPageWithChrome 使用无头浏览器获取页面（用于微信文章）
func (s *CrawlerService) fetchPageWithChrome(urlStr string) (*goquery.Document, error) {
	if err := s.StartChrome(); err != nil {
		return nil, err
	}

	// 为每个请求创建一个新的浏览器 tab 上下文，避免并发冲突
	ctx, cancel := chromedp.NewContext(s.allocatorCtx)
	defer cancel()

	var html string
	var err error

	// 使用更短的超时，避免测试超时
	timeoutCtx, timeoutCancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer timeoutCancel()

	// 使用 goroutine 来实现更可靠的超时控制
	done := make(chan error, 1)
	go func() {
		// 随机延迟 2-5 秒
		delay := time.Duration(2000+rand.Intn(3000)) * time.Millisecond
		s.log.Info("等待随机延迟", zap.String("url", urlStr), zap.Duration("delay", delay))
		time.Sleep(delay)

		done <- chromedp.Run(ctx,
			chromedp.Navigate(urlStr),
			// 等待页面加载完成，最多等 3 秒
			chromedp.Sleep(3*time.Second),
			// 获取页面 HTML
			chromedp.OuterHTML("html", &html, chromedp.ByQuery),
		)
	}()

	select {
	case err = <-done:
		if err != nil {
			s.log.Warn("无头浏览器获取页面失败", zap.String("url", urlStr), zap.Error(err))
			return nil, err
		}
	case <-timeoutCtx.Done():
		s.log.Warn("无头获取页面超时", zap.String("url", urlStr))
		return nil, errors.New("获取页面超时")
	}

	if html == "" {
		s.log.Warn("页面 HTML 为空", zap.String("url", urlStr))
		return nil, errors.New("页面内容为空")
	}

	return goquery.NewDocumentFromReader(strings.NewReader(html))
}
