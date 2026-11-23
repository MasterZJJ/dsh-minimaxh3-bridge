#!/bin/sh
# 在线冒烟：健康检查 + 任务列表（不创建新任务，不消耗 GPU）。
# 用法：sh scripts/smoke.sh
# 环境变量：PORT（默认 8099）、KEY（设置了 apiKey 时必填）。
set -u

PORT=${PORT:-8099}
KEY=${KEY:-}
BASE="http://127.0.0.1:${PORT}"
AUTH=""
[ -n "$KEY" ] && AUTH="Authorization: Bearer $KEY"

echo "1) health ${BASE}/health"
if [ -n "$AUTH" ]; then
  curl -fsS -m 5 -H "$AUTH" "${BASE}/health" || { echo "health 失败"; exit 1; }
else
  curl -fsS -m 5 "${BASE}/health" || { echo "health 失败"; exit 1; }
fi
echo

echo "2) tasks ${BASE}/v2/query/video_generation?limit=5"
if [ -n "$AUTH" ]; then
  curl -fsS -m 5 -H "$AUTH" "${BASE}/v2/query/video_generation?limit=5"
else
  curl -fsS -m 5 "${BASE}/v2/query/video_generation?limit=5"
fi
echo
echo "smoke ok"
