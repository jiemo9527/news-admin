from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import create_app
from .backup import create_backup_archive, import_backup_archive
from .cleanup import cleanup_old_data
from .config import Paths
from .db import ensure_dirs, init_db
from .repository import seed_defaults
from .service import execute_pipeline


def export_bundle(destination: Path | None = None) -> Path:
    ensure_dirs()
    if destination is None:
        destination = Paths.exports_dir / 'news-admin-export.zip'
    return create_backup_archive(destination)


def import_bundle(source: Path) -> None:
    ensure_dirs()
    import_backup_archive(source)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='News admin control panel')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-db')
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', default=5080, type=int)
    sub.add_parser('run-now')
    cleanup = sub.add_parser('cleanup')
    cleanup.add_argument('--days', default=7, type=int)
    export = sub.add_parser('export')
    export.add_argument('--output', default='')
    imp = sub.add_parser('import')
    imp.add_argument('--input', required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'init-db':
        init_db()
        seed_defaults()
        print(Paths.db_path)
        return
    if args.command == 'serve':
        app = create_app(start_jobs=True)
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return
    if args.command == 'run-now':
        init_db()
        seed_defaults()
        run_id = execute_pipeline('cli')
        print(run_id)
        return
    if args.command == 'cleanup':
        init_db()
        summary = cleanup_old_data(args.days)
        print(summary)
        return
    if args.command == 'export':
        output = Path(args.output) if args.output else None
        bundle = export_bundle(output)
        print(bundle)
        return
    if args.command == 'import':
        import_bundle(Path(args.input))
        print(Paths.data_dir)
        return
    parser.print_help()
    sys.exit(1)
