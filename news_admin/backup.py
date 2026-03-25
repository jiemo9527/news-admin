from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from .config import Paths


EXCLUDED_RELATIVE_PREFIXES = {'exports/', 'live-server.log', 'server-after-import.log', 'run.lock'}
REQUIRED_BACKUP_FILES = {'news_admin.db'}


def should_include_in_backup(file_path: Path, destination: Path) -> bool:
    if not file_path.is_file() or file_path == destination:
        return False
    try:
        relative = file_path.relative_to(Paths.data_dir).as_posix()
    except ValueError:
        return False
    if any(relative == prefix.rstrip('/') or relative.startswith(prefix) for prefix in EXCLUDED_RELATIVE_PREFIXES):
        return False
    return True


def create_backup_archive(destination: Path | None = None) -> Path:
    Paths.exports_dir.mkdir(parents=True, exist_ok=True)
    if destination is None:
        stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
        destination = Paths.exports_dir / f'news-admin-backup-{stamp}.zip'
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, 'w', compression=ZIP_DEFLATED) as archive:
        for file_path in Paths.data_dir.rglob('*'):
            if should_include_in_backup(file_path, destination):
                archive.write(file_path, file_path.relative_to(Paths.data_dir))
    return destination


def import_backup_archive(source: Path) -> Path:
    temp_source = Path(tempfile.mkstemp(suffix='.zip', prefix='news-admin-import-')[1])
    shutil.copy2(source, temp_source)
    extract_dir = Path(tempfile.mkdtemp(prefix='news-admin-restore-'))
    backup_dir = Paths.base_dir / 'data-backup-before-import'
    try:
        with ZipFile(temp_source) as archive:
            archive.extractall(extract_dir)
        extracted_files = {path.relative_to(extract_dir).as_posix() for path in extract_dir.rglob('*') if path.is_file()}
        if not REQUIRED_BACKUP_FILES.issubset(extracted_files):
            raise ValueError('备份文件缺少必需内容，无法导入。')
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if Paths.data_dir.exists():
            shutil.copytree(Paths.data_dir, backup_dir)
            shutil.rmtree(Paths.data_dir)
        shutil.copytree(extract_dir, Paths.data_dir)
        return backup_dir
    except Exception:
        if backup_dir.exists() and not Paths.data_dir.exists():
            shutil.copytree(backup_dir, Paths.data_dir)
        raise
    finally:
        temp_source.unlink(missing_ok=True)
        shutil.rmtree(extract_dir, ignore_errors=True)
