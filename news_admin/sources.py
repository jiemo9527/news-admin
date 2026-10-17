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

ORIGINAL_SOURCES = [  # max_items 默认 10
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

# 推荐来源：后台「新闻来源 → 推荐来源」一键添加。note 为 2026-10-17 实测结论。
# 美联社、法新社官网对程序访问返回 403/无 RSS，经 Google 新闻按站点聚合（只有标题和时间，无正文，正文页也不可抓）。
EXTRA_SOURCES = [
    {'name': '彭博社', 'kind': 'jsonfeed', 'feed_url': 'https://bbg.buzzing.cc/lite/feed.json', 'weight': 1.2, 'fetch_body': False,
     'note': '彭博全站新闻（buzzing.cc 中文翻译标题 + 英文原题），付费墙不抓正文。推荐'},
    {'name': '华尔街日报', 'kind': 'rss', 'feed_url': 'https://feeds.content.dowjones.io/public/rss/RSSWorldNews', 'weight': 1.2, 'fetch_body': False,
     'note': '道琼斯官方国际新闻 RSS，带一句导语；付费墙不抓正文。推荐'},
    {'name': '华尔街日报·市场', 'kind': 'rss', 'feed_url': 'https://feeds.content.dowjones.io/public/rss/RSSMarketsMain', 'weight': 1.1, 'fetch_body': False,
     'note': '道琼斯官方市场/金融 RSS。按需'},
    {'name': '美联社', 'kind': 'rss', 'feed_url': 'https://news.google.com/rss/search?q=when:1d+site:apnews.com&hl=en-US&gl=US&ceid=US:en',
     'weight': 1.2, 'fetch_body': False,
     'note': '官网屏蔽程序访问，经 Google 新闻聚合 apnews.com，只有标题，摘要靠模型依据标题。可选'},
    {'name': '法新社', 'kind': 'rss', 'feed_url': 'https://news.google.com/rss/search?q=when:1d+site:afp.com&hl=en-US&gl=US&ceid=US:en',
     'weight': 1.1, 'fetch_body': False,
     'note': '官网无公开 RSS，经 Google 新闻聚合 afp.com，只有标题。可选'},
    {'name': '法广中文', 'kind': 'rss', 'feed_url': 'https://www.rfi.fr/cn/rss', 'weight': 1.0,
     'note': '简体中文，国际/中国/欧洲时政，更新频繁（每次约 30 条）。推荐开启'},
    {'name': '中新网·国际', 'kind': 'rss', 'feed_url': 'https://www.chinanews.com.cn/rss/world.xml', 'weight': 1.0,
     'note': '简体中文国际新闻，更新快；可与央视、人民网互相印证。推荐开启'},
    {'name': 'BBC 中文', 'kind': 'rss', 'feed_url': 'https://feeds.bbci.co.uk/zhongwen/simp/rss.xml', 'weight': 1.1,
     'note': '中文报道，但标题为繁体（模型会统一转简体）；每天约 10 条。可选'},
    {'name': '德国之声中文', 'kind': 'rss', 'feed_url': 'https://rss.dw.com/rdf/rss-chi-all', 'weight': 1.0,
     'note': '简体中文，评论与观察类较多，部分会被价值过滤。可选'},
    {'name': '中新网·财经', 'kind': 'rss', 'feed_url': 'https://www.chinanews.com.cn/rss/finance.xml', 'weight': 1.0,
     'note': '国内财经，地方经济宣传稿多，容易被价值过滤。按需'},
    {'name': '中新网·国内', 'kind': 'rss', 'feed_url': 'https://www.chinanews.com.cn/rss/china.xml', 'weight': 1.0,
     'note': '国内时政，与央视新闻·国内重复度高。按需'},
    {'name': 'The Guardian World', 'kind': 'rss', 'feed_url': 'https://www.theguardian.com/world/rss', 'weight': 1.0,
     'note': '英文原文，长篇深度；增加模型翻译成本。按需'},
    {'name': 'Al Jazeera', 'kind': 'rss', 'feed_url': 'https://www.aljazeera.com/xml/rss/all.xml', 'weight': 1.0,
     'note': '英文，中东、非洲覆盖好；增加模型翻译成本。按需'},
]

DEFAULT_SOURCES = ORIGINAL_SOURCES
