"""마이그레이션 왕복·alembic check·pg_catalog 비교(AC-2).

alembic check 는 CHECK 제약·부분 인덱스 조건·트리거를 비교하지 못한다. 그래서
① 마이그레이션이 만든 스키마를 기대 목록과 비교하고
② 모델 메타데이터로 만든 스키마(create_all)와 칸·제약·인덱스 정의를 하나하나 비교한다.
"""

from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Base
from tests.integration.conftest import run_alembic
from tests.integration.deploy_harness import DeployHarness
from tests.integration.rollback_support import create_rollback_fixture

MODEL_SCHEMA = "model_check"

EXPECTED_CONSTRAINTS = {
    ("deployments", "ck_deployments_config_snapshot_is_object"): (
        "CHECK ((jsonb_typeof(config_snapshot) = 'object'::text))"
    ),
    ("deployments", "ck_deployments_image_digest_format"): (
        "CHECK (((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text))"
    ),
    ("deployments", "ck_deployments_rollback_of_not_self"): "CHECK ((rollback_of_id <> id))",
    ("deployments", "ck_deployments_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, "
        "'IN_PROGRESS'::character varying, 'SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'CANCELLED'::character varying])::text[])))"
    ),
    ("deployments", "fk_deployments_rollback_of_id_deployments"): (
        "FOREIGN KEY (rollback_of_id) REFERENCES deployments(id) ON DELETE RESTRICT"
    ),
    ("deployments", "fk_deployments_service_environment_id_service_environments"): (
        "FOREIGN KEY (service_environment_id) REFERENCES service_environments(id) "
        "ON DELETE RESTRICT"
    ),
    ("deployments", "pk_deployments"): "PRIMARY KEY (id)",
    ("deployments", "uq_deployments_id_service_environment_id"): (
        "UNIQUE (id, service_environment_id)"
    ),
    ("environments", "ck_environments_config_is_object"): (
        "CHECK ((jsonb_typeof(config) = 'object'::text))"
    ),
    ("environments", "ck_environments_connection_checked_at_consistency"): (
        "CHECK ((((connection_status)::text = 'UNCHECKED'::text) = "
        "(connection_checked_at IS NULL)))"
    ),
    ("environments", "ck_environments_connection_error_consistency"): (
        "CHECK ((((connection_status)::text = 'FAILED'::text) = (connection_error IS NOT NULL)))"
    ),
    ("environments", "ck_environments_connection_status_valid"): (
        "CHECK (((connection_status)::text = ANY ((ARRAY['UNCHECKED'::character varying, "
        "'CONNECTED'::character varying, 'FAILED'::character varying])::text[])))"
    ),
    ("environments", "ck_environments_name_format"): (
        "CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'::text))"
    ),
    ("environments", "ck_environments_soft_delete_consistency"): (
        "CHECK ((is_deleted = (deleted_at IS NOT NULL)))"
    ),
    ("environments", "pk_environments"): "PRIMARY KEY (id)",
    ("service_environments", "ck_service_environments_soft_delete_consistency"): (
        "CHECK ((is_deleted = (deleted_at IS NOT NULL)))"
    ),
    ("service_environments", "ck_service_environments_traffic_weight_range"): (
        "CHECK (((traffic_weight >= 0) AND (traffic_weight <= 100)))"
    ),
    ("service_environments", "fk_service_environments_current_deployment_id_id_deployments"): (
        "FOREIGN KEY (current_deployment_id, id) "
        "REFERENCES deployments(id, service_environment_id) ON DELETE RESTRICT"
    ),
    ("service_environments", "fk_service_environments_environment_id_environments"): (
        "FOREIGN KEY (environment_id) REFERENCES environments(id) ON DELETE RESTRICT"
    ),
    ("service_environments", "fk_service_environments_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("service_environments", "pk_service_environments"): "PRIMARY KEY (id)",
    ("services", "ck_services_name_format"): (
        "CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'::text))"
    ),
    ("services", "ck_services_soft_delete_consistency"): (
        "CHECK ((is_deleted = (deleted_at IS NOT NULL)))"
    ),
    ("services", "pk_services"): "PRIMARY KEY (id)",
    # --- API 서버 3: 원터치 배포 ---
    ("services", "ck_services_container_port_range"): (
        "CHECK (((container_port >= 1) AND (container_port <= 65535)))"
    ),
    ("services", "ck_services_delete_error_only_when_deleting"): (
        "CHECK (((delete_error IS NULL) OR ((status)::text = 'DELETING'::text)))"
    ),
    ("services", "ck_services_health_check_path_format"): (
        "CHECK (((health_check_path)::text ~~ '/%'::text))"
    ),
    ("services", "ck_services_health_check_timeout_range"): (
        "CHECK (((health_check_timeout_seconds >= 10) AND (health_check_timeout_seconds <= 1800)))"
    ),
    ("services", "ck_services_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, "
        "'DELETING'::character varying])::text[])))"
    ),
    ("builds", "ck_builds_commit_sha_format"): "CHECK ((commit_sha ~ '^[0-9a-f]{40}$'::text))",
    ("builds", "ck_builds_error_code_consistency"): (
        "CHECK ((((status)::text = 'FAILED'::text) = (error_code IS NOT NULL)))"
    ),
    ("builds", "ck_builds_finished_at_consistency"): (
        "CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying])::text[])) = (finished_at IS NOT NULL)))"
    ),
    ("builds", "ck_builds_image_consistency"): (
        "CHECK ((((status)::text = 'SUCCEEDED'::text) = "
        "((image_digest IS NOT NULL) AND (image_repository IS NOT NULL))))"
    ),
    ("builds", "ck_builds_image_digest_format"): (
        "CHECK (((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text))"
    ),
    ("builds", "ck_builds_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'RUNNING'::character varying, 'SUCCEEDED'::character varying, "
        "'FAILED'::character varying])::text[])))"
    ),
    ("builds", "fk_builds_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("builds", "pk_builds"): "PRIMARY KEY (id)",
    ("deploy_requests", "ck_deploy_requests_build_reused_consistency"): (
        "CHECK (((build_id IS NULL) = (build_reused IS NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_cancel_reason_consistency"): (
        "CHECK ((((status)::text = 'CANCELLED'::text) = (cancel_reason IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_cancel_reason_valid"): (
        "CHECK (((cancel_reason)::text = ANY ((ARRAY['SUPERSEDED'::character varying, "
        "'SERVICE_DELETED'::character varying, 'BASE_CHANGED'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_commit_sha_format"): (
        "CHECK ((commit_sha ~ '^[0-9a-f]{40}$'::text))"
    ),
    ("deploy_requests", "ck_deploy_requests_config_snapshot_is_object"): (
        "CHECK ((jsonb_typeof(config_snapshot) = 'object'::text))"
    ),
    ("deploy_requests", "ck_deploy_requests_error_code_consistency"): (
        "CHECK ((((status)::text = 'FAILED'::text) = (error_code IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_finished_at_consistency"): (
        "CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'CANCELLED'::character varying])::text[])) = "
        "(finished_at IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_last_health_result_valid"): (
        "CHECK (((last_health_result)::text = ANY ((ARRAY['OK'::character varying, "
        "'HTTP_ERROR'::character varying, 'UNREACHABLE'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_ref_type_valid"): (
        "CHECK (((ref_type)::text = ANY ((ARRAY['BRANCH'::character varying, "
        "'COMMIT'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_revert_status_valid"): (
        "CHECK (((revert_status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_simulate_is_object"): (
        "CHECK ((jsonb_typeof(simulate) = 'object'::text))"
    ),
    ("deploy_requests", "ck_deploy_requests_source_request_consistency"): (
        "CHECK ((((trigger)::text = 'STANDBY_SYNC'::text) = (source_request_id IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'BUILDING'::character varying, 'DEPLOYING'::character varying, "
        "'VERIFYING'::character varying, 'SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'CANCELLED'::character varying])::text[])))"
    ),
    ("deploy_requests", "ck_deploy_requests_trigger_valid"): (
        "CHECK (((trigger)::text = ANY ((ARRAY['MANUAL'::character varying, "
        "'STANDBY_SYNC'::character varying, 'ROLLBACK'::character varying])::text[])))"
    ),
    ("deploy_requests", "fk_deploy_requests_build_id_builds"): (
        "FOREIGN KEY (build_id) REFERENCES builds(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_deployment_id_deployments"): (
        "FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_environment_id_environments"): (
        "FOREIGN KEY (environment_id) REFERENCES environments(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_service_environment_id_service_environments"): (
        "FOREIGN KEY (service_environment_id) REFERENCES service_environments(id) "
        "ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_source_request_id_deploy_requests"): (
        "FOREIGN KEY (source_request_id) REFERENCES deploy_requests(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_superseded_by_id_deploy_requests"): (
        "FOREIGN KEY (superseded_by_id) REFERENCES deploy_requests(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "pk_deploy_requests"): "PRIMARY KEY (id)",
    ("jobs", "ck_jobs_kind_valid"): (
        "CHECK (((kind)::text = ANY ((ARRAY['DEPLOY_REQUEST'::character varying, "
        "'SERVICE_TEARDOWN'::character varying])::text[])))"
    ),
    ("jobs", "ck_jobs_lease_consistency"): (
        "CHECK ((((status)::text = 'RUNNING'::text) = "
        "((lease_token IS NOT NULL) AND (lease_expires_at IS NOT NULL))))"
    ),
    ("jobs", "ck_jobs_lease_losses_non_negative"): "CHECK ((lease_losses >= 0))",
    ("jobs", "ck_jobs_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'RUNNING'::character varying, 'DONE'::character varying, "
        "'FAILED'::character varying, 'CANCELLED'::character varying])::text[])))"
    ),
    ("jobs", "pk_jobs"): "PRIMARY KEY (id)",
    # --- API 서버 6: 로그인·조직 ---
    ("services", "fk_services_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("environments", "fk_environments_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("users", "pk_users"): "PRIMARY KEY (id)",
    ("users", "uq_users_github_id"): "UNIQUE (github_id)",
    ("organizations", "ck_organizations_name_not_empty"): (
        "CHECK ((char_length((name)::text) >= 1))"
    ),
    ("organizations", "ck_organizations_slug_format"): (
        "CHECK (((slug)::text ~ '^[a-z0-9](-?[a-z0-9]){0,38}$'::text))"
    ),
    ("organizations", "fk_organizations_created_by_user_id_users"): (
        "FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("organizations", "pk_organizations"): "PRIMARY KEY (id)",
    ("organizations", "uq_organizations_slug"): "UNIQUE (slug)",
    ("organization_members", "ck_organization_members_role_valid"): (
        "CHECK (((role)::text = ANY ((ARRAY['ADMIN'::character varying, "
        "'DEVELOPER'::character varying, 'VIEWER'::character varying])::text[])))"
    ),
    ("organization_members", "fk_organization_members_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("organization_members", "fk_organization_members_user_id_users"): (
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("organization_members", "pk_organization_members"): "PRIMARY KEY (id)",
    ("organization_members", "uq_organization_members_organization_id_user_id"): (
        "UNIQUE (organization_id, user_id)"
    ),
    ("invitations", "ck_invitations_accepted_at_consistency"): (
        "CHECK ((((status)::text = 'ACCEPTED'::text) = (accepted_at IS NOT NULL)))"
    ),
    ("invitations", "ck_invitations_role_valid"): (
        "CHECK (((role)::text = ANY ((ARRAY['ADMIN'::character varying, "
        "'DEVELOPER'::character varying, 'VIEWER'::character varying])::text[])))"
    ),
    ("invitations", "ck_invitations_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, "
        "'ACCEPTED'::character varying, 'EXPIRED'::character varying, "
        "'REVOKED'::character varying])::text[])))"
    ),
    ("invitations", "fk_invitations_invited_by_user_id_users"): (
        "FOREIGN KEY (invited_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("invitations", "fk_invitations_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("invitations", "pk_invitations"): "PRIMARY KEY (id)",
    ("sessions", "fk_sessions_user_id_users"): (
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("sessions", "pk_sessions"): "PRIMARY KEY (id)",
    ("sessions", "uq_sessions_token_hash"): "UNIQUE (token_hash)",
    ("agent_tokens", "fk_agent_tokens_created_by_user_id_users"): (
        "FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("agent_tokens", "fk_agent_tokens_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("agent_tokens", "pk_agent_tokens"): "PRIMARY KEY (id)",
    ("agent_tokens", "uq_agent_tokens_token_hash"): "UNIQUE (token_hash)",
    ("audit_logs", "ck_audit_logs_actor_consistency"): (
        "CHECK (((((actor_type)::text = 'USER'::text) AND (actor_user_id IS NOT NULL) "
        "AND (actor_agent_token_id IS NULL)) OR (((actor_type)::text = 'AGENT'::text) "
        "AND (actor_agent_token_id IS NOT NULL) AND (actor_user_id IS NULL))))"
    ),
    ("audit_logs", "ck_audit_logs_actor_type_valid"): (
        "CHECK (((actor_type)::text = ANY ((ARRAY['USER'::character varying, "
        "'AGENT'::character varying])::text[])))"
    ),
    ("audit_logs", "ck_audit_logs_result_valid"): "CHECK (((result)::text = 'DENIED'::text))",
    ("audit_logs", "fk_audit_logs_actor_agent_token_id_agent_tokens"): (
        "FOREIGN KEY (actor_agent_token_id) REFERENCES agent_tokens(id) ON DELETE RESTRICT"
    ),
    ("audit_logs", "fk_audit_logs_actor_user_id_users"): (
        "FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("audit_logs", "fk_audit_logs_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("audit_logs", "pk_audit_logs"): "PRIMARY KEY (id)",
    # --- API 서버 3-1: 고객 앱 주소 형식 ---
    ("services", "ck_services_host_suffix_format"): "CHECK ((host_suffix ~ '^[a-z0-9]{8}$'::text))",
    # --- API 서버 4: 되돌리기·배포 작업 로그 ---
    ("deploy_requests", "ck_deploy_requests_rollback_of_request_consistency"): (
        "CHECK ((((trigger)::text = 'ROLLBACK'::text) = (rollback_of_request_id IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_rollback_base_consistency"): (
        "CHECK ((((trigger)::text = 'ROLLBACK'::text) = (rollback_base_deployment_id IS NOT NULL)))"
    ),
    ("deploy_requests", "ck_deploy_requests_rollback_of_request_not_self"): (
        "CHECK ((rollback_of_request_id <> id))"
    ),
    ("deploy_requests", "fk_deploy_requests_rollback_of_request_id_deploy_requests"): (
        "FOREIGN KEY (rollback_of_request_id) REFERENCES deploy_requests(id) ON DELETE RESTRICT"
    ),
    ("deploy_requests", "fk_deploy_requests_rollback_base_deployment_id_deployments"): (
        "FOREIGN KEY (rollback_base_deployment_id) REFERENCES deployments(id) ON DELETE RESTRICT"
    ),
    ("deploy_request_logs", "ck_deploy_request_logs_level_valid"): (
        "CHECK (((level)::text = ANY ((ARRAY['INFO'::character varying, "
        "'WARN'::character varying, 'ERROR'::character varying])::text[])))"
    ),
    ("deploy_request_logs", "ck_deploy_request_logs_seq_positive"): "CHECK ((seq >= 1))",
    ("deploy_request_logs", "fk_deploy_request_logs_deploy_request_id_deploy_requests"): (
        "FOREIGN KEY (deploy_request_id) REFERENCES deploy_requests(id) ON DELETE RESTRICT"
    ),
    ("deploy_request_logs", "pk_deploy_request_logs"): "PRIMARY KEY (id)",
    ("deploy_request_logs", "uq_deploy_request_logs_deploy_request_id_seq"): (
        "UNIQUE (deploy_request_id, seq)"
    ),
}

EXPECTED_INDEXES = {
    "ix_deployments_service_environment_id_created_at": (
        "CREATE INDEX ix_deployments_service_environment_id_created_at ON deployments "
        "USING btree (service_environment_id, created_at)"
    ),
    "pk_deployments": "CREATE UNIQUE INDEX pk_deployments ON deployments USING btree (id)",
    "pk_environments": "CREATE UNIQUE INDEX pk_environments ON environments USING btree (id)",
    "pk_service_environments": (
        "CREATE UNIQUE INDEX pk_service_environments ON service_environments USING btree (id)"
    ),
    "pk_services": "CREATE UNIQUE INDEX pk_services ON services USING btree (id)",
    "uq_deployments_id_service_environment_id": (
        "CREATE UNIQUE INDEX uq_deployments_id_service_environment_id ON deployments "
        "USING btree (id, service_environment_id)"
    ),
    # API 서버 6 이 uq_environments_name_live(전역)를 조직 안 유일로 바꿨다.
    "uq_environments_org_name_live": (
        "CREATE UNIQUE INDEX uq_environments_org_name_live ON environments "
        "USING btree (organization_id, name) WHERE (is_deleted = false)"
    ),
    "uq_service_environments_service_id_environment_id_live": (
        "CREATE UNIQUE INDEX uq_service_environments_service_id_environment_id_live "
        "ON service_environments USING btree (service_id, environment_id) "
        "WHERE (is_deleted = false)"
    ),
    # 서비스 이름은 조직 안에서만 유일하다(API 서버 3-1, 주소 유일성은 public_host 가 맡는다).
    "uq_services_org_name_live": (
        "CREATE UNIQUE INDEX uq_services_org_name_live ON services "
        "USING btree (organization_id, name) WHERE (is_deleted = false)"
    ),
    # --- API 서버 3: 원터치 배포 ---
    "uq_services_public_host_live": (
        "CREATE UNIQUE INDEX uq_services_public_host_live ON services USING btree (public_host) "
        "WHERE (is_deleted = false)"
    ),
    "uq_service_environments_public_host_live": (
        "CREATE UNIQUE INDEX uq_service_environments_public_host_live "
        "ON service_environments USING btree (public_host) WHERE (is_deleted = false)"
    ),
    "pk_builds": "CREATE UNIQUE INDEX pk_builds ON builds USING btree (id)",
    "uq_builds_service_id_repo_url_commit_sha_not_failed": (
        "CREATE UNIQUE INDEX uq_builds_service_id_repo_url_commit_sha_not_failed ON builds "
        "USING btree (service_id, repo_url, commit_sha) WHERE ((status)::text <> 'FAILED'::text)"
    ),
    "pk_deploy_requests": (
        "CREATE UNIQUE INDEX pk_deploy_requests ON deploy_requests USING btree (id)"
    ),
    "ix_deploy_requests_build_id": (
        "CREATE INDEX ix_deploy_requests_build_id ON deploy_requests USING btree (build_id)"
    ),
    "ix_deploy_requests_service_environment_id_created_at": (
        "CREATE INDEX ix_deploy_requests_service_environment_id_created_at ON deploy_requests "
        "USING btree (service_environment_id, created_at)"
    ),
    "ix_deploy_requests_service_id_created_at": (
        "CREATE INDEX ix_deploy_requests_service_id_created_at ON deploy_requests "
        "USING btree (service_id, created_at)"
    ),
    "uq_deploy_requests_service_environment_id_active": (
        "CREATE UNIQUE INDEX uq_deploy_requests_service_environment_id_active ON deploy_requests "
        "USING btree (service_environment_id) WHERE ((status)::text = ANY "
        "((ARRAY['BUILDING'::character varying, 'DEPLOYING'::character varying, "
        "'VERIFYING'::character varying])::text[]))"
    ),
    "uq_deploy_requests_service_environment_id_queued": (
        "CREATE UNIQUE INDEX uq_deploy_requests_service_environment_id_queued ON deploy_requests "
        "USING btree (service_environment_id) WHERE ((status)::text = 'QUEUED'::text)"
    ),
    "pk_jobs": "CREATE UNIQUE INDEX pk_jobs ON jobs USING btree (id)",
    "ix_jobs_status_run_after": (
        "CREATE INDEX ix_jobs_status_run_after ON jobs USING btree (status, run_after)"
    ),
    "uq_jobs_subject_id_open": (
        "CREATE UNIQUE INDEX uq_jobs_subject_id_open ON jobs USING btree (subject_id) "
        "WHERE ((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'RUNNING'::character varying])::text[]))"
    ),
    # --- API 서버 6: 로그인·조직 ---
    "ix_services_organization_id": (
        "CREATE INDEX ix_services_organization_id ON services USING btree (organization_id)"
    ),
    "pk_users": "CREATE UNIQUE INDEX pk_users ON users USING btree (id)",
    "uq_users_github_id": (
        "CREATE UNIQUE INDEX uq_users_github_id ON users USING btree (github_id)"
    ),
    "pk_organizations": "CREATE UNIQUE INDEX pk_organizations ON organizations USING btree (id)",
    "uq_organizations_slug": (
        "CREATE UNIQUE INDEX uq_organizations_slug ON organizations USING btree (slug)"
    ),
    "pk_organization_members": (
        "CREATE UNIQUE INDEX pk_organization_members ON organization_members USING btree (id)"
    ),
    "uq_organization_members_organization_id_user_id": (
        "CREATE UNIQUE INDEX uq_organization_members_organization_id_user_id "
        "ON organization_members USING btree (organization_id, user_id)"
    ),
    "ix_organization_members_user_id": (
        "CREATE INDEX ix_organization_members_user_id ON organization_members USING btree (user_id)"
    ),
    "pk_invitations": "CREATE UNIQUE INDEX pk_invitations ON invitations USING btree (id)",
    "ix_invitations_github_id": (
        "CREATE INDEX ix_invitations_github_id ON invitations USING btree (github_id)"
    ),
    "uq_invitations_organization_id_github_id_pending": (
        "CREATE UNIQUE INDEX uq_invitations_organization_id_github_id_pending ON invitations "
        "USING btree (organization_id, github_id) WHERE ((status)::text = 'PENDING'::text)"
    ),
    "pk_sessions": "CREATE UNIQUE INDEX pk_sessions ON sessions USING btree (id)",
    "uq_sessions_token_hash": (
        "CREATE UNIQUE INDEX uq_sessions_token_hash ON sessions USING btree (token_hash)"
    ),
    "pk_agent_tokens": "CREATE UNIQUE INDEX pk_agent_tokens ON agent_tokens USING btree (id)",
    "uq_agent_tokens_token_hash": (
        "CREATE UNIQUE INDEX uq_agent_tokens_token_hash ON agent_tokens USING btree (token_hash)"
    ),
    "ix_agent_tokens_organization_id_created_at": (
        "CREATE INDEX ix_agent_tokens_organization_id_created_at ON agent_tokens "
        "USING btree (organization_id, created_at)"
    ),
    "pk_audit_logs": "CREATE UNIQUE INDEX pk_audit_logs ON audit_logs USING btree (id)",
    # --- API 서버 4 ---
    "ix_deploy_requests_rollback_of_request_id": (
        "CREATE INDEX ix_deploy_requests_rollback_of_request_id ON deploy_requests "
        "USING btree (rollback_of_request_id)"
    ),
    "pk_deploy_request_logs": (
        "CREATE UNIQUE INDEX pk_deploy_request_logs ON deploy_request_logs USING btree (id)"
    ),
    "uq_deploy_request_logs_deploy_request_id_seq": (
        "CREATE UNIQUE INDEX uq_deploy_request_logs_deploy_request_id_seq "
        "ON deploy_request_logs USING btree (deploy_request_id, seq)"
    ),
    "ix_audit_logs_organization_id_created_at": (
        "CREATE INDEX ix_audit_logs_organization_id_created_at ON audit_logs "
        "USING btree (organization_id, created_at)"
    ),
}

EXPECTED_TRIGGERS = {
    ("deployments", "trg_deployments_prevent_delete"): (
        "CREATE TRIGGER trg_deployments_prevent_delete BEFORE DELETE ON deployments "
        "FOR EACH ROW EXECUTE FUNCTION prevent_deployment_delete()"
    ),
    ("deploy_requests", "trg_deploy_requests_prevent_delete"): (
        "CREATE TRIGGER trg_deploy_requests_prevent_delete BEFORE DELETE ON deploy_requests "
        "FOR EACH ROW EXECUTE FUNCTION prevent_append_only_delete()"
    ),
    # 거부 기록도 지우지 않는다(API 서버 3 의 공용 함수).
    ("audit_logs", "trg_audit_logs_prevent_delete"): (
        "CREATE TRIGGER trg_audit_logs_prevent_delete BEFORE DELETE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION prevent_append_only_delete()"
    ),
    # 배포 작업 로그도 지우지 않는다(같은 공용 함수).
    ("deploy_request_logs", "trg_deploy_request_logs_prevent_delete"): (
        "CREATE TRIGGER trg_deploy_request_logs_prevent_delete BEFORE DELETE ON "
        "deploy_request_logs FOR EACH ROW EXECUTE FUNCTION prevent_append_only_delete()"
    ),
}

# 기존 표에 칸을 더하기 전(접속 상태 revision) 시점.
BEFORE_ONE_TOUCH_DEPLOY = "6f6d92350314"
# 조직·로그인 표를 더하기 전(API 서버 3 head) 시점.
BEFORE_ORGANIZATIONS = "d95d675cb256"
BEFORE_CONNECTION_STATUS = "b17994eaa7aa"
# 고객 앱 주소 suffix 를 더하기 전(API 서버 6 head) 시점.
BEFORE_HOST_SUFFIX = "ce35cab9b2e9"
# 되돌리기·배포 작업 로그를 더하기 전(API 서버 3-1 head) 시점.
BEFORE_ROLLBACK = "1d1601c0111a"
ROLLBACK_COLUMNS = {"rollback_of_request_id", "rollback_base_deployment_id"}
SERVICE_NAME_INDEXES = {"uq_services_name_live", "uq_services_org_name_live"}
HOST_SUFFIX_CHECK = "ck_services_host_suffix_format"


def _snapshot(connection: Connection, schema: str) -> dict[str, dict[Any, Any]]:
    """schema 안 칸·제약·인덱스·트리거 정의를 스키마 이름 없이 모은다."""
    connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    prefix = f"{schema}."

    def rows(sql: str) -> list[Any]:
        return list(connection.execute(text(sql), {"schema": schema}))

    columns = {
        (row.table_name, row.column_name): (
            row.ordinal_position,
            row.data_type,
            row.character_maximum_length,
            row.is_nullable,
            row.column_default,
        )
        for row in rows(
            "SELECT table_name, column_name, ordinal_position, data_type, "
            "character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name <> 'alembic_version'"
        )
    }
    constraints = {
        (row.table_name, row.conname): row.definition
        for row in rows(
            "SELECT c.relname AS table_name, con.conname, "
            "pg_get_constraintdef(con.oid) AS definition "
            "FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname <> 'alembic_version'"
        )
    }
    indexes = {
        row.indexname: row.indexdef.replace(prefix, "")
        for row in rows(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename <> 'alembic_version'"
        )
    }
    triggers = {
        (row.table_name, row.tgname): row.definition.replace(prefix, "")
        for row in rows(
            "SELECT c.relname AS table_name, t.tgname, "
            "pg_get_triggerdef(t.oid) AS definition "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND NOT t.tgisinternal"
        )
    }
    return {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "triggers": triggers,
    }


CONNECTION_COLUMNS = {"connection_status", "connection_checked_at", "connection_error"}
CONNECTION_CHECKS = {
    "ck_environments_connection_status_valid",
    "ck_environments_connection_checked_at_consistency",
    "ck_environments_connection_error_consistency",
}


def _environment_columns_and_checks(connection: Connection) -> tuple[set[str], set[str]]:
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("environments")}
    checks = {check["name"] or "" for check in inspector.get_check_constraints("environments")}
    return columns, checks


async def test_migration_downgrade_one_removes_connection_columns_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_CONNECTION_STATUS))
        )
        columns_down, checks_down = await connection.run_sync(_environment_columns_and_checks)
        # 조직 칸이 생기기 전 시점에 행을 넣고,
        # 행이 있어도 되는 revision(API 서버 3 head)까지만 올린다.
        await connection.execute(
            text("INSERT INTO environments (name, kind, config) VALUES ('legacy-env', 'aws', '{}')")
        )
        await connection.run_sync(
            run_alembic(lambda config: command.upgrade(config, BEFORE_ORGANIZATIONS))
        )
        columns_up, checks_up = await connection.run_sync(_environment_columns_and_checks)
        legacy_status = await connection.scalar(
            text("SELECT connection_status FROM environments WHERE name = 'legacy-env'")
        )
        # 조직 revision 은 빈 표를 가정한다. 행을 지운 뒤 head 로 올린다.
        await connection.execute(text("DELETE FROM environments"))
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        await connection.run_sync(run_alembic(command.check))
        # downgrade -1 → upgrade head 는 지운 칸 번호를 다시 쓰지 않아 칸 위치가 달라진다.
        # 같은 세션의 다른 비교 시험이 실행 순서에 흔들리지 않게 표를 처음부터 다시 만든다.
        await connection.run_sync(run_alembic(lambda config: command.downgrade(config, "base")))
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))

    assert not columns_down & CONNECTION_COLUMNS
    assert not checks_down & CONNECTION_CHECKS
    assert CONNECTION_COLUMNS <= columns_up
    assert CONNECTION_CHECKS <= checks_up
    # 기존 행은 UNCHECKED 가 된다.
    assert legacy_status == "UNCHECKED"


async def test_migration_round_trip_upgrade_downgrade_upgrade(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(run_alembic(lambda config: command.downgrade(config, "base")))
        tables_after_downgrade = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )
        functions_after_downgrade = await connection.scalar(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'prevent_deployment_delete'")
        )
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        tables_after_upgrade = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )

    assert tables_after_downgrade == ["alembic_version"]
    assert functions_after_downgrade == 0
    assert sorted(tables_after_upgrade) == [
        "agent_tokens",
        "alembic_version",
        "audit_logs",
        "builds",
        "deploy_request_logs",
        "deploy_requests",
        "deployments",
        "environments",
        "invitations",
        "jobs",
        "organization_members",
        "organizations",
        "service_environments",
        "services",
        "sessions",
        "users",
    ]


ONE_TOUCH_DEPLOY_TABLES = {"builds", "deploy_requests", "jobs"}
ONE_TOUCH_DEPLOY_SERVICE_COLUMNS = {"repo_url", "public_host", "status", "simulated"}


def _one_touch_deploy_state(connection: Connection) -> tuple[set[str], set[str], int]:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names()) & ONE_TOUCH_DEPLOY_TABLES
    service_columns = {column["name"] for column in inspector.get_columns("services")}
    functions = connection.scalar(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'prevent_append_only_delete'")
    )
    return tables, service_columns & ONE_TOUCH_DEPLOY_SERVICE_COLUMNS, int(functions or 0)


async def test_one_touch_deploy_revision_downgrade_removes_tables_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    # 트랜잭션을 되돌려 칸 번호(attnum)가 바뀐 스키마를 남기지 않는다(PostgreSQL DDL 은 트랜잭션).
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_ONE_TOUCH_DEPLOY))
        )
        down = await connection.run_sync(_one_touch_deploy_state)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_one_touch_deploy_state)
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    assert down == (set(), set(), 0)
    assert up == (ONE_TOUCH_DEPLOY_TABLES, ONE_TOUCH_DEPLOY_SERVICE_COLUMNS, 1)


