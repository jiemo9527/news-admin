from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from urllib.parse import unquote
from typing import Any

from .auth import BOOTSTRAP_PATH, get_admin_auth, update_admin_password, verify_admin_password
from .backup import create_backup_archive, import_backup_archive
from .cleanup import cleanup_old_data
from .config import Paths, SECRET_KEY, TIMEZONE
from .db import init_db
from .repository import (
    get_dashboard_stats,
    get_latest_publication,
    get_platform,
    get_publication_by_token,
    get_recipient,
    get_run,
    get_schedule,
    get_source,
    list_deliveries,
    list_activity_rows,
    list_platforms,
    list_publications,
    list_recipients,
    list_run_source_results,
    list_runs,
    list_schedules,
    list_sources,
    list_story_groups,
    set_schedule_enabled,
    set_recipient_enabled,
    seed_defaults,
    create_or_update_platform,
    create_or_update_recipient,
    create_or_update_schedule,
    delete_platform,
    delete_recipient,
    delete_schedule,
    create_source,
    delete_source,
    update_source,
    set_source_enabled,
)
from .scheduler import start_scheduler, sync_scheduler
from .service import is_background_cleanup_active, is_pipeline_busy, recover_stale_runs, send_latest_publication_to_recipient, start_cleanup_in_background, start_pipeline_in_background
from .thumbnail_cache import get_cached_thumbnail


