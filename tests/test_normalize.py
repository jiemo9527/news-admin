from news_admin.normalize import canonical_url, clean_text


def test_canonical_url_strips_tracking_params_fragment_and_trailing_slash():
    assert canonical_url('HTTPS://WWW.Example.com/a/b/?utm_source=x&id=3&fbclid=1#top') == 'https://example.com/a/b?id=3'


def test_canonical_url_sorts_query_and_drops_amp_suffix():
    assert canonical_url('https://example.com/news/1.amp?b=2&a=1') == 'https://example.com/news/1?a=1&b=2'


def test_clean_text_unescapes_html_and_normalizes_width_and_space():
    assert clean_text('  Ｈｅｌｌｏ&nbsp;&amp;\u200b 世界\n\n ') == 'Hello & 世界'
