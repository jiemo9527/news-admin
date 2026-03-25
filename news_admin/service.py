from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import Any

from .aggregator import aggregate_candidates, build_observation_analysis, render_text_digest, sectioned_groups
from .cleanup import cleanup_old_data
from .config import LOCK_PATH, PUBLIC_WEB_BASE_URL, Paths
from .fetchers import fetch_source
from .publish import generate_publication_token, publication_path, render_publication_html, save_publication
from .push import next_retry, send_message
from .text_normalization import normalize_body_punctuation
from .repository import (
    add_run_source_result,
    add_story_evidence,
    create_delivery,
    create_publication,
    create_run,
    create_story_group,
    finish_run,
    get_first_completed_run_on_date,
    get_latest_publication,
    get_pending_deliveries,
    get_platform,
    get_recipient,
    get_run,
    list_platforms,
    list_recipients,
    list_story_groups,
    list_sources,
    mark_running_runs_stale,
    mark_source_health,
    recipient_target_issue,
    update_delivery,
)


BACKGROUND_RUNS_LOCK = Lock()
BACKGROUND_RUNS: set[Thread] = set()
BACKGROUND_CLEANUPS_LOCK = Lock()
BACKGROUND_CLEANUPS: set[Thread] = set()
UPDATE_SECTION_LIMIT = 12


def prune_background_runs() -> None:
    with BACKGROUND_RUNS_LOCK:
        dead = {thread for thread in BACKGROUND_RUNS if not thread.is_alive()}
        BACKGROUND_RUNS.difference_update(dead)


def is_background_run_active() -> bool:
    prune_background_runs()
    with BACKGROUND_RUNS_LOCK:
        return any(thread.is_alive() for thread in BACKGROUND_RUNS)


def prune_background_cleanups() -> None:
    with BACKGROUND_CLEANUPS_LOCK:
        dead = {thread for thread in BACKGROUND_CLEANUPS if not thread.is_alive()}
        BACKGROUND_CLEANUPS.difference_update(dead)


def is_background_cleanup_active() -> bool:
    prune_background_cleanups()
    with BACKGROUND_CLEANUPS_LOCK:
        return any(thread.is_alive() for thread in BACKGROUND_CLEANUPS)


def is_run_lock_active() -> bool:
    Paths.data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_PATH, 'a+', encoding='utf-8')
    try:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return True
    try:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        lock_file.close()


def is_pipeline_busy() -> bool:
    return is_background_run_active() or is_run_lock_active()


def recover_stale_runs() -> int:
    if is_pipeline_busy():
        return 0
    return mark_running_runs_stale('Run interrupted before completion; marked stale automatically.')


def start_cleanup_in_background(retention_days: int) -> bool:
    prune_background_cleanups()
    with BACKGROUND_CLEANUPS_LOCK:
        if any(thread.is_alive() for thread in BACKGROUND_CLEANUPS):
            return False
        thread = Thread(target=_background_cleanup_worker, args=(retention_days,), daemon=True, name='news-admin-cleanup')
        BACKGROUND_CLEANUPS.add(thread)
        thread.start()
        return True


def start_pipeline_in_background(trigger_kind: str = 'manual') -> bool:
    recover_stale_runs()
    prune_background_runs()
    with BACKGROUND_RUNS_LOCK:
        if any(thread.is_alive() for thread in BACKGROUND_RUNS) or is_run_lock_active():
            return False
        thread = Thread(target=_background_pipeline_worker, args=(trigger_kind,), daemon=True, name='news-admin-pipeline')
        BACKGROUND_RUNS.add(thread)
        thread.start()
        return True


def _background_pipeline_worker(trigger_kind: str) -> None:
    try:
        execute_pipeline(trigger_kind)
    except Exception:
        pass
    finally:
        with BACKGROUND_RUNS_LOCK:
            BACKGROUND_RUNS.discard(current_thread())


def _background_cleanup_worker(retention_days: int) -> None:
    try:
        with run_lock():
            cleanup_old_data(retention_days)
    except Exception:
        pass
    finally:
        with BACKGROUND_CLEANUPS_LOCK:
            BACKGROUND_CLEANUPS.discard(current_thread())


