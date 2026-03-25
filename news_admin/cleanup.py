from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Paths, RETENTION_DAYS, TIMEZONE
from .db import db_cursor, ensure_dirs


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def cleanup_old_data(retention_days: int = RETENTION_DAYS) -> dict[str, Any]:
    ensure_dirs()
    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=retention_days)
    pinned_run_id: int | None = None
    deleted_run_ids: list[int] = []
    deleted_publication_paths: list[Path] = []

    with db_cursor() as conn:
        latest_publication = conn.execute('SELECT run_id, relpath FROM publications ORDER BY id DESC LIMIT 1').fetchone()
        if latest_publication and latest_publication['run_id'] is not None:
            pinned_run_id = int(latest_publication['run_id'])

        runs = conn.execute('SELECT id, started_at, completed_at, status FROM runs ORDER BY id ASC').fetchall()
        for row in runs:
            run_id = int(row['id'])
            if row['status'] == 'running' or run_id == pinned_run_id:
                continue
            pivot = parse_iso_datetime(str(row['completed_at'] or row['started_at'] or ''))
            if pivot is None or pivot >= cutoff:
                continue
            deleted_run_ids.append(run_id)

        if deleted_run_ids:
            placeholders = ','.join('?' for _ in deleted_run_ids)
            publication_rows = conn.execute(
                f'SELECT relpath FROM publications WHERE run_id IN ({placeholders})',
                tuple(deleted_run_ids),
            ).fetchall()
            deleted_publication_paths = [Paths.data_dir / str(row['relpath']) for row in publication_rows]
            conn.executemany('DELETE FROM runs WHERE id = ?', ((run_id,) for run_id in deleted_run_ids))

    deleted_artifacts = 0
    for run_id in deleted_run_ids:
        run_dir = Paths.artifacts_dir / f'run-{run_id}'
        if run_dir.exists():
            for child in sorted(run_dir.rglob('*'), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            run_dir.rmdir()
            deleted_artifacts += 1

    deleted_publications = 0
    for file_path in deleted_publication_paths:
        if file_path.exists() and file_path.name != 'latest.html':
            file_path.unlink(missing_ok=True)
            deleted_publications += 1

    deleted_exports = delete_old_files(Paths.exports_dir, cutoff, keep_names=set())
    deleted_thumbnails = delete_old_files(Paths.thumbnails_dir, cutoff, keep_names=set())
    deleted_legacy_thumbnails = cleanup_thumbnail_cache_garbage(Paths.thumbnails_dir)

    backup_dir = Paths.base_dir / 'data-backup-before-import'
    deleted_backups = 0
    if backup_dir.exists() and datetime.fromtimestamp(backup_dir.stat().st_mtime, tz=cutoff.tzinfo) < cutoff:
        for child in sorted(backup_dir.rglob('*'), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                child.rmdir()
        backup_dir.rmdir()
        deleted_backups = 1

    return {
        'retention_days': retention_days,
        'timezone': TIMEZONE,
        'deleted_runs': len(deleted_run_ids),
        'deleted_artifact_dirs': deleted_artifacts,
        'deleted_publication_files': deleted_publications,
        'deleted_exports': deleted_exports,
        'deleted_thumbnails': deleted_thumbnails,
        'deleted_legacy_thumbnails': deleted_legacy_thumbnails,
        'deleted_backups': deleted_backups,
    }


def delete_old_files(directory: Path, cutoff: datetime, keep_names: set[str]) -> int:
    if not directory.exists():
        return 0
    deleted = 0
    for path in directory.iterdir():
        if path.name in keep_names or not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=cutoff.tzinfo)
        if modified < cutoff:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted


def cleanup_thumbnail_cache_garbage(directory: Path) -> int:
    if not directory.exists():
        return 0
    deleted = 0
    valid_stems = {path.stem for path in directory.iterdir() if path.is_file() and path.suffix == '.json'}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if path.suffix not in {'.jpg', '.json'}:
            path.unlink(missing_ok=True)
            deleted += 1
            continue
        if path.suffix == '.jpg' and path.stem not in valid_stems:
            path.unlink(missing_ok=True)
            deleted += 1
            continue
        if path.suffix == '.json' and not (directory / f'{path.stem}.jpg').exists():
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted
