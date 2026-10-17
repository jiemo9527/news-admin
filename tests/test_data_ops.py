import io
import json
import zipfile

import pytest

from news_admin.auth import hash_password
from news_admin.config import Settings
from news_admin.runtime import build_runtime
from news_admin.data_ops import export_backup, restore_backup, clear_data
from tests.test_store import make_event


def runtime(tmp_path):
    return build_runtime(Settings(data_dir=tmp_path / 'data', env_path=tmp_path / '.env', _env_file=None))


def seed(rt):
    admin = rt.store.create_user('admin', hash_password('admin-password'), 'admin')
    reader = rt.store.create_user('reader', hash_password('reader-password'), 'viewer')
    rid = rt.store.start_run('test')
    rt.store.save_events(rid, [make_event()])
    rt.store.finish_run(rid, 'completed', {})
    rt.store.log_access(reader, 'reader', 'login', '127.0.0.1')
    rt.store.set_setting('schedule_cron', '2 */2 * * *')
    rt.store.set_setting('schedule_enabled', '1')
    rt.settings.env_path.write_text('NEWS_LLM_MODEL=backup-model\nNEWS_LLM_API_KEY=private-key\n', encoding='utf-8')
    thumb = rt.settings.data_dir / 'thumbs' / ('a' * 32 + '.webp')
    thumb.write_bytes(b'test-thumb')
    return admin, reader, rid


def test_manual_backup_round_trip_including_accounts_settings_and_images(tmp_path):
    rt = runtime(tmp_path)
    admin, reader, rid = seed(rt)
    archive = export_backup(rt)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        assert 'manifest.json' in z.namelist() and 'news.db' in z.namelist() and 'config.env' in z.namelist()
        assert not any('cache/' in n or 'backup' in n for n in z.namelist())
    before = rt.store.get_user(admin)['password_hash']
    clear_data(rt)
    assert rt.store.get_user(reader) is None and rt.store.get_run(rid) is None
    result = restore_backup(rt, archive)
    assert result['runs'] == 1 and result['users'] == 2
    assert rt.store.get_run(rid)['status'] == 'completed'
    assert rt.store.get_user(admin)['password_hash'] == before
    assert rt.store.get_user(reader) is not None
    assert rt.settings.llm_model == 'backup-model'
    assert rt.settings.llm_api_key == 'private-key'
    assert rt.store.get_setting('schedule_cron') == '2 */2 * * *'
    assert rt.thumbnails.read('a' * 32)[0] == b'test-thumb'
    assert not list(rt.settings.data_dir.glob('backups/*'))  # 任何操作都不会创建自动备份


def test_clear_keeps_all_admins_sources_channels_model_and_schedules(tmp_path):
    rt = runtime(tmp_path)
    admin, reader, rid = seed(rt)
    second = rt.store.create_user('manager', hash_password('another-password'), 'admin')
    rt.store.upsert_channel(None, 'telegram', 'test', {'bot_token': 'x', 'chat_id': '1'})
    old_hash = rt.store.get_user(admin)['password_hash']
    cache = rt.settings.cache_dir / 'llm'; cache.mkdir(parents=True, exist_ok=True)
    (cache / 'x.json').write_text('{}')
    sources = len(rt.store.list_sources())
    clear_data(rt)
    assert rt.store.get_user(admin)['password_hash'] == old_hash
    assert rt.store.get_user(second) is not None and rt.store.get_user(reader) is None
    assert len(rt.store.list_sources()) == sources and rt.store.list_channels()[0]['name'] == 'test'
    assert rt.store.get_setting('schedule_cron') == '2 */2 * * *'
    for table in ('runs', 'events', 'event_urls', 'source_results', 'llm_calls', 'deliveries', 'access_log'):
        assert rt.store._rows(f'SELECT COUNT(*) AS n FROM {table}')[0]['n'] == 0
    assert not list(cache.glob('*')) and not list(rt.thumbnails.directory.glob('*'))
    assert 'private-key' in rt.settings.env_path.read_text()


def test_corrupt_or_unsafe_archives_are_rejected_without_changes(tmp_path):
    rt = runtime(tmp_path)
    _, _, rid = seed(rt)
    for name, content in [('broken.zip', b'bad'), ('../news.db', b'bad'), ('news.db', b'not sqlite')]:
        if name == 'broken.zip':
            payload = content
        else:
            b = io.BytesIO()
            with zipfile.ZipFile(b, 'w') as z:
                z.writestr(name, content)
            payload = b.getvalue()
        with pytest.raises(ValueError):
            restore_backup(rt, payload)
        assert rt.store.get_run(rid) is not None
    good = export_backup(rt)
    b = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(b, 'w') as dst:
        for info in src.infolist():
            dst.writestr(info.filename, b'tampered' if info.filename == 'config.env' else src.read(info))
    with pytest.raises(ValueError):
        restore_backup(rt, b.getvalue())
    assert rt.store.get_run(rid) is not None


def test_data_operations_refuse_during_run(tmp_path):
    rt = runtime(tmp_path)
    rt.pipeline._lock.acquire()
    try:
        for fn, args in ((export_backup, ()), (clear_data, ()), (restore_backup, (b'bad',))):
            with pytest.raises(RuntimeError):
                fn(rt, *args)
    finally:
        rt.pipeline._lock.release()



def test_restore_revokes_versions_newer_than_backup(tmp_path):
    rt=runtime(tmp_path);admin, reader, _=seed(rt)
    with rt.store._conn() as conn:
        conn.execute('UPDATE users SET session_version=4')
    payload=export_backup(rt)
    with rt.store._conn() as conn:
        conn.execute('UPDATE users SET session_version=5')
    restore_backup(rt,payload)
    assert rt.store.get_user(admin)['session_version'] > 5
    assert rt.store.get_user(reader)['session_version'] > 5


def test_failed_restore_rolls_back_all_changes_without_retained_backup(tmp_path, monkeypatch):
    from news_admin import data_ops
    rt=runtime(tmp_path);admin,reader,rid=seed(rt)
    payload=export_backup(rt)
    clear_data(rt)
    original_env=rt.settings.env_path.read_bytes()
    thumb=rt.thumbnails.directory / ('b'*32+'.webp');thumb.write_bytes(b'current')
    def broken(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(data_ops,'update_env_file',broken)
    with pytest.raises(ValueError):
        restore_backup(rt,payload)
    assert rt.store.get_run(rid) is None
    assert rt.store.get_user(reader) is None
    assert thumb.read_bytes() == b'current'
    assert rt.settings.env_path.read_bytes() == original_env
    assert not list(rt.settings.data_dir.glob('backups/*'))


def test_restore_validates_schedules_before_applying(tmp_path):
    rt=runtime(tmp_path);_,_,rid=seed(rt)
    rt.store.set_setting('push_cron','not valid')
    payload=export_backup(rt)
    rt.store.set_setting('push_cron','0 6,20 * * *')
    with pytest.raises(ValueError):
        restore_backup(rt,payload)
    assert rt.store.get_run(rid) is not None
    assert rt.store.get_setting('push_cron') == '0 6,20 * * *'
