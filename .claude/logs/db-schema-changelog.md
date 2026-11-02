# DB 스키마 변경 로그 (db-schema-changelog)

`app/models/**/*.py` 변경 시 PostToolUse hook(`.claude/hooks/db_schema_changelog.py`)이 **한 줄**로 자동 기록한다. 상세 diff·스키마 DDL 은 git 이력과 [db-schema.sql](../rules/db-schema.sql) 을 본다.

형식: `- <UTC> · <파일> (<도구>) · <+추가 −삭제>`

<!-- 아래에 hook이 한 줄씩 prepend 한다 (이 마커 라인은 삭제하지 않는다) -->
<!-- CHANGELOG-ENTRIES -->
- 2026-11-02T03:38Z · app/models/service_environment.py (Edit) · +2 −2
- 2026-11-02T03:29Z · app/models/service.py (Edit) · host_suffix 컬럼 추가
- 2026-11-02T03:27Z · app/models/service.py (Edit) · host_suffix 컬럼 추가
- 2026-11-01T19:53Z · app/models/environment.py (Edit) · organization_id 컬럼 추가
- 2026-11-01T19:53Z · app/models/environment.py (Edit) · organization_id 컬럼 추가
- 2026-11-01T19:53Z · app/models/environment.py (Edit) · organization_id 컬럼 추가
- 2026-11-01T19:53Z · app/models/service.py (Edit) · organization_id 컬럼 추가
- 2026-11-01T19:53Z · app/models/service.py (Edit) · organization_id 컬럼 추가
- 2026-11-01T19:53Z · app/models/service.py (Edit) · organization_id 컬럼 추가
- 2026-11-01T19:53Z · app/models/__init__.py (Edit) · +13 −0
- 2026-11-01T19:53Z · app/models/audit_log.py (Write) · 신규/동일
- 2026-11-01T19:53Z · app/models/agent_token.py (Write) · 신규/동일
- 2026-11-01T19:53Z · app/models/user_session.py (Write) · 신규/동일
- 2026-11-01T19:53Z · app/models/invitation.py (Write) · 신규/동일
- 2026-11-01T19:53Z · app/models/organization.py (Write) · 신규/동일
- 2026-11-01T19:53Z · app/models/user.py (Write) · 신규/동일
- 2026-11-01T19:53Z · app/models/base.py (Edit) · created_at 컬럼 추가
- 2026-11-01T18:02Z · app/models/deploy_request.py (Edit) · 신규/동일
- 2026-11-01T17:45Z · app/models/service.py (Edit) · repo_url, repo_private, health_check_path, health_check_timeout_seconds, container_port, public_host, status, delete_error, simulated 컬럼 추가
- 2026-11-01T17:45Z · app/models/deploy_request.py (Edit) · 신규/동일
- 2026-11-01T17:43Z · app/models/service_environment.py (Edit) · public_host, teardown_attempt, teardown_requested_at, teardown_external_ref, teardown_error 컬럼 추가
- 2026-11-01T17:43Z · app/models/__init__.py (Write) · +13 −1
- 2026-11-01T17:43Z · app/models/job.py (Write) · 신규/동일
- 2026-11-01T17:43Z · app/models/deploy_request.py (Write) · 신규/동일
- 2026-11-01T17:43Z · app/models/build.py (Write) · 신규/동일
- 2026-11-01T17:42Z · app/models/service.py (Write) · repo_url, repo_private, health_check_path, health_check_timeout_seconds, container_port, public_host, status, delete_error, simulated 컬럼 추가
- 2026-11-01T14:48Z · app/models/environment.py (Edit) · connection_status, connection_checked_at, connection_error 컬럼 추가; secret_ref 컬럼 변경
- 2026-11-01T14:48Z · app/models/environment.py (Edit) · +20 −1
- 2026-11-01T14:48Z · app/models/environment.py (Edit) · +4 −1
- 2026-11-01T14:48Z · app/models/environment.py (Edit) · +2 −1
- 2026-11-01T14:48Z · app/models/base.py (Edit) · +3 −0
- 2026-11-01T08:38Z · app/models/base.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/base.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/base.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/deployment.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/service_environment.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/environment.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/service.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/service.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/environment.py (Edit) · 신규/동일
- 2026-11-01T08:38Z · app/models/__init__.py (Write) · 신규/동일
- 2026-11-01T08:38Z · app/models/deployment.py (Write) · 신규/동일
- 2026-11-01T08:38Z · app/models/service_environment.py (Write) · 신규/동일
- 2026-11-01T08:38Z · app/models/environment.py (Write) · 신규/동일
- 2026-11-01T08:38Z · app/models/service.py (Write) · 신규/동일
- 2026-11-01T08:37Z · app/models/base.py (Edit) · 신규/동일
- 2026-11-01T08:36Z · app/models/base.py (Edit) · 신규/동일
- 2026-11-01T08:36Z · app/models/base.py (Write) · 신규/동일