async def test_one_touch_deploy_revision_upgrade_with_existing_services_fails_clearly(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_ONE_TOUCH_DEPLOY))
        )
        await connection.execute(text("INSERT INTO services (name) VALUES ('legacy-service')"))
        with pytest.raises(RuntimeError, match="services has 1 rows") as raised:
            await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        await transaction.rollback()

    assert "expects services to be empty" in str(raised.value)


ORGANIZATION_TABLES = {
    "users",
    "organizations",
    "organization_members",
    "invitations",
    "sessions",
    "agent_tokens",
    "audit_logs",
}


def _organization_state(connection: Connection) -> tuple[set[str], set[str], set[str], int]:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names()) & ORGANIZATION_TABLES
    organization_columns = {
        table
        for table in ("services", "environments")
        if "organization_id" in {column["name"] for column in inspector.get_columns(table)}
    }
    environment_indexes = {index["name"] or "" for index in inspector.get_indexes("environments")}
    functions = connection.scalar(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'prevent_append_only_delete'")
    )
    return tables, organization_columns, environment_indexes, int(functions or 0)


async def test_organization_revision_downgrade_removes_tables_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_ORGANIZATIONS))
        )
        down = await connection.run_sync(_organization_state)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_organization_state)
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    tables_down, columns_down, indexes_down, functions_down = down
    assert (tables_down, columns_down) == (set(), set())
    assert "uq_environments_name_live" in indexes_down
    assert "uq_environments_org_name_live" not in indexes_down
    # 공용 DELETE 금지 함수는 API 서버 3 소유라 내려도 남는다.
    assert functions_down == 1
    tables_up, columns_up, indexes_up, functions_up = up
    assert (tables_up, columns_up) == (ORGANIZATION_TABLES, {"services", "environments"})
    assert "uq_environments_org_name_live" in indexes_up
    assert "uq_environments_name_live" not in indexes_up
    assert functions_up == 1


