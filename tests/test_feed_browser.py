"""Browser regressions using isolated test data, without a server or scheduler."""
from datetime import timedelta
import time
from urllib.parse import urlsplit

import pytest

from news_admin import tz
from tests.test_feed import ev, save
from tests.test_web import make_client


@pytest.fixture(scope='module')
def chrome():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='chrome', headless=True)
        except playwright.Error as exc:
            if 'not found' in str(exc) or "doesn't exist" in str(exc):
                pytest.skip('Installed Chrome is required for browser regressions')
            raise
        yield browser
        browser.close()


@pytest.mark.parametrize('width,height', [
    (1920, 1080), (1366, 900), (1366, 768), (768, 1024), (390, 844), (360, 800),
])
def test_pagination_returns_to_first_news_after_refresh(tmp_path, chrome, width, height):
    rt, client = make_client(tmp_path)
    now = tz.now()
    save(rt.store, [ev(f'e{i}', f'分页测试新闻{i}', now - timedelta(minutes=i)) for i in range(45)])
    page = chrome.new_page(viewport={'width': width, 'height': height})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def serve(route):
        url = urlsplit(route.request.url)
        if url.query and url.path == '/':
            # The old smooth scroll finishes before this delayed replacement.
            time.sleep(0.8)
        response = client.get(url.path + (f'?{url.query}' if url.query else ''))
        route.fulfill(status=response.status_code, body=response.content,
                      content_type=response.headers.get('content-type', 'text/html'))

    page.route('http://testserver/**', serve)
    try:
        page.goto('http://testserver/')
        # Next page, short final page, previous page, then a numbered link.
        for selector, expected in [
            ('.pager a[rel="next"]', 'e-e20'),
            ('.pager a[rel="next"]', 'e-e40'),
            ('.pager a[rel="prev"]', 'e-e20'),
            ('.pager-pages a[href="/"]', 'e-e0'),
        ]:
            page.locator(selector).click()
            page.wait_for_function("id => document.querySelector('.news-card')?.id === id", arg=expected)
            page.wait_for_function("""() => {
                const card = document.querySelector('.news-card').getBoundingClientRect();
                const header = document.querySelector('.topbar').getBoundingClientRect();
                const filters = document.querySelector('[data-live-form]').getBoundingClientRect();
                const covered = Math.max(header.bottom, filters.bottom);
                return card.top >= covered && card.top < window.innerHeight - 40;
            }""", timeout=4000)
        assert not errors
    finally:
        page.close()
        client.close()
