"""FastAPI 后台：简报阅读（公开）+ 管理页面（需登录）。"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .digest import build_sections, event_region_label
from .fetch import SOURCE_KINDS
from .models import TOPIC_LABELS
from .pipeline import PipelineBusy
from .push import CHANNEL_KINDS
from .runtime import Runtime, build_runtime

BASE = Path(__file__).resolve().parent
PUBLIC_PREFIXES = ('/briefs', '/static', '/login', '/healthz')


def create_app(runtime: Runtime | None = None, start_scheduler: bool = True) -> FastAPI:
    rt = runtime or build_runtime()
    settings = rt.settings
    secret = settings.secret_key or rt.store.get_setting('secret_key')
    if not secret:
        secret = secrets.token_urlsafe(32)
        rt.store.set_setting('secret_key', secret)
    admin_password = settings.admin_password or rt.store.get_setting('admin_password')
    if not admin_password:
        admin_password = secrets.token_urlsafe(12)
        rt.store.set_setting('admin_password', admin_password)
        (settings.data_dir / 'admin_password.txt').write_text(admin_password, encoding='utf-8')

    scheduler = BackgroundScheduler(timezone=settings.timezone)

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
    templates.env.globals.update(source_kinds=SOURCE_KINDS, topic_labels=TOPIC_LABELS, region_label=event_region_label, channel_kinds=CHANNEL_KINDS)
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
            scheduler.add_job(lambda: run_in_background('schedule'), CronTrigger.from_crontab(cron, timezone=settings.timezone), id='pipeline')

    @app.middleware('http')
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path == '/' or path.startswith(PUBLIC_PREFIXES) or request.session.get('admin'):
            return await call_next(request)
        return RedirectResponse(f'/login?next={path}', status_code=303)

    app.add_middleware(SessionMiddleware, secret_key=secret, same_site='lax', max_age=12 * 3600, https_only=False)

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {'rt': rt, 'admin': request.session.get('admin', False), **context})

    def flash(request: Request, message: str) -> None:
        request.session['flash'] = message

    # ---------- public ----------
    @app.get('/healthz')
    def healthz() -> dict[str, Any]:
        latest = rt.store.latest_completed_run()
        return {'ok': True, 'busy': rt.pipeline.busy, 'latest_run': latest['id'] if latest else None}

    @app.get('/', response_class=HTMLResponse)
    def index(request: Request):
        latest = rt.store.latest_completed_run()
        if latest:
            return RedirectResponse(f"/briefs/{latest['id']}", status_code=303)
        return page(request, 'empty.html')

    @app.get('/briefs/{run_id}', response_class=HTMLResponse)
    def brief(request: Request, run_id: int):
        run = rt.store.get_run(run_id)
        if not run or run['status'] != 'completed':
            raise HTTPException(404)
        events = rt.store.load_events(run_id)
        return page(request, 'brief.html', run=run, sections=build_sections(events))

    @app.get('/login', response_class=HTMLResponse)
    def login_form(request: Request, next: str = '/admin'):
        return page(request, 'login.html', next=next)

    @app.post('/login')
    def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form('/admin')):
        if hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(password, admin_password):
            request.session['admin'] = True
            return RedirectResponse(next if next.startswith('/') else '/admin', status_code=303)
        flash(request, '用户名或密码错误')
        return RedirectResponse('/login', status_code=303)

    @app.post('/logout')
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse('/', status_code=303)

    # ---------- admin ----------
    @app.get('/admin', response_class=HTMLResponse)
    def dashboard(request: Request):
        return page(
            request, 'dashboard.html', runs=rt.store.list_runs(20), busy=rt.pipeline.busy,
            schedule_cron=rt.store.get_setting('schedule_cron', '0 8,18 * * *'),
            schedule_enabled=rt.store.get_setting('schedule_enabled', '0') == '1',
        )

    @app.post('/admin/run')
    def trigger_run(request: Request):
        flash(request, '已开始运行' if run_in_background('manual') else '已有任务在运行')
        return RedirectResponse('/admin', status_code=303)

    @app.post('/admin/schedule')
    def save_schedule(request: Request, cron: str = Form(...), enabled: str = Form('0')):
        try:
            CronTrigger.from_crontab(cron)
        except ValueError as exc:
            flash(request, f'Cron 表达式无效：{exc}')
            return RedirectResponse('/admin', status_code=303)
        rt.store.set_setting('schedule_cron', cron.strip())
        rt.store.set_setting('schedule_enabled', '1' if enabled == '1' else '0')
        sync_schedule()
        flash(request, '定时任务已保存')
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
        flash(request, '；'.join(f"{r['channel']}：{r['status']}" for r in results) or '没有启用的推送渠道')
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
                flash(request, f'链接正则无效：{exc}')
                return RedirectResponse('/admin/sources', status_code=303)
            if not link_pattern.strip():
                flash(request, '网页列表源必须填写文章链接正则')
                return RedirectResponse('/admin/sources', status_code=303)
        rt.store.upsert_source(source_id, name.strip(), feed_url.strip(), weight, max_items, fetch_body == '1', kind, link_pattern.strip())
        flash(request, '来源已保存')
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
        flash(request, '渠道已保存')
        return RedirectResponse('/admin/channels', status_code=303)

    @app.post('/admin/channels/{channel_id}/delete')
    def delete_channel(channel_id: int):
        rt.store.delete_channel(channel_id)
        return RedirectResponse('/admin/channels', status_code=303)

    return app
