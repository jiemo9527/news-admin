#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if [ -f .env.example ] && [ ! -f .env ]; then
  cp .env.example .env
fi

python run.py init-db

cat <<'EOF'

初始化完成。

建议下一步：
1. 按需编辑 .env（尤其是 NEWS_ADMIN_PUBLIC_BASE_URL）
2. 启动服务：python run.py serve --host 127.0.0.1 --port 5080
3. 如果没有在 .env 中设置管理员密码，请打开 data/admin_bootstrap.txt 查看首次登录密码
4. 首次登录后，在后台配置推送平台、接收人和定时任务

如果你不想使用 .venv，也可以直接执行：
python3 -m pip install -r requirements.txt
python3 run.py init-db
EOF