@pytest.mark.parametrize(
    ("table", "insert"),
    [
        ("environments", "INSERT INTO environments (name, kind, config) VALUES ('e', 'aws', '{}')"),
        (
            "services",
            "INSERT INTO services (name, repo_url, repo_private, public_host) "
            "VALUES ('s', 'https://github.com/a/b', false, 's.apps.echo.test')",
        ),
    ],
)
async def test_organization_revision_upgrade_with_existing_rows_fails_clearly(
    engine: AsyncEngine, table: str, insert: str
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_ORGANIZATIONS))
        )
        await connection.execute(text(insert))
        with pytest.raises(RuntimeError, match=f"{table} has 1 rows") as raised:
            await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        await transaction.rollback()

    assert f"expects {table} to be empty" in str(raised.value)


def _host_suffix_state(connection: Connection) -> tuple[bool, set[str], set[str]]:
    inspector = inspect(connection)
    has_column = "host_suffix" in {column["name"] for column in inspector.get_columns("services")}
    indexes = {index["name"] or "" for index in inspector.get_indexes("services")}
    checks = {check["name"] or "" for check in inspector.get_check_constraints("services")}
    return has_column, indexes & SERVICE_NAME_INDEXES, checks & {HOST_SUFFIX_CHECK}


async def test_host_suffix_revision_downgrade_restores_global_name_index_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_HOST_SUFFIX))
        )
        down = await connection.run_sync(_host_suffix_state)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_host_suffix_state)
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    assert down == (False, {"uq_services_name_live"}, set())
    assert up == (True, {"uq_services_org_name_live"}, {HOST_SUFFIX_CHECK})