@contextmanager
def run_lock():
    Paths.data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_PATH, 'a+', encoding='utf-8')
    try:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError('Another run is already in progress') from exc
    try:
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def make_update_items(groups: list[dict[str, Any]], baseline_groups: list[dict[str, Any]], limit: int = UPDATE_SECTION_LIMIT) -> list[dict[str, Any]]:
    baseline_map = {str(group['canonical_key']): group for group in baseline_groups}
    updates: list[dict[str, Any]] = []
    for group in groups:
        canonical_key = str(group['canonical_key'])
        baseline = baseline_map.get(canonical_key)
        if baseline is None:
            updates.append(group)
            if len(updates) >= limit:
                break
            continue
        baseline_sources = {str(row['source_name']) for row in baseline.get('evidence', [])}
        current_sources = {str(row['source_name']) for row in group.get('evidence', [])}
        new_sources = [source for source in current_sources if source not in baseline_sources]
        if not new_sources:
            continue
        update_group = dict(group)
        update_group['summary_text'] = normalize_body_punctuation(f"较首轮新增来源：{'、'.join(new_sources[:3])}。 {group['summary_text']}")
        updates.append(update_group)
        if len(updates) >= limit:
            break
    return updates


def execute_pipeline(trigger_kind: str = 'manual') -> int:
    with run_lock():
        _ = mark_running_runs_stale('Run interrupted before completion; marked stale automatically.')
        run_id = create_run(trigger_kind)
        try:
            cleanup_summary: dict[str, Any] = {}
            candidates: list[dict[str, Any]] = []
            sources = [row for row in list_sources() if row['enabled']]
            for source in sources:
                result = fetch_source(source, run_id)
                ok = len(result['items']) >= 3 and not result['errors']
                add_run_source_result(run_id, source['id'], 'ok' if ok else 'warning', len(result['items']), len(result['items']), result['artifact_relpath'], '\n'.join(result['errors']))
                mark_source_health(source['id'], ok, '\n'.join(result['errors']))
                candidates.append({'source': source, 'items': result['items']})
            groups = aggregate_candidates(candidates)
            for group in groups:
                group_id = create_story_group(run_id, group['canonical_key'], group['category'], group['title'], group['score'], group['source_count'], group['summary_text'])
                for evidence in group['evidence']:
                    add_story_evidence(group_id, evidence['source_id'], evidence['title'], evidence['url'], evidence['snippet'], evidence['rank_order'])
            current_run = get_run(run_id) or {}
            baseline_run = get_first_completed_run_on_date(str(current_run.get('started_at', ''))[:10]) if current_run else None
            baseline_groups = list_story_groups(int(baseline_run['id'])) if baseline_run and int(baseline_run['id']) != run_id else []
            update_items = make_update_items(groups, baseline_groups) if baseline_groups else []
            sections = sectioned_groups(groups, update_items)
            digest_title = '每日新闻聚合简报'
            token = generate_publication_token()
            publication_web_url = build_publication_web_url(token)
            digest_text = render_text_digest(sections, publication_web_url)
            publication_html = render_publication_html(digest_title, sections, publication_path(token), candidates)
            relpath, latest_relpath = save_publication(run_id, publication_html, token)
            publication_id = create_publication(run_id, digest_title, relpath, latest_relpath)
            queue_deliveries(run_id, publication_id, digest_text, publication_web_url)
            process_deliveries(digest_text)
            try:
                cleanup_summary = cleanup_old_data()
            except Exception as cleanup_exc:
                cleanup_summary = {'error': str(cleanup_exc)}
            finish_run(
                run_id,
                'completed',
                {
                    'source_count': len(sources),
                    'story_groups': len(groups),
                    'baseline_run_id': int(baseline_run['id']) if baseline_run else None,
                    'update_story_count': len(update_items),
                    'sections': [section['title'] for section in sections],
                    'publication_relpath': relpath,
                    'cleanup': cleanup_summary,
                },
                '',
            )
            return run_id
        except Exception as exc:
            finish_run(run_id, 'failed', {}, str(exc))
            raise


def build_publication_web_url(token_or_relpath: str, base_url: str = '') -> str:
    token = Path(token_or_relpath).stem if token_or_relpath.endswith('.html') else token_or_relpath.strip('/').split('/')[-1]
    path = f'/published/{token}'
    root = base_url.strip().rstrip('/') or PUBLIC_WEB_BASE_URL or discover_public_web_base_url()
    return f'{root}{path}' if root else path


def discover_public_web_base_url() -> str:
    log_path = Paths.data_dir / 'live-server.log'
    if not log_path.exists():
        return ''
    try:
        lines = log_path.read_text(encoding='utf-8', errors='ignore').splitlines()
    except OSError:
        return ''
    pattern = re.compile(r'Running on (https?://(?!127\.0\.0\.1|localhost)[^\s]+)')
    for line in reversed(lines):
        match = pattern.search(line)
        if match:
            return match.group(1).rstrip('/')
    return ''


