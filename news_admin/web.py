"""FastAPI 后台：新闻汇总与简报（公开）+ 管理页面（需登录）。所有时间均为北京时间（东八区）。"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import tz
from .digest import article_times, build_sections, event_region_label
from .fetch import SOURCE_KINDS
from .models import CONTENT_TYPE_LABELS, REGION_LABELS, TOPIC_LABELS, Topic
from .pipeline import PipelineBusy
from .push import CHANNEL_KINDS
from .rank import GRADE_LABELS, rate_event
from .runtime import Runtime, build_runtime
from .store import FEED_RANGES, FEED_SORTS
from .timefmt import created_updated, format_time

BASE = Path(__file__).resolve().parent
PUBLIC_PREFIXES = ('/briefs', '/static', '/login', '/healthz', '/thumb/', '/favicon.ico')
PER_PAGE = 20
MIN_PASSWORD = 8


def highlight(text: str, q: str):
    """转义后把关键词包上 <mark>（不区分大小写）。"""
    from markupsafe import Markup, escape

    safe = str(escape(text or ''))
    terms = sorted({str(escape(t)) for t in (q or '').split() if t}, key=len, reverse=True)
    if not terms:
        return Markup(safe)
    pattern = re.compile('|'.join(re.escape(t) for t in terms), re.IGNORECASE)
    return Markup(pattern.sub(lambda m: f'<mark>{m.group(0)}</mark>', safe))


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
    password_file = settings.data_dir / 'admin_password.txt'
    password_pinned = bool(settings.admin_password)
    if not password_pinned and not rt.store.get_setting('admin_password_hash') and not rt.store.get_setting('admin_password'):
        initial = secrets.token_urlsafe(12)
        rt.store.set_setting('admin_password', initial)
        password_file.write_text(initial, encoding='utf-8')

    def check_password(password: str) -> bool:
        if password_pinned:
            return hmac.compare_digest(password, settings.admin_password)
        stored_hash = rt.store.get_setting('admin_password_hash')
        if stored_hash:
            return hmac.compare_digest(_hash(password, stored_hash.split('$')[0]), stored_hash)
        return hmac.compare_digest(password, rt.store.get_setting('admin_password'))

    scheduler = BackgroundScheduler(timezone=tz.TZ)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_scheduler:
            scheduler.start()
            sync_schedule()
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)

    app = FastAPI(title='News Admin', docs_url=None, redoc_url=None, lifespan=lifespan)
    templates = Jinja2Templates(directory=str(BASE / 'templates'))
    templates.env.globals.update(
        format_time=format_time, created_updated=created_updated, article_times=article_times, local_time=local_time, duration=duration,
        source_kinds=SOURCE_KINDS, topic_labels=TOPIC_LABELS, region_labels=REGION_LABELS, region_label=event_region_label,
        content_type_labels=CONTENT_TYPE_LABELS, channel_kinds=CHANNEL_KINDS, grade_labels=GRADE_LABELS,
        feed_ranges=FEED_RANGES, feed_sorts=FEED_SORTS, fetch_windows=tz.FETCH_WINDOWS,
    )
    templates.env.filters['highlight'] = highlight
    app.mount('/static', StaticFiles(directory=str(BASE / 'static')), name='static')

    def run_in_background(trigger: str) -> bool:
        if rt.pipeline.busy:
            return False

        def worker() -> None:
            try:
                rt.run_and_deliver(trigger)
            except PipelineBusy:
                pass
            except Exception:  # 已在 runs 表中记录失败
                pass

        threading.Thread(target=worker, daemon=True).start()
        return True

    def sync_schedule() -> None:
        cron = rt.store.get_setting('schedule_cron', '0 8,18 * * *')
        enabled = rt.store.get_setting('schedule_enabled', '0') == '1'
        if scheduler.get_job('pipeline'):
            scheduler.remove_job('pipeline')
        if enabled:
            scheduler.add_job(lambda: run_in_background('schedule'), CronTrigger.from_crontab(cron, timezone=tz.TZ), id='pipeline')

    def next_run_time() -> str:
        job = scheduler.get_job('pipeline') if scheduler.running else None
        return job.next_run_time.astimezone(tz.TZ).strftime('%m-%d %H:%M') if job and job.next_run_time else ''

    @app.middleware('http')
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path == '/' or path.startswith(PUBLIC_PREFIXES) or request.session.get('admin'):
            return await call_next(request)
        return RedirectResponse(f'/login?next={path}', status_code=303)

    app.add_middleware(SessionMiddleware, secret_key=secret, same_site='lax', max_age=12 * 3600, https_only=False)

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {'rt': rt, 'admin': request.session.get('admin', False), 'path': request.url.path, **context}
        )

    def flash(request: Request, message: str, kind: str = 'info') -> None:
        request.session['flash'] = {'text': message, 'kind': kind}

    def ensure_rating(event, at) -> None:
        if event.rating is None:
            event.rating = rate_event(event, at)

    # ---------- public ----------
    @app.get('/healthz')
    def healthz() -> dict[str, Any]:
        latest = rt.store.latest_completed_run()
        return {'ok': True, 'busy': rt.pipeline.busy, 'latest_run': latest['id'] if latest else None, 'time': tz.iso()}

    @app.get('/', response_class=HTMLResponse)
    def index(request: Request, q: str = '', range: str = 'all', sort: str = 'time', topic: str = ''):
        q = ' '.join(q.split())[:60]
        range_ = range if range in FEED_RANGES else 'all'
        sort = sort if sort in FEED_SORTS else 'time'
        topic = topic if topic in {t.value for t in Topic} else ''
        raw = request.query_params.get('page', '1')
        current = int(raw) if raw.isdigit() and int(raw) > 0 else 1
        feed = rt.store.feed(range_=range_, q=q, page=current, per_page=PER_PAGE, sort=sort, topic=topic)
        if feed.total and feed.page > feed.pages:
            feed = rt.store.feed(range_=range_, q=q, page=feed.pages, per_page=PER_PAGE, sort=sort, topic=topic)
        now = tz.now()
        for item in feed.items:
            ensure_rating(item.event, now)
        defaults = {'q': '', 'range': 'all', 'sort': 'time', 'topic': '', 'page': 1}

        def url(**override: Any) -> str:
            merged = {'q': q, 'range': range_, 'sort': sort, 'topic': topic, 'page': 1, **override}
            query = {k: v for k, v in merged.items() if v != defaults[k]}
            return '/' + ('?' + urlencode(query) if query else '')

        return page(
            request, 'feed.html', feed=feed, q=q, range_=range_, sort=sort, topic=topic, url=url,
            topic_counts=rt.store.topic_counts(range_=range_, q=q), latest=rt.store.latest_completed_run(),
            retention_days=int(rt.store.get_setting('retention_days', str(settings.retention_days))),
        )

    @app.get('/favicon.ico', include_in_schema=False)
    def favicon():
        return RedirectResponse('/static/favicon.svg', status_code=301)

    @app.get('/briefs', response_class=HTMLResponse)
    def brief_list(request: Request):
        return page(request, 'briefs.html', briefs=rt.store.brief_index())

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
        return page(request, 'brief.html', run=run, sections=build_sections(events))

    @app.get('/thumb/{key}')
    def thumb(key: str):
        found = rt.thumbnails.read(key)
        if not found:
            raise HTTPException(404)
        data, mime = found
        return Response(data, media_type=mime, headers={'Cache-Control': 'public, max-age=604800, immutable'})

    @app.get('/login', response_class=HTMLResponse)
    def login_form(request: Request, next: str = '/admin'):
        return page(request, 'login.html', next=next, password_pinned=password_pinned, initial_file=password_file.exists())

    @app.post('/login')
    def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form('/admin')):
        if hmac.compare_digest(username, settings.admin_username) and check_password(password):
            request.session['admin'] = True
            return RedirectResponse(next if next.startswith('/') and not next.startswith('//') else '/admin', status_code=303)
        flash(request, '用户名或密码错误', 'error')
        return RedirectResponse('/login', status_code=303)

    @app.post('/logout')
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse('/', status_code=303)

    # ---------- admin ----------
    @app.get('/admin', response_class=HTMLResponse)
    def dashboard(request: Request):
        runs = rt.store.list_runs(30)
        return page(
            request, 'dashboard.html', runs=runs, busy=rt.pipeline.busy, next_run=next_run_time(),
            schedule_cron=rt.store.get_setting('schedule_cron', '0 8,18 * * *'),
            schedule_enabled=rt.store.get_setting('schedule_enabled', '0') == '1',
            sources=rt.store.list_sources(), health=rt.store.source_health(), channels=rt.store.list_channels(),
            fetch_window=rt.store.get_setting('fetch_window', settings.fetch_window),
        )

    @app.post('/admin/run')
    def trigger_run(request: Request):
        started = run_in_background('manual')
        flash(request, '已开始运行，约 3-5 分钟完成，可刷新查看进度' if started else '已有任务在运行', 'success' if started else 'error')
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

    @app.get('/admin/runs/{run_id}', response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        run = rt.store.get_run(run_id)
        if not run:
            raise HTTPException(404)
        return page(request, 'run.html', run=run, events=rt.store.load_events(run_id), deliveries=rt.store.deliveries(run_id))

    @app.post('/admin/runs/{run_id}/deliver')
    def redeliver(request: Request, run_id: int):
        results = rt.deliver(run_id)
        flash(request, '；'.join(f"{r['channel']}：{'成功' if r['status'] == 'sent' else '失败'}" for r in results) or '没有启用的推送渠道')
        return RedirectResponse(f'/admin/runs/{run_id}', status_code=303)

    @app.get('/admin/sources', response_class=HTMLResponse)
    def sources(request: Request):
        return page(request, 'sources.html', sources=rt.store.list_sources(), health=rt.store.source_health())

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

    @app.get('/admin/channels', response_class=HTMLResponse)
    def channels(request: Request):
        return page(request, 'channels.html', channels=rt.store.list_channels())

    @app.post('/admin/channels')
    async def save_channel(request: Request):
        form = await request.form()
        kind = str(form.get('kind', ''))
        if kind not in CHANNEL_KINDS:
            raise HTTPException(400)
        channel_id = int(form['channel_id']) if form.get('channel_id') else None
        existing = rt.store.get_channel(channel_id) if channel_id else None
        config = {}
        for field in CHANNEL_KINDS[kind]['fields']:
            value = str(form.get(field, '')).strip()
            if not value and existing and field in existing['config']:
                value = existing['config'][field]  # 留空表示不修改已保存的密钥
            config[field] = value
        rt.store.upsert_channel(channel_id, kind, str(form.get('name', '')).strip() or CHANNEL_KINDS[kind]['label'], config, form.get('enabled') == '1')
        flash(request, '渠道已保存', 'success')
        return RedirectResponse('/admin/channels', status_code=303)

    @app.post('/admin/channels/{channel_id}/delete')
    def delete_channel(channel_id: int):
        rt.store.delete_channel(channel_id)
        return RedirectResponse('/admin/channels', status_code=303)

    @app.get('/admin/settings', response_class=HTMLResponse)
    def settings_page(request: Request):
        return page(
            request, 'settings.html', password_pinned=password_pinned,
            fetch_window=rt.store.get_setting('fetch_window', settings.fetch_window),
            retention_days=rt.store.get_setting('retention_days', str(settings.retention_days)),
        )

    @app.post('/admin/settings')
    def save_settings(request: Request, fetch_window: str = Form(...), retention_days: str = Form(...)):
        if fetch_window not in tz.FETCH_WINDOWS or not retention_days.isdigit() or not 7 <= int(retention_days) <= 365:
            flash(request, '参数无效：保留天数需在 7-365 之间', 'error')
            return RedirectResponse('/admin/settings', status_code=303)
        rt.store.set_setting('fetch_window', fetch_window)
        rt.store.set_setting('retention_days', retention_days)
        flash(request, '设置已保存，下次运行生效', 'success')
        return RedirectResponse('/admin/settings', status_code=303)

    @app.post('/admin/password')
    def change_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
        if password_pinned:
            flash(request, '密码由环境变量 NEWS_ADMIN_PASSWORD 设置，请修改 .env', 'error')
        elif not check_password(current):
            flash(request, '当前密码不正确', 'error')
        elif new != confirm:
            flash(request, '两次输入的新密码不一致', 'error')
        elif len(new) < MIN_PASSWORD:
            flash(request, f'新密码至少 {MIN_PASSWORD} 位', 'error')
        else:
            rt.store.set_setting('admin_password_hash', _hash(new))
            rt.store.set_setting('admin_password', '')
            password_file.unlink(missing_ok=True)
            flash(request, '密码已修改，初始密码文件已删除', 'success')
        return RedirectResponse('/admin/settings', status_code=303)

    return app


def _hash(password: str, salt: str | None = None) -> str:
    import hashlib

    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f'{salt}${digest}'