async def test_host_suffix_revision_upgrade_with_existing_services_fails_clearly(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_HOST_SUFFIX))
        )
        await connection.execute(
            text(
                "WITH u AS (INSERT INTO users (github_id, github_login) "
                "VALUES (1, 'legacy') RETURNING id), "
                "o AS (INSERT INTO organizations (slug, name, created_by_user_id) "
                "SELECT 'legacy', 'Legacy', id FROM u RETURNING id) "
                "INSERT INTO services (name, repo_url, repo_private, public_host, organization_id) "
                "SELECT 'shop', 'https://github.com/a/b', false, 'shop.apps.echo.test', id FROM o"
            )
        )
        with pytest.raises(RuntimeError, match="services has 1 rows") as raised:
            await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        await transaction.rollback()

    assert "expects services to be empty" in str(raised.value)


def _rollback_state(connection: Connection) -> tuple[bool, set[str], set[str], int]:
    inspector = inspect(connection)
    has_table = "deploy_request_logs" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("deploy_requests")}
    checks = {check["name"] or "" for check in inspector.get_check_constraints("deploy_requests")}
    functions = connection.scalar(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'prevent_append_only_delete'")
    )
    return (
        has_table,
        columns & ROLLBACK_COLUMNS,
        {name for name in checks if "rollback" in name},
        int(functions or 0),
    )