def build_delivery_request_payload(digest_text: str, publication_web_url: str, trigger_label: str, manual: bool) -> dict[str, Any]:
    return {
        'preview': digest_text[:400],
        'manual': manual,
        'trigger_label': trigger_label,
        'publication_web_url': publication_web_url,
        'digest_length': len(digest_text),
    }


def queue_deliveries(run_id: int, publication_id: int, digest_text: str, publication_web_url: str) -> None:
    platforms = {row['id']: row for row in list_platforms()}
    recipients = [row for row in list_recipients() if row['enabled']]
    for recipient in recipients:
        platform = platforms.get(recipient['platform_id'])
        if not platform or not platform['enabled']:
            continue
        idem = sha256(f"{publication_id}:{recipient['id']}:{digest_text}".encode('utf-8')).hexdigest()
        request_payload = build_delivery_request_payload(digest_text, publication_web_url, '自动投递', False)
        request_payload['source'] = 'pipeline-auto-delivery'
        _ = create_delivery(run_id, publication_id, recipient['id'], platform['kind'], idem, request_payload)


def process_deliveries(digest_text: str) -> None:
    for delivery in get_pending_deliveries():
        target = json.loads(delivery['target_json'])
        config = json.loads(delivery['config_json'])
        attempts = delivery['attempts'] + 1
        try:
            response_json = send_message(delivery['platform_kind_actual'], config, target, digest_text)
            update_delivery(delivery['id'], 'sent', attempts, response_json, '')
        except Exception as exc:
            update_delivery(delivery['id'], 'retry', attempts, {}, str(exc), next_retry(attempts))


def render_run_digest(run_id: int, publication_web_url: str = '') -> str:
    groups = list_story_groups(run_id)
    enriched_groups: list[dict[str, Any]] = []
    for group in groups:
        category = str(group['category'])
        title = str(group['title'])
        evidence = group.get('evidence', [])
        enriched_groups.append(
            {
                'canonical_key': str(group['canonical_key']),
                'category': category,
                'title': title,
                'score': float(group['score']),
                'source_count': int(group['source_count']),
                'summary_text': str(group['summary_text']),
                'analysis_text': build_observation_analysis(title, evidence, category),
                'thumbnail_url': '',
                'evidence': evidence,
            }
        )
    sections = sectioned_groups(enriched_groups)
    return render_text_digest(sections, publication_web_url)


def send_latest_publication_to_recipient(recipient_id: int, request_base_url: str = '') -> dict[str, Any]:
    publication = get_latest_publication()
    if not publication:
        raise ValueError('暂无可发送的最新发布结果')
    recipient = get_recipient(recipient_id)
    if not recipient:
        raise ValueError('接收人不存在')
    platform = get_platform(int(recipient['platform_id']))
    if not platform:
        raise ValueError('接收人关联的平台不存在')
    if not recipient['enabled']:
        raise ValueError('接收人已停用，无法手动发送')
    if not platform['enabled']:
        raise ValueError('关联平台已停用，无法手动发送')
    target_issue = recipient_target_issue(str(platform['kind']), recipient['target'])
    if target_issue:
        raise ValueError(f'接收人配置不完整：{target_issue}')
    publication_web_url = build_publication_web_url(str(publication.get('relpath') or ''), request_base_url)
    digest_text = render_run_digest(int(publication['run_id']), publication_web_url)
    manual_key_seed = f"manual:{publication['id']}:{recipient_id}:{datetime.now(timezone.utc).isoformat()}"
    request_payload = build_delivery_request_payload(digest_text, publication_web_url, '手动触发', True)
    request_payload['source'] = 'recipient-send-latest'
    delivery_id = create_delivery(
        int(publication['run_id']),
        int(publication['id']),
        recipient_id,
        str(platform['kind']),
        sha256(manual_key_seed.encode('utf-8')).hexdigest(),
        request_payload,
    )
    try:
        response_json = send_message(str(platform['kind']), platform['config'], recipient['target'], digest_text)
        if delivery_id:
            update_delivery(delivery_id, 'sent', 1, response_json, '')
        return {
            'delivery_id': delivery_id,
            'response': response_json,
            'publication_id': int(publication['id']),
            'run_id': int(publication['run_id']),
            'publication_web_url': publication_web_url,
        }
    except Exception as exc:
        if delivery_id:
            update_delivery(delivery_id, 'failed', 1, {}, str(exc))
        raise
