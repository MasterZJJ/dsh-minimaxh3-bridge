#!/bin/sh
# 运行环境自检：Node / Python / 模板 / 在线健康。
# 用法：sh scripts/doctor.sh
# 环境变量：PORT（默认 8099）、KEY（设置了 apiKey 时用于在线探测）。
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PORT=${PORT:-8099}
KEY=${KEY:-}
FAILED=0

ok()   { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAILED=1; }

echo "dsh-minimaxh3-bridge doctor"
echo "root: $ROOT"
echo

echo "runtime:"
if command -v node >/dev/null 2>&1; then
  node_major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
  if [ "${node_major:-0}" -ge 22 ] 2>/dev/null; then
    ok "node $(node -v)"
  else
    bad "node $(node -v 2>/dev/null)（DSH 需要 >= 22）"
  fi
else
  bad "未找到 node（DSH 宿主需要 Node.js >= 22）"
fi

if command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    ok "python3 $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  else
    bad "python3 $(python3 -V 2>&1)（需要 >= 3.9）"
  fi
else
  bad "未找到 python3（桥接服务需要 Python 3.9+）"
fi

if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
else
  warn "没有 ffmpeg（仅 generate_audio=false 去音轨时需要）"
fi

echo
echo "bridge files:"
for f in index.js client.js cordis.patch.yml bridge/minimax_bridge.py \
         bridge/h3_api.py bridge/templates/pika_h3_stage1.api.json \
         bridge/templates/pika_h3_2stage.api.json; do
  if [ -f "$ROOT/$f" ]; then ok "$f"; else bad "缺少 $f"; fi
done

echo
echo "python syntax:"
if python3 -m py_compile "$ROOT/bridge/minimax_bridge.py" "$ROOT/bridge/h3_api.py" 2>/dev/null; then
  ok "bridge/*.py 可编译"
  rm -rf "$ROOT/bridge/__pycache__"
else
  bad "bridge/*.py 编译失败"
fi

echo
echo "live bridge (:${PORT}):"
if command -v curl >/dev/null 2>&1; then
  if [ -n "$KEY" ]; then
    body=$(curl -fsS -m 3 -H "Authorization: Bearer $KEY" "http://127.0.0.1:${PORT}/health" 2>/dev/null)
  else
    body=$(curl -fsS -m 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null)
  fi
  if [ -n "${body:-}" ]; then
    ok "health: $body"
  else
    warn "127.0.0.1:${PORT} 无响应（DSH 未启动 / 端口不同 / 服务未拉起）"
  fi
else
  warn "没有 curl，跳过在线探测"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "结果：必需项通过。"
  exit 0
fi
echo "结果：存在失败项，见上面的 [FAIL]。"
exit 1
