"""FastAPI 后台：新闻汇总、简报与管理页面，全部需要登录。所有时间均为北京时间（东八区）。

角色：admin 管理员（全部功能）；viewer 只读用户（可浏览所有页面，不能做任何修改）。游客只能看到登录页。
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

from . import tz
from .data_ops import export_backup, restore_backup, clear_data, MAX_ARCHIVE
from .auth import ROLES, generate_password, generate_username, hash_password, validate_password, validate_username, verify_password
from .config import Settings, update_env_file
from .digest import article_times, build_sections, event_region_label
from .fetch import SOURCE_KINDS
from .insight import ANALYSIS_MODEL, InsightError, InsightService
from .models import CONTENT_TYPE_LABELS, REGION_LABELS, TOPIC_LABELS, Topic
from .pipeline import PipelineBusy
from .push import CHANNEL_KINDS
from .rank import GRADE_LABELS, rate_event
from .rss import RSS_LIMIT, TOKEN_PATTERN, TOKEN_SETTING, new_token, render_rss, rss_path
from .runtime import Runtime, build_runtime
from .sources import EXTRA_SOURCES, ORIGINAL_SOURCES
from .store import FEED_RANGES, FEED_SORTS
from .timefmt import created_updated, format_time

BASE = Path(__file__).resolve().parent
PUBLIC_PATHS = ('/login', '/healthz', '/favicon.ico')
PUBLIC_PREFIXES = ('/static/',)
RSS_PUBLIC = re.compile(r'/rss/[A-Za-z0-9_-]+/rss\.xml')  # 免登录，由路径中的随机令牌鉴权
PER_PAGE = 20
PER_PAGE_CHOICES = (10, 20, 50)
STALE_AFTER = timedelta(hours=24)
SESSION_MAX_AGE = 10 * 365 * 86400
SECRET_CHANNEL_FIELDS = ('bot_token', 'webhook', 'secret', 'password')
REPO_URL = 'https://github.com/jiemo9527/news-admin'


def highlight(text: str, q: str):
    """转义后把关键词包上 <mark>（不区分大小写）。"""
    from markupsafe import Markup, escape

    safe = str(escape(text or ''))
    terms = sorted({str(escape(t)) for t in (q or '').split() if t}, key=len, reverse=True)
    if not terms:
        return Markup(safe)
    pattern = re.compile('|'.join(re.escape(t) for t in terms), re.IGNORECASE)
    return Markup(pattern.sub(lambda m: f'<mark>{m.group(0)}</mark>', safe))


def short_ua(ua: str) -> str:
    """User-Agent → 「Chrome · Windows」这样的简称。"""
    ua = ua or ''
    if not ua:
        return '—'
    browser = next((name for key, name in (('Edg/', 'Edge'), ('OPR/', 'Opera'), ('MicroMessenger', '微信'), ('Firefox/', 'Firefox'),
                                           ('Chrome/', 'Chrome'), ('Safari/', 'Safari'), ('curl/', 'curl'), ('python', 'Python'),
                                           ('testclient', '测试客户端')) if key in ua), ua.split('/')[0][:20])
    system = next((name for key, name in (('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android'), ('Windows', 'Windows'),
                                          ('Mac OS', 'macOS'), ('Linux', 'Linux')) if key in ua), '')
    return f'{browser} · {system}' if system else browser


def duration(start: str | None, end: str | None) -> str:
    a, b = tz.parse(start), tz.parse(end)
    if not a or not b:
        return '-'
    seconds = int((b - a).total_seconds())
    return f'{seconds // 60}分{seconds % 60:02d}秒' if seconds >= 60 else f'{seconds}秒'


def local_time(value: str | None, fmt: str = '%Y-%m-%d %H:%M') -> str:
    """存储的 ISO 时间 → 北京时间字符串。"""
    parsed = tz.parse(value)
    return parsed.strftime(fmt) if parsed else '-'


def create_app(runtime: Runtime | None = None, start_scheduler: bool = True) -> FastAPI:
    rt = runtime or build_runtime()
    settings = rt.settings
    secret = settings.secret_key or rt.store.get_setting('secret_key')
    if not secret:
        secret = secrets.token_urlsafe(32)
        rt.store.set_setting('secret_key', secret)
    password_file = settings.data_dir / 'admin_password.txt'  # 仅用于清理旧版本遗留文件，不再创建
    password_pinned = bool(settings.admin_password)

    def bootstrap_admin() -> None:
        """首次启动 / 从单密码版本升级：保证存在管理员账号。"""
        admin = rt.store.get_user_by_name(settings.admin_username)
        if password_pinned:
            if admin is None:
                rt.store.create_user(settings.admin_username, hash_password(settings.admin_password), 'admin')
            elif not verify_password(settings.admin_password, admin['password_hash']) or admin['role'] != 'admin':
                rt.store.update_user(admin['id'], password_hash=hash_password(settings.admin_password), role='admin', enabled=1)
            return
        if admin is not None:
            return
        legacy_hash = rt.store.get_setting('admin_password_hash')
        legacy_plain = rt.store.get_setting('admin_password')
        if legacy_hash:
            rt.store.create_user(settings.admin_username, legacy_hash, 'admin')
            password_file.unlink(missing_ok=True)
        else:
            initial = legacy_plain or secrets.token_urlsafe(12)
            rt.store.create_user(settings.admin_username, hash_password(initial), 'admin')
            logging.getLogger(__name__).warning('初始化管理员：%s  初始密码：%s  请登录后修改密码，此信息仅在首次初始化输出', settings.admin_username, initial)
        rt.store.set_setting('admin_password', '')

    bootstrap_admin()
    password_file.unlink(missing_ok=True)

    scheduler = BackgroundScheduler(timezone=tz.TZ)
    rt.store.set_setting('push_cron', rt.store.get_setting('push_cron', '0 6,20 * * *'))
    rt.store.set_setting('push_enabled', rt.store.get_setting('push_enabled', '1'))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_scheduler:
            scheduler.start()
            sync_schedule()
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)

    app = FastAPI(title='News Admin', docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.scheduler = scheduler
    templates = Jinja2Templates(directory=str(BASE / 'templates'))
    templates.env.globals.update(
        format_time=format_time, created_updated=created_updated, article_times=article_times, local_time=local_time, duration=duration,
        source_kinds=SOURCE_KINDS, topic_labels=TOPIC_LABELS, region_labels=REGION_LABELS, region_label=event_region_label,
        content_type_labels=CONTENT_TYPE_LABELS, channel_kinds=CHANNEL_KINDS, grade_labels=GRADE_LABELS,
        feed_ranges=FEED_RANGES, feed_sorts=FEED_SORTS, fetch_windows=tz.FETCH_WINDOWS, repo_url=REPO_URL,
    )
    templates.env.filters['highlight'] = highlight
    templates.env.filters['ua'] = short_ua
    app.mount('/static', StaticFiles(directory=str(BASE / 'static')), name='static')

    def run_in_background(trigger: str) -> bool:
        if rt.pipeline.busy:
            return False

        def worker() -> None:
            try:
                rt.collect(trigger)
            except PipelineBusy:
                pass
            except Exception:  # 已在 runs 表中记录失败
                pass

        threading.Thread(target=worker, daemon=True).start()
        return True

    def sync_schedule() -> None:
        cron = rt.store.get_setting('schedule_cron', '0 */2 * * *')
        enabled = rt.store.get_setting('schedule_enabled', '0') == '1'
        if scheduler.get_job('pipeline'):
            scheduler.remove_job('pipeline')
        if enabled:
            scheduler.add_job(lambda: run_in_background('schedule'), CronTrigger.from_crontab(cron, timezone=tz.TZ),
                              id='pipeline', coalesce=True, max_instances=1, misfire_grace_time=3600)
        if scheduler.get_job('push_digest'):
            scheduler.remove_job('push_digest')
        if rt.store.get_setting('push_enabled', '1') == '1':
            scheduler.add_job(scheduled_push,
                CronTrigger.from_crontab(rt.store.get_setting('push_cron', '0 6,20 * * *'), timezone=tz.TZ),
                id='push_digest', coalesce=True, max_instances=1, misfire_grace_time=3600)

    def scheduled_push() -> None:
        log = logging.getLogger(__name__)
        try:
            for r in rt.deliver_digest():
                log.info('scheduled push %s: %s %s', r['channel'], r['status'], r.get('count', '') or r.get('error', ''))
        except Exception:
            log.exception('scheduled push failed')

    def next_run_time(job_id='pipeline') -> str:
        job = scheduler.get_job(job_id) if scheduler.running else None
        return job.next_run_time.astimezone(tz.TZ).strftime('%m-%d %H:%M') if job and job.next_run_time else ''

    def client_ip(request: Request) -> str:
        if rt.settings.trust_proxy:
            forwarded = request.headers.get('x-forwarded-for', '').split(',')[0].strip()
            if forwarded:
                return forwarded[:64]
            real = request.headers.get('x-real-ip', '').strip()
            if real:
                return real[:64]
        return (request.client.host if request.client else '')[:64]

    def audit(request: Request, user: dict | None, event: str, username: str = '') -> None:
        rt.store.log_access(
            user['id'] if user else None, user['username'] if user else username, event, client_ip(request),
            request.headers.get('user-agent', ''), request.url.path + (f'?{request.url.query}' if request.url.query else ''),
        )

    def current_user(request: Request) -> dict | None:
        uid = request.session.get('uid')
        if not uid:
            return None
        user = rt.store.get_user(uid)
        if not user or not user['enabled'] or user['session_version'] != request.session.get('sv'):
            request.session.clear()
            return None
        return user

    @app.middleware('http')
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or (request.method == 'GET' and RSS_PUBLIC.fullmatch(path)):
            return await call_next(request)
        user = current_user(request)
        if user is None:
            target = request.url.path + (f'?{request.url.query}' if request.url.query else '')
            return RedirectResponse('/login' + (f'?next={quote(target)}' if path != '/' else ''), status_code=303)
        request.state.user = user
        if request.method == 'GET' and not path.startswith('/thumb/'):
            audit(request, user, 'visit')
        # 只读用户：所有写操作（POST）一律拒绝，修改自己的密码和个人阅读状态（/me/）除外
        if request.method != 'GET' and user['role'] != 'admin' and path not in ('/logout', '/admin/password') and not path.startswith('/me/'):
            return Response('只读用户无权执行此操作', status_code=403, media_type='text/plain; charset=utf-8')
        if (path.startswith('/admin/users') or path.startswith('/admin/data')) and user['role'] != 'admin':
            return Response('仅管理员可访问', status_code=403, media_type='text/plain; charset=utf-8')
        return await call_next(request)

    # 登录长期有效、可多设备同时登录；改密码、改角色、停用账号或恢复备份时所有设备统一失效（session_version）
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site='lax', max_age=SESSION_MAX_AGE, https_only=False)

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        user = getattr(request.state, 'user', None)
        return templates.TemplateResponse(request, name, {
            'rt': rt, 'user': user, 'admin': bool(user and user['role'] == 'admin'), 'signed_in': user is not None,
            'path': request.url.path, 'stale_warning': stale_warning() if user and name == 'feed.html' else '',
            'analysis_enabled': analysis_enabled(), 'analysis_model': ANALYSIS_MODEL, **context,
        })

    insights = InsightService()

    def analysis_enabled() -> bool:
        return rt.store.get_setting('analysis_enabled', '1') == '1'

    def stale_warning() -> str:
        """最近 24 小时没有任何新内容入库时提示（只在首页显示，5 秒后消失）。"""
        last = tz.parse(rt.store.last_content_at())
        if last is None or tz.now() - last < STALE_AFTER:
            return ''
        hours = int((tz.now() - last).total_seconds() // 3600)
        return f'已超过 24 小时没有新内容（上次 {last.strftime("%m-%d %H:%M")}，{hours} 小时前），请检查抓取计划和来源'

    def is_fetch(request: Request) -> bool:
        return request.headers.get('x-requested-with') == 'fetch'

    def back(request: Request, fallback: str = '/') -> RedirectResponse:
        """无 JS 时回到原页面；只接受本站路径。"""
        from urllib.parse import urlsplit
        parts = urlsplit(request.headers.get('referer', ''))
        same = parts.netloc == request.url.netloc and parts.path.startswith('/') and not parts.path.startswith('//')
        return RedirectResponse((parts.path + (f'?{parts.query}' if parts.query else '')) if same else fallback, status_code=303)

    def flash(request: Request, message: str, kind: str = 'info') -> None:
        request.session['flash'] = {'text': message, 'kind': kind}

    def ensure_rating(event, at) -> None:
        if event.rating is None:
            event.rating = rate_event(event, at)

    # ---------- public ----------
    @app.get('/healthz')
    def healthz() -> dict[str, Any]:
        return {'ok': True}

    @app.get('/', response_class=HTMLResponse)
    def index(request: Request, q: str = '', range: str = 'all', sort: str = 'time', topic: str = '', source: str = '', unread: str = ''):
        q = ' '.join(q.split())[:60]
        source = source.strip()[:60]
        range_ = range if range in FEED_RANGES else 'all'
        sort = sort if sort in FEED_SORTS else 'time'
        topic = topic if topic in {t.value for t in Topic} else ''
        only_unread = unread == '1'
        uid = request.state.user['id']
        raw = request.query_params.get('page', '1')
        current = int(raw) if raw.isdigit() and int(raw) > 0 else 1
        # 每页条数：显式选择时记入会话，之后刷新/返回首页沿用
        raw_per = request.query_params.get('per', '')
        if raw_per.isdigit() and int(raw_per) in PER_PAGE_CHOICES:
            per = int(raw_per)
            request.session['per'] = per
        else:
            saved = request.session.get('per')
            per = saved if saved in PER_PAGE_CHOICES else PER_PAGE
        # 先固定快照（最新运行 + 当前时间），渲染与长按「至此已读」都基于它
        latest = rt.store.latest_completed_run()
        max_run = latest['id'] if latest else 0
        now = tz.now()

        def load(page_no: int):
            return rt.store.feed(range_=range_, q=q, page=page_no, per_page=per, sort=sort, topic=topic, source=source,
                                 user_id=uid, unread=only_unread, max_run=max_run, now=now)

        feed = load(current)
        if feed.total and feed.page > feed.pages:
            feed = load(feed.pages)
        for item in feed.items:
            ensure_rating(item.event, now)
        defaults = {'q': '', 'range': 'all', 'sort': 'time', 'topic': '', 'source': '', 'unread': '', 'page': 1, 'per': PER_PAGE}

        def url(**override: Any) -> str:
            merged = {'q': q, 'range': range_, 'sort': sort, 'topic': topic, 'source': source, 'unread': '1' if only_unread else '',
                      'page': 1, 'per': per, **override}
            # 明确切换每页条数时总带上 per，才能覆盖会话里记住的值
            query = {k: v for k, v in merged.items() if v != defaults[k] or (k == 'per' and 'per' in override)}
            return '/' + ('?' + urlencode(query) if query else '')

        return page(
            request, 'feed.html', feed=feed, q=q, range_=range_, sort=sort, topic=topic, source=source, url=url, unread=only_unread,
            per=per, per_choices=PER_PAGE_CHOICES,
            topic_counts=rt.store.topic_counts(range_=range_, q=q, source=source, user_id=uid, unread=only_unread, now=now),
            source_counts=rt.store.source_counts(range_=range_, q=q, topic=topic, user_id=uid, unread=only_unread, now=now), latest=latest,
            history=rt.store.search_history(uid), max_run=max_run, snapshot_at=tz.iso(now),
            retention_days=int(rt.store.get_setting('retention_days', str(settings.retention_days))),
        )

    @app.get('/favorites', response_class=HTMLResponse)
    def favorites(request: Request):
        items = rt.store.favorites(request.state.user['id'])
        now = tz.now()
        for item in items:
            ensure_rating(item.event, now)
        return page(request, 'favorites.html', items=items)

    # ---------- 个人阅读状态：只读用户也可使用，只影响自己 ----------
    def personal_result(request: Request, payload: dict) -> Any:
        return payload if is_fetch(request) else back(request)

    @app.post('/me/read')
    def toggle_read(request: Request, canonical: str = Form(...), value: str = Form('1')):
        if not rt.store.canonical_exists(canonical):
            raise HTTPException(404)
        rt.store.set_read(request.state.user['id'], [canonical], value == '1')
        return personal_result(request, {'ok': True, 'value': value == '1'})

    @app.post('/me/favorite')
    def toggle_favorite(request: Request, canonical: str = Form(...), value: str = Form('1')):
        if not rt.store.canonical_exists(canonical):
            raise HTTPException(404)
        rt.store.set_favorite(request.state.user['id'], canonical, value == '1')
        return personal_result(request, {'ok': True, 'value': value == '1'})

    @app.post('/me/read/upto')
    def read_upto(request: Request, canonical: str = Form(...), max_run: int = Form(...), range: str = Form('all'), q: str = Form(''),
                  sort: str = Form('time'), topic: str = Form(''), source: str = Form(''), unread: str = Form(''), at: str = Form('')):
        # 用页面打开时的时间重建筛选边界（跨午夜、3 天自动已读边界不漂移）；不信任未来时间
        snapshot = tz.parse(at)
        if snapshot is None or snapshot > tz.now():
            snapshot = tz.now()
        count = rt.store.mark_read_upto(
            request.state.user['id'], canonical, max_run, range_=range if range in FEED_RANGES else 'all', q=' '.join(q.split())[:60],
            sort=sort if sort in FEED_SORTS else 'time', topic=topic if topic in {t.value for t in Topic} else '',
            source=source.strip()[:60], unread=unread == '1', now=snapshot)
        if count is None:
            raise HTTPException(404)
        return personal_result(request, {'ok': True, 'count': count})

    @app.post('/me/read/brief/{run_id}')
    def read_brief(request: Request, run_id: int):
        run = rt.store.get_run(run_id)
        if not run or run['status'] != 'completed':
            raise HTTPException(404)
        count = rt.store.mark_run_read(request.state.user['id'], run_id)
        if is_fetch(request):
            return {'ok': True, 'count': count}
        flash(request, f'已将本期 {count} 条标为已读', 'success')
        return back(request, f'/briefs/{run_id}')

    @app.post('/me/read/push/{key}')
    def read_push_brief(request: Request, key: str):
        snapshot = rt.store.get_push_brief(key)
        if snapshot is None:
            raise HTTPException(404)
        count = rt.store.mark_events_read(request.state.user['id'], snapshot['run_id'], [e.id for e in snapshot['events']])
        if is_fetch(request):
            return {'ok': True, 'count': count}
        flash(request, f'已将本期 {count} 条标为已读', 'success')
        return back(request, f'/briefs/push/{key}')

    @app.post('/me/search-history')
    def save_search(request: Request, q: str = Form('')):
        return personal_result(request, {'items': rt.store.add_search_history(request.state.user['id'], q)})

    @app.post('/me/search-history/clear')
    def clear_search(request: Request):
        rt.store.clear_search_history(request.state.user['id'])
        return personal_result(request, {'items': []})

    @app.post('/me/search-history/remove')
    def remove_search(request: Request, q: str = Form('')):
        return personal_result(request, {'items': rt.store.remove_search_history(request.state.user['id'], q)})

    @app.post('/me/analyze')
    def analyze_event(request: Request, canonical: str = Form(...)):
        """按需分析（gpt-5.6-luna 联网检索）。只读用户也可用；后台可整体关闭。"""
        if not analysis_enabled():
            raise HTTPException(403, '文章分析已在后台关闭')
        found = rt.store.latest_event(canonical)
        if found is None:
            raise HTTPException(404, '新闻不存在或已过期')
        run_id, event = found
        try:
            return insights.analyze(rt, request.state.user['id'], run_id, canonical, event)
        except InsightError as exc:
            logging.getLogger(__name__).warning('analysis failed for %s: %s', canonical, exc)
            raise HTTPException(exc.status, str(exc))

    # ---------- RSS：免登录，凭路径中的随机令牌访问；未开启或令牌不符一律 404 ----------
    def site_url(request: Request) -> str:
        return rt.settings.public_base_url.rstrip('/') or str(request.base_url).rstrip('/')

    @app.get('/rss/{token}/rss.xml', include_in_schema=False)
    def rss_feed(request: Request, token: str):
        expected = rt.store.get_setting(TOKEN_SETTING)
        if not expected or not TOKEN_PATTERN.match(token) or not hmac.compare_digest(token, expected):
            raise HTTPException(404)
        now = tz.now()
        items = rt.store.feed(page=1, per_page=RSS_LIMIT, sort='time', now=now).items
        for item in items:
            ensure_rating(item.event, now)
        return Response(render_rss(items, site_url(request) + '/', now=now), media_type='application/rss+xml; charset=utf-8',
                        headers={'Cache-Control': 'no-cache', 'X-Robots-Tag': 'noindex'})

    @app.get('/favicon.ico', include_in_schema=False)
    def favicon():
        return RedirectResponse('/static/favicon.svg', status_code=301)

    @app.get('/briefs', response_class=HTMLResponse)
    def brief_list(request: Request):
        return page(request, 'briefs.html', briefs=rt.store.brief_index())

    @app.get('/briefs/push/{key}', response_class=HTMLResponse)
    def push_brief(request: Request, key: str):
        snapshot = rt.store.get_push_brief(key)
        if snapshot is None:
            raise HTTPException(404)
        canonical = rt.store.canonical_ids_upto(snapshot['run_id'], [e.id for e in snapshot['events']])
        return page(request, 'brief.html', run={'id': snapshot['run_id'], 'finished_at': snapshot['created_at']},
                    sections=build_sections(snapshot['events']), push_snapshot=True, push_key=key, canonical_ids=canonical)

    @app.get('/briefs/{run_id}', response_class=HTMLResponse)
    def brief(request: Request, run_id: int):
        run = rt.store.get_run(run_id)
        if not run or run['status'] != 'completed':
            raise HTTPException(404)
        events = rt.store.load_events(run_id)
        # 早期快照没有存评级：按运行完成时刻补算（新鲜度相对当时，而不是现在）
        finished = tz.parse(run.get('finished_at')) or tz.now()
        for event in events:
            ensure_rating(event, finished)
        canonical = rt.store.canonical_ids(run_id, [e.id for e in events])
        return page(request, 'brief.html', run=run, sections=build_sections(events), canonical_ids=canonical)

    @app.get('/thumb/{key}')
    def thumb(key: str):
        found = rt.thumbnails.read(key)
        if not found:
            raise HTTPException(404)
        data, mime = found
        return Response(data, media_type=mime, headers={'Cache-Control': 'public, max-age=604800, immutable'})

    @app.get('/login', response_class=HTMLResponse)
    def login_form(request: Request, next: str = '/'):
        return page(request, 'login.html', next=next, initial_file=password_file.exists())

    @app.post('/login')
    def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form('/')):
        user = rt.store.get_user_by_name(username.strip())
        if user and user['enabled'] and verify_password(password, user['password_hash']):
            audit(request, user, 'login')
            request.session.clear()
            request.session.update({'uid': user['id'], 'sv': user['session_version']})
            rt.store.touch_login(user['id'])
            return RedirectResponse(next if next.startswith('/') and not next.startswith('//') else '/', status_code=303)
        audit(request, None, 'login_failed', username.strip()[:64] or '（空）')
        flash(request, '用户名或密码错误，或账号已停用', 'error')
        return RedirectResponse('/login' + (f'?next={quote(next)}' if next and next != '/' else ''), status_code=303)

    @app.post('/logout')
    def logout(request: Request):
        user = getattr(request.state, 'user', None)
        if user:
            audit(request, user, 'logout')
        request.session.clear()
        return RedirectResponse('/login', status_code=303)

    # ---------- admin ----------
    @app.get('/admin', response_class=HTMLResponse)
    def dashboard(request: Request):
        runs = rt.store.list_runs(30)
        return page(
            request, 'dashboard.html', runs=runs, busy=rt.pipeline.busy, next_run=next_run_time(),
            schedule_cron=rt.store.get_setting('schedule_cron', '0 */2 * * *'),
            schedule_enabled=rt.store.get_setting('schedule_enabled', '0') == '1',
            push_cron=rt.store.get_setting('push_cron', '0 6,20 * * *'),
            push_enabled=rt.store.get_setting('push_enabled', '1') == '1', next_push=next_run_time('push_digest'),
            sources=rt.store.list_sources(), health=rt.store.source_health(), channels=rt.store.list_channels(),
            fetch_window=rt.store.get_setting('fetch_window', settings.fetch_window),
        )

    @app.post('/admin/run')
    def trigger_run(request: Request):
        started = run_in_background('manual')
        flash(request, '已开始抓取（不推送），约 3-10 分钟完成，可刷新查看进度' if started else '已有抓取任务在运行', 'success' if started else 'error')
        return RedirectResponse('/admin', status_code=303)

    @app.post('/admin/schedule')
    def save_schedule(request: Request, cron: str = Form(...), enabled: str = Form('0')):
        try:
            CronTrigger.from_crontab(cron, timezone=tz.TZ)
        except ValueError as exc:
            flash(request, f'Cron 表达式无效：{exc}', 'error')
            return RedirectResponse('/admin', status_code=303)
        rt.store.set_setting('schedule_cron', cron.strip())
        rt.store.set_setting('schedule_enabled', '1' if enabled == '1' else '0')
        sync_schedule()
        flash(request, '定时任务已保存', 'success')
        return RedirectResponse('/admin', status_code=303)

    @app.post('/admin/push-schedule')
    def save_push_schedule(request: Request, push_cron: str = Form(...), enabled: str = Form('0')):
        try:
            CronTrigger.from_crontab(push_cron, timezone=tz.TZ)
        except ValueError as exc:
            flash(request, f'推送 Cron 无效：{exc}', 'error')
            return RedirectResponse('/admin', status_code=303)
        rt.store.set_setting('push_cron', push_cron.strip())
        rt.store.set_setting('push_enabled', '1' if enabled == '1' else '0')
        sync_schedule()
        flash(request, '推送计划已保存；不改变抓取计划', 'success')
        return RedirectResponse('/admin', status_code=303)

    @app.get('/admin/data', response_class=HTMLResponse)
    def data_page(request: Request):
        return page(request, 'data.html')

    def verify_data_password(request: Request, password: str):
        if not verify_password(password, request.state.user['password_hash']):
            raise HTTPException(400, '当前管理员密码不正确')

    @app.post('/admin/data/export')
    def download_backup(request: Request, password: str = Form(...)):
        verify_data_password(request, password)
        try:
            data = export_backup(rt)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return Response(data, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="news-admin-' + tz.now().strftime('%Y%m%d-%H%M%S') + '.zip"',
            'Cache-Control': 'no-store'})

    @app.post('/admin/data/restore')
    async def upload_backup(request: Request, password: str = Form(...), file: UploadFile = File(...)):
        verify_data_password(request, password)
        payload = await file.read(MAX_ARCHIVE + 1)
        try:
            await run_in_threadpool(restore_backup, rt, payload)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        sync_schedule()
        request.session.clear()
        return RedirectResponse('/login', status_code=303)

    @app.post('/admin/data/clear')
    def clear_saved_data(request: Request, password: str = Form(...)):
        verify_data_password(request, password)
        uid = request.state.user['id']
        try:
            clear_data(rt)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        request.session.clear()
        fresh = rt.store.get_user(uid)
        request.session.update({'uid': uid, 'sv': fresh['session_version']})
        flash(request, '新闻、简报、运行与访问记录、普通用户已清空；所有管理员和系统配置保留。', 'success')
        return RedirectResponse('/admin/data', status_code=303)

    @app.get('/admin/runs/{run_id}', response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        run = rt.store.get_run(run_id)
        if not run:
            raise HTTPException(404)
        return page(request, 'run.html', run=run, events=rt.store.load_events(run_id, brief_only=False), deliveries=rt.store.deliveries(run_id))

    @app.post('/admin/runs/{run_id}/delete')
    def delete_run(request: Request, run_id: int):
        run = rt.store.get_run(run_id)
        if not run or run['status'] == 'running':
            flash(request, '运行中的任务不能删除', 'error')
        elif rt.store.delete_run(run_id):
            flash(request, f'已删除运行记录 #{run_id}', 'success')
        return RedirectResponse('/admin', status_code=303)

    @app.post('/admin/runs/{run_id}/deliver')
    def redeliver(request: Request, run_id: int):
        results = rt.deliver(run_id)
        labels = {'sent': '成功', 'busy': '另一个推送正在进行，请稍后重试'}
        flash(request, '；'.join(f"{r['channel']}：{labels.get(r['status'], '失败')}" for r in results) or '没有启用的推送渠道',
              'error' if any(r['status'] != 'sent' for r in results) else 'success')
        return RedirectResponse(f'/admin/runs/{run_id}', status_code=303)

    presets = {p['feed_url']: p for p in [*ORIGINAL_SOURCES, *EXTRA_SOURCES]}

    @app.get('/admin/sources', response_class=HTMLResponse)
    def sources(request: Request, edit: int | None = None):
        current = sorted(rt.store.list_sources(), key=lambda s: (not s.enabled, s.id))
        active = {s.feed_url for s in current if s.enabled}
        added = {s.feed_url for s in current}
        editing = next((s for s in current if s.id == edit), None)
        return page(
            request, 'sources.html', sources=current, health=rt.store.source_health(), editing=editing,
            presets=[{**p, 'added': p['feed_url'] in added} for p in presets.values() if p['feed_url'] not in active],
            test_result=request.session.pop('source_test', None),
        )

    @app.post('/admin/sources/preset')
    def add_preset(request: Request, feed_url: str = Form(...)):
        preset = presets.get(feed_url)
        if preset is None:
            flash(request, '未知的推荐来源', 'error')
        else:
            rt.store.add_preset_source(preset)
            flash(request, f"已启用「{preset['name']}」，下次运行生效", 'success')
        return RedirectResponse('/admin/sources', status_code=303)

    @app.post('/admin/sources/{source_id}/test')
    def test_source(request: Request, source_id: int):
        source = next((s for s in rt.store.list_sources() if s.id == source_id), None)
        if source is None:
            raise HTTPException(404)
        import dataclasses

        result = rt.fetch_preview(dataclasses.replace(source, fetch_body=False))
        request.session['source_test'] = {
            'name': source.name, 'count': len(result.articles), 'error': result.error,
            'items': [{'title': a.title, 'url': a.url, 'time': format_time(a.published_at, a.time_precision)} for a in result.articles[:8]],
        }
        return RedirectResponse('/admin/sources#test', status_code=303)

    @app.post('/admin/sources')
    def save_source(
        request: Request, name: str = Form(...), feed_url: str = Form(...), weight: float = Form(1.0),
        max_items: int = Form(20), fetch_body: str = Form('0'), source_id: int | None = Form(None),
        kind: str = Form('rss'), link_pattern: str = Form(''),
    ):
        if kind not in SOURCE_KINDS:
            raise HTTPException(400)
        if kind == 'html':
            try:
                re.compile(link_pattern)
            except re.error as exc:
                flash(request, f'链接正则无效：{exc}', 'error')
                return RedirectResponse('/admin/sources', status_code=303)
            if not link_pattern.strip():
                flash(request, '网页列表源必须填写文章链接正则', 'error')
                return RedirectResponse('/admin/sources', status_code=303)
        rt.store.upsert_source(source_id, name.strip(), feed_url.strip(), weight, max_items, fetch_body == '1', kind, link_pattern.strip())
        flash(request, '来源已保存', 'success')
        return RedirectResponse('/admin/sources', status_code=303)

    @app.post('/admin/sources/{source_id}/toggle')
    def toggle_source(source_id: int):
        source = next((s for s in rt.store.list_sources() if s.id == source_id), None)
        if source:
            rt.store.set_source_enabled(source_id, not source.enabled)
        return RedirectResponse('/admin/sources', status_code=303)

    @app.post('/admin/sources/{source_id}/delete')
    def delete_source(source_id: int):
        rt.store.delete_source(source_id)
        return RedirectResponse('/admin/sources', status_code=303)

    @app.post('/admin/briefs/{run_id}/delete')
    def delete_brief(request: Request, run_id: int):
        if rt.pipeline.busy and (rt.store.get_run(run_id) or {}).get('status') == 'running':
            flash(request, '运行中的任务不能删除', 'error')
        elif rt.store.delete_run(run_id):
            flash(request, f'已删除简报 #{run_id}', 'success')
        target = request.headers.get('referer', '/briefs')
        return RedirectResponse('/briefs' if f'/briefs/{run_id}' in target or '/admin/runs/' in target else target, status_code=303)

    def channel_view(channel: dict, admin: bool) -> dict:
        """页面展示用：密钥字段只显示是否已设置；只读用户看不到任何配置值。"""
        spec = CHANNEL_KINDS[channel['kind']]
        view = {}
        for field in spec['fields']:
            value = str(channel['config'].get(field, ''))
            secret = field in SECRET_CHANNEL_FIELDS
            view[field] = {'set': bool(value), 'value': '' if (secret or not admin) else value, 'secret': secret}
        return {**channel, 'view': view, 'config': None}

    @app.get('/admin/channels', response_class=HTMLResponse)
    def channels(request: Request, edit: int | None = None, add: str = ''):
        admin = request.state.user['role'] == 'admin'
        items = [channel_view(c, admin) for c in rt.store.list_channels()]
        last = {r['channel_id']: r for r in rt.store._rows(
            'SELECT d.channel_id, d.status, d.error, d.created_at FROM deliveries d '
            'JOIN (SELECT channel_id, MAX(id) AS id FROM deliveries GROUP BY channel_id) m ON m.id=d.id')}
        cursors = {c['id']: rt.store.get_setting(f"push_cursor_{c['id']}") for c in items}
        editing = next((c for c in items if c['id'] == edit), None) if admin else None
        return page(request, 'channels.html', channels=items, editing=editing, add_kind=add if add in CHANNEL_KINDS and admin else '',
                    last=last, cursors=cursors, test_result=request.session.pop('channel_test', None),
                    push_cron=rt.store.get_setting('push_cron', '0 6,20 * * *'),
                    push_enabled=rt.store.get_setting('push_enabled', '1') == '1', next_push=next_run_time('push_digest'))

    def channel_config(form, kind: str, existing: dict | None) -> tuple[dict, list[str]]:
        spec = CHANNEL_KINDS[kind]
        config, missing = {}, []
        for field in spec['fields']:
            value = str(form.get(field, '')).strip()
            if not value and existing and existing['config'].get(field) and (field in SECRET_CHANNEL_FIELDS or field in spec['required']):
                value = existing['config'][field]  # 密钥/必填项留空 = 保持原值
            if any(c in value for c in '\r\n'):
                value = ''
            config[field] = value
            if field in spec['required'] and not value:
                missing.append(spec['fields'][field])
        return config, missing

    @app.post('/admin/channels')
    async def create_channel(request: Request):
        form = await request.form()
        kind = str(form.get('kind', ''))
        if kind not in CHANNEL_KINDS:
            raise HTTPException(400)
        config, missing = channel_config(form, kind, None)
        if missing:
            flash(request, '请填写：' + '、'.join(missing), 'error')
            return RedirectResponse(f'/admin/channels?add={kind}#form', status_code=303)
        name = str(form.get('name', '')).strip()[:40] or CHANNEL_KINDS[kind]['label']
        rt.store.upsert_channel(None, kind, name, config, form.get('enabled') == '1')
        flash(request, f'已添加渠道「{name}」，可点击「测试」确认能收到消息', 'success')
        return RedirectResponse('/admin/channels', status_code=303)

    @app.post('/admin/channels/{channel_id}')
    async def update_channel(request: Request, channel_id: int):
        existing = rt.store.get_channel(channel_id)
        if existing is None:
            raise HTTPException(404)
        form = await request.form()
        config, missing = channel_config(form, existing['kind'], existing)
        if missing:
            flash(request, '请填写：' + '、'.join(missing), 'error')
            return RedirectResponse(f'/admin/channels?edit={channel_id}#form', status_code=303)
        name = str(form.get('name', '')).strip()[:40] or existing['name']
        rt.store.upsert_channel(channel_id, existing['kind'], name, config, form.get('enabled') == '1')
        flash(request, f'渠道「{name}」已保存', 'success')
        return RedirectResponse('/admin/channels', status_code=303)

    @app.post('/admin/channels/{channel_id}/toggle')
    def toggle_channel(request: Request, channel_id: int):
        channel = rt.store.get_channel(channel_id)
        if channel:
            rt.store.set_channel_enabled(channel_id, not channel['enabled'])
            flash(request, f"渠道「{channel['name']}」已{'停用' if channel['enabled'] else '启用'}", 'success')
        return RedirectResponse('/admin/channels', status_code=303)

    @app.post('/admin/channels/{channel_id}/test')
    def test_channel(request: Request, channel_id: int):
        ok, message = rt.test_channel(channel_id)
        request.session['channel_test'] = {'id': channel_id, 'ok': ok, 'message': message}
        return RedirectResponse('/admin/channels', status_code=303)

    @app.post('/admin/channels/{channel_id}/delete')
    def delete_channel(request: Request, channel_id: int):
        rt.store.delete_channel(channel_id)
        flash(request, '渠道已删除', 'success')
        return RedirectResponse('/admin/channels', status_code=303)

    @app.post('/admin/push-now')
    def push_now(request: Request):
        results = rt.deliver_digest()
        labels = {'sent': '已推送', 'empty': '没有新内容', 'failed': '失败', 'busy': '另一个推送正在进行，请稍后重试'}
        text = '；'.join(f"{r['channel']}：{labels.get(r['status'], r['status'])}{'（' + str(r['count']) + ' 条）' if r.get('count') else ''}" for r in results)
        flash(request, text or '没有启用的推送渠道', 'error' if any(r['status'] == 'failed' for r in results) else 'success')
        return RedirectResponse(request.headers.get('referer') or '/admin/channels', status_code=303)

    @app.get('/admin/settings', response_class=HTMLResponse)
    def settings_page(request: Request):
        return page(
            request, 'settings.html', password_pinned=password_pinned,
            fetch_window=rt.store.get_setting('fetch_window', settings.fetch_window),
            retention_days=rt.store.get_setting('retention_days', str(settings.retention_days)),
            s=rt.settings, test_results=request.session.pop('conn_test', {}),
            rss_url=(site_url(request) + rss_path(token)) if (token := rt.store.get_setting(TOKEN_SETTING)) else '', rss_limit=RSS_LIMIT,
        )

    @app.post('/admin/settings/rss')
    def save_rss(request: Request, action: str = Form(...)):
        """生成 / 重置（旧地址立即失效）/ 关闭 RSS 订阅地址。"""
        if action in ('enable', 'reset'):
            rt.store.set_setting(TOKEN_SETTING, new_token())
            flash(request, 'RSS 订阅地址已' + ('生成' if action == 'enable' else '重置，旧地址已失效'), 'success')
        elif action == 'disable':
            rt.store.set_setting(TOKEN_SETTING, '')
            flash(request, 'RSS 订阅已关闭，原地址失效', 'success')
        else:
            raise HTTPException(400)
        return RedirectResponse('/admin/settings#rss', status_code=303)

    @app.post('/admin/settings')
    def save_settings(request: Request, fetch_window: str = Form(...), retention_days: str = Form(...)):
        if fetch_window not in tz.FETCH_WINDOWS or not retention_days.isdigit() or not 7 <= int(retention_days) <= 365:
            flash(request, '参数无效：保留天数需在 7-365 之间', 'error')
            return RedirectResponse('/admin/settings', status_code=303)
        rt.store.set_setting('fetch_window', fetch_window)
        rt.store.set_setting('retention_days', retention_days)
        flash(request, '设置已保存，下次运行生效', 'success')
        return RedirectResponse('/admin/settings', status_code=303)

    @app.post('/admin/settings/analysis')
    def save_analysis(request: Request, enabled: str = Form('0')):
        rt.store.set_setting('analysis_enabled', '1' if enabled == '1' else '0')
        flash(request, '文章分析已' + ('开启' if enabled == '1' else '关闭'), 'success')
        return RedirectResponse('/admin/settings#analysis', status_code=303)

    @app.post('/admin/settings/models')
    async def save_models(request: Request):
        form = {k: str(v).strip() for k, v in (await request.form()).items()}
        current = rt.settings
        errors: list[str] = []

        def url(key: str) -> str:
            value = form.get(key, '') or getattr(current, key)
            if not re.match(r'^https?://[^\s]+$', value):
                errors.append(f'{key} 必须是 http(s) 地址')
            return value.rstrip('/')

        def text(key: str) -> str:
            value = form.get(key, '') or getattr(current, key)
            if not re.match(r'^[\w./:@+\-]{1,120}$', value):
                errors.append(f'{key} 含非法字符')
            return value

        def number(key: str, cast, lo, hi, optional=False):
            raw = form.get(key, '')
            if raw == '':
                return None if optional else getattr(current, key)
            try:
                value = cast(raw)
            except ValueError:
                errors.append(f'{key} 不是有效数字')
                return getattr(current, key)
            if not lo <= value <= hi:
                errors.append(f'{key} 需在 {lo}-{hi} 之间')
            return value

        values = {
            'llm_base_url': url('llm_base_url'), 'llm_model': text('llm_model'),
            'llm_concurrency': number('llm_concurrency', int, 1, 16),
            'embedding_base_url': url('embedding_base_url'), 'embedding_model': text('embedding_model'),
            'cluster_high': number('cluster_high', float, 0.3, 0.99, optional=True),
            'cluster_low': number('cluster_low', float, 0.2, 0.95, optional=True),
            'min_impact': number('min_impact', int, 1, 5), 'max_events': number('max_events', int, 5, 200),
        }
        if 'public_base_url' in form:
            public_url = form['public_base_url'].rstrip('/')
            if public_url:
                from .digest import render_push
                try:
                    render_push([], public_url)
                except ValueError:
                    errors.append('简报公网地址必须是 http(s) 地址，不含账号密码、查询参数或片段')
            values['public_base_url'] = public_url
        for secret_key in ('llm_api_key', 'embedding_api_key'):
            if form.get(secret_key):
                if any(c in form[secret_key] for c in '\r\n '):
                    errors.append(f'{secret_key} 含空白字符')
                values[secret_key] = form[secret_key]
        if values['cluster_high'] is not None and values['cluster_low'] is not None and values['cluster_low'] >= values['cluster_high']:
            errors.append('合并阈值下限必须小于上限')
        if errors:
            flash(request, '；'.join(errors), 'error')
            return RedirectResponse('/admin/settings#models', status_code=303)
        env = {f'NEWS_{k.upper()}': (None if v is None else str(v)) for k, v in values.items()}
        update_env_file(settings.env_path, env)
        updated = current.model_copy(update=values)
        rt.apply_settings(updated)
        flash(request, '模型与向量配置已保存到 .env，立即生效', 'success')
        return RedirectResponse('/admin/settings#models', status_code=303)

    @app.post('/admin/settings/test/{target}')
    def test_connection(request: Request, target: str):
        if target not in ('llm', 'embedding'):
            raise HTTPException(404)
        ok, message = rt.test_llm() if target == 'llm' else rt.test_embedding()
        results = request.session.get('conn_test', {})
        results[target] = {'ok': ok, 'message': message}
        request.session['conn_test'] = results
        return RedirectResponse('/admin/settings#models', status_code=303)

    @app.post('/admin/password')
    def change_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
        user = request.state.user
        if password_pinned and user['username'] == settings.admin_username:
            flash(request, '管理员密码由环境变量 NEWS_ADMIN_PASSWORD 设置，请修改 .env', 'error')
        elif not verify_password(current, user['password_hash']):
            flash(request, '当前密码不正确', 'error')
        elif new != confirm:
            flash(request, '两次输入的新密码不一致', 'error')
        elif problem := validate_password(new):
            flash(request, problem, 'error')
        else:
            rt.store.update_user(user['id'], password_hash=hash_password(new))
            fresh = rt.store.get_user(user['id'])
            request.session['sv'] = fresh['session_version']
            if user['username'] == settings.admin_username:
                password_file.unlink(missing_ok=True)
            flash(request, '密码已修改', 'success')
        return RedirectResponse('/admin/settings', status_code=303)

    @app.get('/admin/users', response_class=HTMLResponse)
    def users_page(request: Request, log_user: int | None = None):
        return page(
            request, 'users.html', users=rt.store.list_users(), roles=ROLES,
            credentials=request.session.pop('new_credentials', None),
            access=rt.store.list_access(limit=200, user_id=log_user), log_user=log_user,
        )

    def show_credentials(request: Request, username: str, password: str, action: str) -> None:
        # 只放在会话里显示一次，不入库、不写日志
        request.session['new_credentials'] = {'username': username, 'password': password, 'action': action}

    @app.post('/admin/users')
    def create_user(request: Request, username: str = Form(''), password: str = Form(''), role: str = Form('viewer')):
        username = username.strip()
        generated = not password
        if not username:
            username = generate_username()
            while rt.store.get_user_by_name(username):
                username = generate_username()
        password = password or generate_password()
        problem = validate_username(username) or validate_password(password) or (None if role in ROLES else '角色无效')
        if problem is None and rt.store.get_user_by_name(username):
            problem = '用户名已存在'
        if problem:
            flash(request, problem, 'error')
        else:
            rt.store.create_user(username, hash_password(password), role)
            if generated:
                show_credentials(request, username, password, f'已创建{ROLES[role]}')
            else:
                flash(request, f'已创建{ROLES[role]}「{username}」', 'success')
        return RedirectResponse('/admin/users', status_code=303)

    def other_user(request: Request, user_id: int) -> dict | None:
        target = rt.store.get_user(user_id)
        if target is None:
            flash(request, '用户不存在', 'error')
        elif target['id'] == request.state.user['id']:
            flash(request, '不能对自己执行此操作', 'error')
            return None
        return target

    @app.post('/admin/users/{user_id}/role')
    def change_role(request: Request, user_id: int, role: str = Form(...)):
        target = other_user(request, user_id)
        if target and role in ROLES:
            rt.store.update_user(user_id, role=role)
            flash(request, f"「{target['username']}」已改为{ROLES[role]}", 'success')
        return RedirectResponse('/admin/users', status_code=303)

    @app.post('/admin/users/{user_id}/toggle')
    def toggle_user(request: Request, user_id: int):
        target = other_user(request, user_id)
        if target:
            rt.store.update_user(user_id, enabled=0 if target['enabled'] else 1)
            flash(request, f"「{target['username']}」已{'停用' if target['enabled'] else '启用'}", 'success')
        return RedirectResponse('/admin/users', status_code=303)

    @app.post('/admin/users/{user_id}/password')
    def reset_password(request: Request, user_id: int, password: str = Form('')):
        target = other_user(request, user_id)
        if target:
            generated = not password
            password = password or generate_password()
            if problem := validate_password(password):
                flash(request, problem, 'error')
            else:
                rt.store.update_user(user_id, password_hash=hash_password(password))
                if generated:
                    show_credentials(request, target['username'], password, '已重置密码（原登录已失效）')
                else:
                    flash(request, f"已重置「{target['username']}」的密码，其已登录的会话失效", 'success')
        return RedirectResponse('/admin/users', status_code=303)

    @app.post('/admin/users/{user_id}/delete')
    def delete_user(request: Request, user_id: int):
        target = other_user(request, user_id)
        if target:
            rt.store.delete_user(user_id)
            flash(request, f"已删除用户「{target['username']}」", 'success')
        return RedirectResponse('/admin/users', status_code=303)

    sync_schedule()
    return app


