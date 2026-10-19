#!/usr/bin/env bash
set -euo pipefail

reader_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  printf '用法：%s [端口]\n默认端口：8765，监听所有 IPv4 网络接口。\n支持本机地址、主机名（如 reader.local）及局域网 IP。\n额外域名可在 .env.local 中配置 READER_ALLOWED_HOSTS（逗号分隔，不含协议和端口）。\n' "$0"
  exit 0
fi

reader_port="${1:-8765}"
if [[ $# -gt 1 || ! "$reader_port" =~ ^[1-9][0-9]{0,4}$ ]] || (( reader_port > 65535 )); then
  printf '端口必须是 1–65535 之间的整数。例如：./start.sh 8766\n' >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/\n' >&2
  exit 1
fi

cd -- "$reader_root"
printf '正在检查并同步项目依赖……\n'
uv sync --locked

uv run --no-sync python - "$reader_port" <<'PY'
import socket
import sys
from reader_backend.config import Settings

port = int(sys.argv[1])
with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(('0.0.0.0', port))
    except OSError as error:
        print(f'无法监听端口 {port}：{error}。可指定其他端口，例如 ./start.sh 8766', file=sys.stderr)
        sys.exit(1)

settings = Settings()
if not settings.api_key:
    print('尚未配置模型密钥，仍可打开原书及手动划线。')
    print('启用 AI 批注：运行 uv run python scripts/configure_secret.py 配置密钥后重启。')

print(f'\n本机访问：http://127.0.0.1:{port}/')
for host in settings.allowed_hosts:
    if '.' in host and not host.startswith('127.') and '*' not in host:
        print(f'网络访问：http://{host}:{port}/')
print('按 Ctrl+C 停止服务。\n')
PY

exec uv run --no-sync uvicorn reader_backend.api:app \
  --host 0.0.0.0 --port "$reader_port" --timeout-graceful-shutdown 2
