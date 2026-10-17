"""默认来源：沿用 v1 的 8 家（已实测 2026-10 可抓取），并补充少量 RSS 来源作为可选。

weight：来源可信度/原创度的相对权重，参与排序。
max_items：每次每个来源最多取多少条，直接决定模型调用量和成本。
kind：rss / jsonfeed / cctv / html（html 需要 link_pattern 指定文章链接正则）。

v1 原配置的变化：
- 路透社：buzzing.cc 的 JSON Feed（中文翻译标题），reuters.com 原文页有反爬，不抓正文
- 央视新闻：改用央视官网列表接口 world_1.jsonp / news_1.jsonp，比解析首页稳定
- CNN：RSS 已停更（最新条目 2023 年），改为网页列表页
- 新华社、人民网、联合早报：网页列表页 + 链接正则（新华社用首页，politics 频道更新慢）
- 纽约时报、BBC、RT：RSS
"""

ORIGINAL_SOURCES = [
    {'name': '路透社', 'kind': 'jsonfeed', 'feed_url': 'https://reutersnew.buzzing.cc/zh-Hant/lite/feed.json', 'weight': 1.2},
    {'name': 'BBC', 'kind': 'rss', 'feed_url': 'https://feeds.bbci.co.uk/news/world/rss.xml', 'weight': 1.1},
    {'name': '纽约时报', 'kind': 'rss', 'feed_url': 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml', 'weight': 1.1},
    {'name': 'CNN', 'kind': 'html', 'feed_url': 'https://edition.cnn.com/world', 'link_pattern': r'cnn\.com/20\d{2}/\d{2}/\d{2}/', 'weight': 1.0},
    {'name': '央视新闻·国内', 'kind': 'cctv', 'feed_url': 'https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/news_1.jsonp', 'weight': 1.1},
    {'name': '央视新闻·国际', 'kind': 'cctv', 'feed_url': 'https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/world_1.jsonp', 'weight': 1.1},
    {'name': '新华社', 'kind': 'html', 'feed_url': 'https://www.news.cn/', 'link_pattern': r'news\.cn/(?:[a-z]+/)?20\d{6}/\w+/c\.html', 'weight': 1.1},
    {'name': '人民网', 'kind': 'html', 'feed_url': 'http://world.people.com.cn/', 'link_pattern': r'people\.com\.cn/n1/20\d{2}/\d{4}/', 'weight': 1.0},
    {'name': 'RT', 'kind': 'rss', 'feed_url': 'https://www.rt.com/rss/news/', 'weight': 0.9},
    {'name': '联合早报', 'kind': 'html', 'feed_url': 'https://www.zaobao.com.sg/news/world', 'link_pattern': r'zaobao\.com\.sg/news/world/story\d+', 'weight': 1.1},
]

EXTRA_SOURCES = [
    {'name': 'BBC 中文', 'kind': 'rss', 'feed_url': 'https://feeds.bbci.co.uk/zhongwen/simp/rss.xml', 'weight': 1.1},
    {'name': '德国之声中文', 'kind': 'rss', 'feed_url': 'https://rss.dw.com/rdf/rss-chi-all', 'weight': 1.0},
    {'name': '法广中文', 'kind': 'rss', 'feed_url': 'https://www.rfi.fr/cn/rss', 'weight': 1.0},
    {'name': '中新网·国内', 'kind': 'rss', 'feed_url': 'https://www.chinanews.com.cn/rss/china.xml', 'weight': 1.0},
    {'name': '中新网·国际', 'kind': 'rss', 'feed_url': 'https://www.chinanews.com.cn/rss/world.xml', 'weight': 1.0},
    {'name': '中新网·财经', 'kind': 'rss', 'feed_url': 'https://www.chinanews.com.cn/rss/finance.xml', 'weight': 1.0},
    {'name': 'The Guardian World', 'kind': 'rss', 'feed_url': 'https://www.theguardian.com/world/rss', 'weight': 1.0},
    {'name': 'Al Jazeera', 'kind': 'rss', 'feed_url': 'https://www.aljazeera.com/xml/rss/all.xml', 'weight': 1.0},
]

DEFAULT_SOURCES = ORIGINAL_SOURCES
