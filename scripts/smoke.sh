#!/usr/bin/env bash
# scripts/smoke.sh — 后端主链路 smoke 测试
# 用法：bash scripts/smoke.sh
# 需要：后端在 8000 端口跑（uvicorn app.main:app --port 8000）
# 注意：依赖 .env 中有效的 LLM token；不调 LLM 的端点（health, list, create empty）能跑通

set -e

BASE_URL="${BASE_URL:-http://localhost:8000}"
PASS=0
FAIL=0

check() {
  local name="$1"
  local cmd="$2"
  local expected="$3"
  local actual
  if actual=$(eval "$cmd" 2>&1); then
    if [[ "$actual" == *"$expected"* ]]; then
      echo "  [OK] $name"
      PASS=$((PASS + 1))
    else
      echo "  [FAIL] $name — 期望包含 '$expected'，实际: $actual"
      FAIL=$((FAIL + 1))
    fi
  else
    echo "  [FAIL] $name — 命令失败: $actual"
    FAIL=$((FAIL + 1))
  fi
}

echo "== health check =="
check "GET /health" "curl -s ${BASE_URL}/health" '"ok"'

echo "== list cases =="
check "GET /api/cases" "curl -s ${BASE_URL}/api/cases" '"cases"'

echo "== get current case (empty) =="
check "GET /api/case" "curl -s ${BASE_URL}/api/case" '"case":'

echo "== create empty case =="
check "POST /api/cases (empty)" "curl -s -X POST -H 'Content-Type: application/json' -d '{}' ${BASE_URL}/api/cases" '"success":true'

echo "== smoke summary =="
echo "  passed: $PASS"
echo "  failed: $FAIL"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