def create_app(start_jobs: bool = True) -> Any:
    init_db()
    seed_defaults()
    flask = importlib.import_module('flask')
    Flask = flask.Flask
    abort = flask.abort
    flash = flask.flash
    redirect = flask.redirect
    render_template = flask.render_template
    request = flask.request
    send_file = flask.send_file
    session = flask.session
    url_for = flask.url_for
    app = Flask(__name__, template_folder=str(Paths.base_dir / 'templates'), static_folder=str(Paths.base_dir / 'static'))
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

    def is_public_request() -> bool:
        endpoint = request.endpoint or ''
        if endpoint in {'static', 'login', 'published_latest', 'published', 'thumb_proxy'}:
            return True
        return request.path.startswith('/published/')

    @app.before_request
    def require_admin_auth():
        get_admin_auth()
        if is_public_request():
            return None
        if session.get('admin_authenticated'):
            session.permanent = True
            return None
        next_path = request.full_path if request.query_string else request.path
        return redirect(url_for('login', next=next_path))

    @app.template_filter('datetime_display')
    def datetime_display(value: object) -> str:
        if value in (None, ''):
            return '-'
        try:
            current = datetime.fromisoformat(str(value))
            if not any([current.hour, current.minute, current.second]):
                return '-'
            if current.second == 0:
                return current.strftime('%Y-%m-%d %H:%M')
            return current.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            return str(value)

    @app.context_processor
    def inject_globals():
        auth = get_admin_auth()
        try:
            static_version = int((Paths.base_dir / 'static' / 'style.css').stat().st_mtime)
        except OSError:
            static_version = 0
        return {
            'timezone_name': TIMEZONE,
            'is_authenticated': bool(session.get('admin_authenticated')),
            'admin_username': str(session.get('admin_username') or auth.get('username', 'admin')),
            'static_version': static_version,
        }

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        next_path = request.args.get('next', '') or request.form.get('next', '') or url_for('dashboard')
        auth = get_admin_auth()
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            if username == str(auth['username']) and verify_admin_password(password):
                session.clear()
                session['admin_authenticated'] = True
                session['admin_username'] = username
                session.permanent = True
                if BOOTSTRAP_PATH.exists():
                    flash('当前仍在使用首次生成的管理员密码，建议登录后立刻到设置页修改密码。')
                return redirect(next_path if next_path.startswith('/') else url_for('dashboard'))
            flash('管理员账号或密码错误。')
        return render_template('login.html', next_path=next_path, bootstrap_exists=BOOTSTRAP_PATH.exists())

    @app.post('/logout')
    def logout():
        session.clear()
        flash('已退出登录。')
        return redirect(url_for('login'))

    @app.get('/')
    def dashboard():
        return render_template('dashboard.html', stats=get_dashboard_stats(), runs=list_runs(8), latest=get_latest_publication())

    @app.get('/connections')
    def connections():
        return render_template('connections.html', platforms=list_platforms(), recipients=list_recipients(), latest=get_latest_publication())

    @app.get('/activity')
    def activity():
        return render_template('activity.html', activity_rows=list_activity_rows(50), latest=get_latest_publication())

    @app.get('/settings')
    def settings():
        _ = recover_stale_runs()
        recent_runs = list_runs(6)
        run_busy = is_pipeline_busy()
        cleanup_busy = is_background_cleanup_active()
        return render_template('settings.html', schedules=list_schedules(), run_busy=run_busy, cleanup_busy=cleanup_busy, bootstrap_exists=BOOTSTRAP_PATH.exists())

    @app.post('/settings/password')
    def settings_password():
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        if not verify_admin_password(current_password):
            flash('当前管理员密码不正确。')
            return redirect(url_for('settings'))
        if len(new_password) < 12:
            flash('新密码至少需要 12 位。')
            return redirect(url_for('settings'))
        if new_password != confirm_password:
            flash('两次输入的新密码不一致。')
            return redirect(url_for('settings'))
        auth = update_admin_password(new_password, str(session.get('admin_username') or get_admin_auth()['username']))
        session['admin_username'] = auth['username']
        flash('管理员密码已更新。')
        return redirect(url_for('settings'))

    @app.post('/settings/backup')
    def settings_backup():
        archive_path = create_backup_archive()
        return send_file(archive_path, as_attachment=True, download_name=archive_path.name)

    @app.post('/settings/import')
    def settings_import():
        uploaded = request.files.get('backup_file')
        if uploaded is None or not uploaded.filename:
            flash('请先选择一个备份 zip 文件。')
            return redirect(url_for('settings'))
        temp_file = tempfile.NamedTemporaryFile(prefix='news-admin-upload-', suffix='.zip', delete=False)
        upload_path = Path(temp_file.name)
        temp_file.close()
        try:
            uploaded.save(str(upload_path))
            import_backup_archive(upload_path)
            init_db()
            seed_defaults()
            sync_scheduler()
            flash('备份导入完成，系统数据已恢复。若迁移了密钥或管理员配置，建议重新登录确认。')
        except Exception as exc:
            flash(f'备份导入失败：{exc}')
        finally:
            Path(upload_path).unlink(missing_ok=True)
        return redirect(url_for('settings'))

    @app.route('/sources')
    def sources():
        return render_template('sources.html', sources=list_sources())

    @app.route('/sources/new', methods=['GET', 'POST'])
    def source_new():
        if request.method == 'POST':
            create_source(request.form)
            flash('来源已创建')
            return redirect(url_for('sources'))
        return render_template('source_form.html', source=None)

    @app.route('/sources/<int:source_id>/edit', methods=['GET', 'POST'])
    def source_edit(source_id: int):
        source = get_source(source_id)
        if not source:
            abort(404)
        if request.method == 'POST':
            update_source(source_id, request.form)
            flash('来源已更新')
            return redirect(url_for('sources'))
        return render_template('source_form.html', source=source)

    @app.post('/sources/<int:source_id>/toggle')
    def source_toggle(source_id: int):
        source = get_source(source_id)
        if not source:
            abort(404)
        assert source is not None
        enabled = not bool(source['enabled'])
        set_source_enabled(source_id, enabled)
        flash('来源已启用' if enabled else '来源已停用')
        return redirect(url_for('sources'))

    @app.post('/sources/<int:source_id>/delete')
    def source_delete(source_id: int):
        source = get_source(source_id)
        if not source:
            abort(404)
        delete_source(source_id)
        flash('来源已删除')
        return redirect(url_for('sources'))

    @app.route('/platforms', methods=['GET'])
    def platforms():
        return redirect(url_for('connections'))

    @app.route('/platforms/new', methods=['GET', 'POST'])
    def platform_new():
        if request.method == 'POST':
            create_or_update_platform(None, request.form)
            flash('平台已创建')
            return redirect(url_for('platforms'))
        return render_template('platform_form.html', platform=None)

    @app.route('/platforms/<int:platform_id>/edit', methods=['GET', 'POST'])
    def platform_edit(platform_id: int):
        platform = get_platform(platform_id)
        if not platform:
            abort(404)
        if request.method == 'POST':
            create_or_update_platform(platform_id, request.form)
            flash('平台已更新')
            return redirect(url_for('platforms'))
        return render_template('platform_form.html', platform=platform)

    @app.post('/platforms/<int:platform_id>/delete')
    def platform_delete(platform_id: int):
        platform = get_platform(platform_id)
        if not platform:
            abort(404)
        delete_platform(platform_id)
        flash('平台已删除。其关联接收人会一并删除。')
        return redirect(url_for('connections'))

    @app.route('/recipients', methods=['GET'])
    def recipients():
        return redirect(url_for('connections'))

    @app.route('/recipients/new', methods=['GET', 'POST'])
    def recipient_new():
        platforms = list_platforms()
        if request.method == 'POST':
            create_or_update_recipient(None, request.form)
            flash('接收人已创建')
            return redirect(url_for('recipients'))
        return render_template('recipient_form.html', recipient=None, platforms=platforms)

    @app.route('/recipients/<int:recipient_id>/edit', methods=['GET', 'POST'])
    def recipient_edit(recipient_id: int):
        recipient = get_recipient(recipient_id)
        if not recipient:
            abort(404)
        platforms = list_platforms()
        if request.method == 'POST':
            create_or_update_recipient(recipient_id, request.form)
            flash('接收人已更新')
            return redirect(url_for('recipients'))
        return render_template('recipient_form.html', recipient=recipient, platforms=platforms)

    @app.post('/recipients/<int:recipient_id>/send-latest')
    def recipient_send_latest(recipient_id: int):
        recipient = get_recipient(recipient_id)
        if not recipient:
            abort(404)
        latest_publication = get_latest_publication()
        try:
            result = send_latest_publication_to_recipient(recipient_id, request.url_root.rstrip('/'))
            flash(f"手动触发成功：delivery #{result['delivery_id'] or 'n/a'} · Web阅读：{result['publication_web_url']}")
            return redirect(url_for('run_detail', run_id=result['run_id']))
        except Exception as exc:
            flash(f'手动触发失败：{exc}')
            if latest_publication:
                return redirect(url_for('run_detail', run_id=latest_publication['run_id']))
        return redirect(url_for('connections'))

    @app.post('/recipients/<int:recipient_id>/toggle')
    def recipient_toggle(recipient_id: int):
        recipient = get_recipient(recipient_id)
        if not recipient:
            abort(404)
        assert recipient is not None
        enabled = not bool(recipient['enabled'])
        set_recipient_enabled(recipient_id, enabled)
        flash('接收人已启用' if enabled else '接收人已禁用')
        return redirect(url_for('connections'))

    @app.post('/recipients/<int:recipient_id>/delete')
    def recipient_delete(recipient_id: int):
        recipient = get_recipient(recipient_id)
        if not recipient:
            abort(404)
        delete_recipient(recipient_id)
        flash('接收人已删除。')
        return redirect(url_for('connections'))

    @app.route('/schedules', methods=['GET'])
    def schedules():
        return redirect(url_for('settings'))

    @app.route('/schedules/new', methods=['GET', 'POST'])
    def schedule_new():
        if request.method == 'POST':
            create_or_update_schedule(None, request.form)
            sync_scheduler()
            flash('计划任务已创建')
            return redirect(url_for('schedules'))
        return render_template('schedule_form.html', schedule=None)

    @app.route('/schedules/<int:schedule_id>/edit', methods=['GET', 'POST'])
    def schedule_edit(schedule_id: int):
        schedule = get_schedule(schedule_id)
        if not schedule:
            abort(404)
        if request.method == 'POST':
            create_or_update_schedule(schedule_id, request.form)
            sync_scheduler()
            flash('计划任务已更新')
            return redirect(url_for('schedules'))
        return render_template('schedule_form.html', schedule=schedule)

    @app.post('/schedules/<int:schedule_id>/toggle')
    def schedule_toggle(schedule_id: int):
        schedule = get_schedule(schedule_id)
        if not schedule:
            abort(404)
        assert schedule is not None
        enabled = not bool(schedule['enabled'])
        set_schedule_enabled(schedule_id, enabled)
        sync_scheduler()
        flash('计划任务已启用' if enabled else '计划任务已禁用')
        return redirect(url_for('settings'))

    @app.post('/schedules/<int:schedule_id>/delete')
    def schedule_delete(schedule_id: int):
        schedule = get_schedule(schedule_id)
        if not schedule:
            abort(404)
        delete_schedule(schedule_id)
        sync_scheduler()
        flash('计划任务已删除')
        return redirect(url_for('settings'))

    @app.post('/runs/trigger')
    def run_trigger():
        _ = recover_stale_runs()
        if is_pipeline_busy():
            flash('已有任务正在后台执行，请稍后刷新运行记录查看。')
            return redirect(url_for('settings'))
        started = start_pipeline_in_background('manual')
        if started:
            flash('任务已提交后台执行，页面不会等待跑完；请稍后刷新运行记录查看进度。')
        else:
            flash('已有任务正在后台执行，请稍后刷新运行记录查看。')
        return redirect(url_for('settings'))

    @app.post('/maintenance/cleanup')
    def maintenance_cleanup():
        try:
            retention_days = max(0, int(request.form.get('days', '7') or '7'))
        except ValueError:
            retention_days = 7
        if is_background_cleanup_active():
            flash('已有清理任务正在后台执行，请稍后刷新页面查看结果。')
            return redirect(url_for('settings'))
        started = start_cleanup_in_background(retention_days)
        if started:
            flash(f'清理任务已提交后台执行，按保留 {retention_days} 天策略处理；页面不会等待清理完成。')
        else:
            flash('已有清理任务正在后台执行，请稍后刷新页面查看结果。')
        return redirect(url_for('settings'))

    @app.get('/runs')
    def runs():
        return redirect(url_for('activity'))

    @app.get('/runs/<int:run_id>')
    def run_detail(run_id: int):
        run = get_run(run_id)
        if not run:
            abort(404)
        return render_template(
            'run_detail.html',
            run=run,
            source_results=list_run_source_results(run_id),
            groups=list_story_groups(run_id),
            deliveries=list_deliveries(run_id),
        )

    @app.get('/publications')
    def publications():
        return redirect(url_for('activity'))

    @app.get('/published/latest')
    def published_latest():
        publication = get_latest_publication()
        if not publication:
            abort(404)
        assert publication is not None
        return send_file(Paths.data_dir / publication['latest_relpath'])

    @app.get('/published/<token>')
    def published(token: str):
        publication = get_publication_by_token(token)
        if not publication:
            abort(404)
        assert publication is not None
        return send_file(Paths.data_dir / publication['relpath'])

    @app.get('/thumb')
    def thumb_proxy():
        source_url = unquote(request.args.get('src', '')).strip()
        if not source_url:
            abort(404)
        file_path = Paths.thumbnails_dir / 'missing.jpg'
        content_type = 'image/jpeg'
        try:
            file_path, content_type = get_cached_thumbnail(source_url)
        except Exception:
            abort(404)
        return send_file(file_path, mimetype=content_type)

    if start_jobs:
        start_scheduler()

    return app
