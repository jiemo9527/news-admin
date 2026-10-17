"""命令行：python -m news_admin <command>"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='news_admin')
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve', help='启动 Web 后台')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=5080)
    run = sub.add_parser('run', help='执行一次流水线')
    run.add_argument('--no-push', action='store_true', help='只生成简报，不推送')
    show = sub.add_parser('show', help='打印某次运行的文本简报')
    show.add_argument('run_id', type=int, nargs='?')
    ev = sub.add_parser('eval', help='在标注数据上评测分类、聚类和摘要')
    ev.add_argument('--data', default='eval/data/sample.jsonl')
    ev.add_argument('--output', default='')
    ex = sub.add_parser('export-eval', help='导出某次运行的文章为待标注 JSONL')
    ex.add_argument('run_id', type=int)
    ex.add_argument('--output', default='eval/data/to_label.jsonl')
    args = parser.parse_args(argv)

    from . import tz

    logging.Formatter.converter = lambda *_: tz.now().timetuple()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')

    if args.command == 'serve':
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0

    from .runtime import build_runtime

    runtime = build_runtime()
    if args.command == 'run':
        run_id = runtime.pipeline.run('cli') if args.no_push else runtime.run_and_deliver('cli')
        run = runtime.store.get_run(run_id)
        print(json.dumps({'run_id': run_id, 'status': run['status'], 'stats': run['stats']}, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'show':
        from .digest import render_text

        run_id = args.run_id or (runtime.store.latest_completed_run() or {}).get('id')
        if not run_id:
            print('还没有完成的运行')
            return 1
        print(render_text(runtime.store.load_events(run_id), title=f'新闻简报 #{run_id}'))
        return 0
    if args.command == 'eval':
        from .evaluate import run_eval

        report = run_eval(runtime, args.data)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print(text)
        if args.output:
            from pathlib import Path

            Path(args.output).write_text(text, encoding='utf-8')
        return 0
    if args.command == 'export-eval':
        from .evaluate import export_for_labeling

        count = export_for_labeling(runtime, args.run_id, args.output)
        print(f'已导出 {count} 篇到 {args.output}，请填写 gold_region / gold_topic / gold_event')
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
