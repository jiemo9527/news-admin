"""默认来源：均为公开 RSS，已实测可访问。weight 表示来源可信度/原创度的相对权重，参与排序。

每个来源每次最多取 max_items 条（默认 20），这直接决定模型调用量和成本。
"""

DEFAULT_SOURCES = [
    {'name': 'BBC 中文', 'feed_url': 'https://feeds.bbci.co.uk/zhongwen/simp/rss.xml', 'weight': 1.1},
    {'name': '德国之声中文', 'feed_url': 'https://rss.dw.com/rdf/rss-chi-all', 'weight': 1.0},
    {'name': '法广中文', 'feed_url': 'https://www.rfi.fr/cn/rss', 'weight': 1.0},
    {'name': 'BBC World', 'feed_url': 'https://feeds.bbci.co.uk/news/world/rss.xml', 'weight': 1.1},
    {'name': 'NYT World', 'feed_url': 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml', 'weight': 1.1},
    {'name': 'The Guardian World', 'feed_url': 'https://www.theguardian.com/world/rss', 'weight': 1.0},
    {'name': 'Al Jazeera', 'feed_url': 'https://www.aljazeera.com/xml/rss/all.xml', 'weight': 1.0},
    {'name': '中新网·国内', 'feed_url': 'https://www.chinanews.com.cn/rss/china.xml', 'weight': 1.0},
    {'name': '中新网·国际', 'feed_url': 'https://www.chinanews.com.cn/rss/world.xml', 'weight': 1.0},
    {'name': '中新网·财经', 'feed_url': 'https://www.chinanews.com.cn/rss/finance.xml', 'weight': 1.0},
]