async def test_rollback_revision_downgrade_removes_table_and_columns_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_ROLLBACK))
        )
        down = await connection.run_sync(_rollback_state)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_rollback_state)
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    # 공용 DELETE 금지 함수는 API 서버 3 소유라 내려도 남는다.
    assert down == (False, set(), set(), 1)
    assert up == (
        True,
        ROLLBACK_COLUMNS,
        {
            "ck_deploy_requests_rollback_of_request_consistency",
            "ck_deploy_requests_rollback_base_consistency",
            "ck_deploy_requests_rollback_of_request_not_self",
        },
        1,
    )


async def test_rollback_revision_downgrade_with_rollback_rows_fails_clearly(
    harness: DeployHarness, engine: AsyncEngine
) -> None:
    rollback = await create_rollback_fixture(harness)

    async with engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(RuntimeError, match="deploy_requests has 1 rollback rows") as raised:
            await connection.run_sync(
                run_alembic(lambda config: command.downgrade(config, BEFORE_ROLLBACK))
            )
        await transaction.rollback()
    async with harness.session_factory() as session:
        still_there = await session.scalar(
            text("SELECT count(*) FROM deploy_requests WHERE id = :id"), {"id": rollback["id"]}
        )
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))

    assert "does not delete request history" in str(raised.value)
    assert still_there == 1
    assert version == "33ad047a493c"


