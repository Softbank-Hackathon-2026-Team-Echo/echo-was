#!/usr/bin/env bash
# 로컬 compose DB 의 현재 스키마를 .claude/rules/db-schema.sql 로 뽑는다.
# 먼저 `docker compose up -d --wait db` 와 `uv run alembic upgrade head` 를 실행해 둔다.
# 출력에서 세션 설정(SET·set_config)·주석·\restrict 줄을 빼고 빈 줄을 하나로 줄인다.
set -euo pipefail

cd "$(dirname "$0")/.."
output=".claude/rules/db-schema.sql"

{
  echo "-- 물리 DB 스키마(DDL)의 단일 출처. 직접 고치지 않는다."
  echo "-- 생성: scripts/dump_schema.sh (pg_dump --schema-only --no-owner --no-privileges)"
  echo "-- 마이그레이션 버전 표(alembic_version)는 뺀다."
  echo
  # 별도 worktree의 격리 DB도 같은 생성 절차를 쓴다.
  if [[ -n "${SCHEMA_CONTAINER:-}" ]]; then
    dump_cmd=(docker exec "$SCHEMA_CONTAINER")
  else
    dump_cmd=(docker compose exec -T db)
  fi
  "${dump_cmd[@]}" \
    pg_dump --username=echo --dbname=echo \
    --schema-only --no-owner --no-privileges --exclude-table=public.alembic_version \
    | grep -v -E '^(SET |SELECT pg_catalog\.set_config|--|\\restrict|\\unrestrict)' \
    | cat -s
} >"$output"

echo "wrote $output"
