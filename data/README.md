# Sanitized data directory

This repository does not publish live runtime data.

Excluded from GitHub on purpose:

- `news_admin.db` and WAL/shm files
- `secret_key.txt`
- `admin_bootstrap.txt`
- `admin_auth.json`
- `translation_cache.json`
- `live-server.log`
- generated `artifacts/`, `publications/`, `exports/`, and `thumbnails/` contents

After cloning:

1. Run `python3 run.py init-db`
2. Start the app with `python3 run.py serve --host 127.0.0.1 --port 5080`
3. New runtime files will be recreated under `data/`