async def test_alembic_check_reports_no_differences(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        # 차이가 있으면 AutogenerateDiffsDetected 가 난다.
        await connection.run_sync(run_alembic(command.check))


async def test_migrated_schema_matches_expected_catalog(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        snapshot = await connection.run_sync(lambda c: _snapshot(c, "public"))

    assert snapshot["constraints"] == EXPECTED_CONSTRAINTS
    assert snapshot["indexes"] == EXPECTED_INDEXES
    assert snapshot["triggers"] == EXPECTED_TRIGGERS


async def test_model_metadata_matches_migrated_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{MODEL_SCHEMA}" CASCADE'))
        await connection.execute(text(f'CREATE SCHEMA "{MODEL_SCHEMA}"'))
        try:
            await connection.run_sync(
                lambda c: Base.metadata.create_all(
                    c.execution_options(schema_translate_map={None: MODEL_SCHEMA})
                )
            )
            from_models = await connection.run_sync(lambda c: _snapshot(c, MODEL_SCHEMA))
            from_migration = await connection.run_sync(lambda c: _snapshot(c, "public"))
        finally:
            await connection.execute(text(f'DROP SCHEMA "{MODEL_SCHEMA}" CASCADE'))

    assert from_models["columns"] == from_migration["columns"]
    assert from_models["constraints"] == from_migration["constraints"]
    assert from_models["indexes"] == from_migration["indexes"]
    # 트리거는 모델 메타데이터에 없고 마이그레이션에만 있다.
    assert from_models["triggers"] == {}
