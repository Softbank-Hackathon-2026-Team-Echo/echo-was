"""마이그레이션 왕복·alembic check·pg_catalog 비교(AC-2).

alembic check 는 CHECK 제약·부분 인덱스 조건·트리거를 비교하지 못한다. 그래서
① 마이그레이션이 만든 스키마를 기대 목록과 비교하고
② 모델 메타데이터로 만든 스키마(create_all)와 칸·제약·인덱스 정의를 하나하나 비교한다.
"""

from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.enums import SecurityControlId
from app.models import Base
from tests.integration.conftest import PROJECT_ROOT, run_alembic
from tests.integration.deploy_harness import DeployHarness
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import create_rollback_fixture
from tests.integration.security_gate_support import (
    add_exception,
    add_precheck,
    enable_gate,
    record_passing_evidence,
    revoke_exception,
)

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
        "'SERVICE_TEARDOWN'::character varying, 'MIGRATION_PLAN'::character varying])::text[])))"
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
        "AND (actor_agent_token_id IS NULL) AND (component IS NULL)) OR "
        "(((actor_type)::text = 'AGENT'::text) AND (actor_agent_token_id IS NOT NULL) "
        "AND (actor_user_id IS NULL) AND (component IS NULL)) OR "
        "(((actor_type)::text = 'SYSTEM'::text) AND (actor_user_id IS NULL) "
        "AND (actor_agent_token_id IS NULL) AND (component IS NOT NULL))))"
    ),
    ("audit_logs", "ck_audit_logs_actor_type_valid"): (
        "CHECK (((actor_type)::text = ANY ((ARRAY['USER'::character varying, "
        "'AGENT'::character varying, 'SYSTEM'::character varying])::text[])))"
    ),
    ("audit_logs", "ck_audit_logs_result_valid"): (
        "CHECK (((result)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'DENIED'::character varying])::text[])))"
    ),
    ("audit_logs", "ck_audit_logs_target_consistency"): (
        "CHECK ((((target_type IS NULL) = (target_id IS NULL)) "
        "AND ((target_label IS NULL) OR (target_type IS NOT NULL))))"
    ),
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
    # --- API 서버 8: 이전 계획 ---
    ("migration_plans", "ck_migration_plans_approved_consistency"): (
        "CHECK (((approved_by_user_id IS NULL) = (approved_at IS NULL)))"
    ),
    ("migration_plans", "ck_migration_plans_current_step_range"): (
        "CHECK (((current_step >= 0) AND (current_step <= 4)))"
    ),
    ("migration_plans", "ck_migration_plans_error_code_consistency"): (
        "CHECK ((((status)::text = ANY ((ARRAY['ROLLING_BACK'::character varying, "
        "'ROLLED_BACK'::character varying, 'FAILED'::character varying])::text[])) = (error_code "
        "IS NOT NULL)))"
    ),
    ("migration_plans", "ck_migration_plans_error_code_valid"): (
        "CHECK (((error_code)::text = ANY ((ARRAY['PREFLIGHT_FAILED'::character varying, "
        "'TRAFFIC_APPLY_FAILED'::character varying, 'HEALTH_CHECK_FAILED'::character varying, "
        "'METRICS_UNAVAILABLE'::character varying, 'THRESHOLD_EXCEEDED'::character varying, "
        "'DATA_LAG_EXCEEDED'::character varying, 'ABORTED'::character varying, "
        "'WORKER_LOST'::character varying, 'INTERNAL_ERROR'::character varying, "
        "'TARGET_NOT_RECEIVING'::character varying, "
        "'SECURITY_GATE_BLOCKED'::character varying])::text[])))"
    ),
    ("migration_plans", "ck_migration_plans_expected_seconds_non_negative"): (
        "CHECK ((expected_seconds >= 0))"
    ),
    ("migration_plans", "ck_migration_plans_fingerprint_is_object"): (
        "CHECK ((jsonb_typeof(fingerprint) = 'object'::text))"
    ),
    ("migration_plans", "ck_migration_plans_finished_at_consistency"): (
        "CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'ROLLED_BACK'::character varying, 'FAILED'::character varying, 'REJECTED'::character "
        "varying, 'CANCELLED'::character varying, 'EXPIRED'::character varying, "
        "'STALE'::character varying])::text[])) = (finished_at IS NOT NULL)))"
    ),
    ("migration_plans", "ck_migration_plans_mode_valid"): (
        "CHECK (((mode)::text = ANY ((ARRAY['PLANNED'::character varying, "
        "'EMERGENCY'::character varying, 'FAILBACK'::character varying])::text[])))"
    ),
    ("migration_plans", "ck_migration_plans_observe_seconds_range"): (
        "CHECK (((observe_seconds >= 1) AND (observe_seconds <= 600)))"
    ),
    ("migration_plans", "ck_migration_plans_rollback_attempts_non_negative"): (
        "CHECK ((rollback_attempts >= 0))"
    ),
    ("migration_plans", "ck_migration_plans_rollback_conditions_is_object"): (
        "CHECK ((jsonb_typeof(rollback_conditions) = 'object'::text))"
    ),
    ("migration_plans", "ck_migration_plans_rollback_status_valid"): (
        "CHECK (((rollback_status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'NOT_NEEDED'::character varying])::text[])))"
    ),
    ("migration_plans", "ck_migration_plans_simulate_is_object"): (
        "CHECK ((jsonb_typeof(simulate) = 'object'::text))"
    ),
    ("migration_plans", "ck_migration_plans_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['PENDING_APPROVAL'::character varying, "
        "'APPROVED'::character varying, 'RUNNING'::character varying, 'ROLLING_BACK'::character "
        "varying, 'SUCCEEDED'::character varying, 'ROLLED_BACK'::character varying, "
        "'FAILED'::character varying, 'REJECTED'::character varying, 'CANCELLED'::character "
        "varying, 'EXPIRED'::character varying, 'STALE'::character varying])::text[])))"
    ),
    ("migration_plans", "ck_migration_plans_step_phase_valid"): (
        "CHECK (((step_phase)::text = ANY ((ARRAY['APPLY'::character varying, "
        "'OBSERVE'::character varying, 'JUDGE'::character varying])::text[])))"
    ),
    ("migration_plans", "ck_migration_plans_step_results_is_array"): (
        "CHECK ((jsonb_typeof(step_results) = 'array'::text))"
    ),
    ("migration_plans", "ck_migration_plans_steps_is_array"): (
        "CHECK ((jsonb_typeof(steps) = 'array'::text))"
    ),
    ("migration_plans", "ck_migration_plans_target_not_source"): (
        "CHECK ((target_service_environment_id <> source_service_environment_id))"
    ),
    ("migration_plans", "fk_migration_plans_aborted_by_user_id_users"): (
        "FOREIGN KEY (aborted_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("migration_plans", "fk_migration_plans_approved_by_user_id_users"): (
        "FOREIGN KEY (approved_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("migration_plans", "fk_migration_plans_cancelled_by_user_id_users"): (
        "FOREIGN KEY (cancelled_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("migration_plans", "fk_migration_plans_created_by_user_id_users"): (
        "FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("migration_plans", "fk_migration_plans_rejected_by_user_id_users"): (
        "FOREIGN KEY (rejected_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("migration_plans", "fk_migration_plans_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("migration_plans", "fk_migration_plans_source_service_environment_id_servic_067d"): (
        "FOREIGN KEY (source_service_environment_id) REFERENCES service_environments(id) ON "
        "DELETE RESTRICT"
    ),
    ("migration_plans", "fk_migration_plans_target_service_environment_id_servic_a900"): (
        "FOREIGN KEY (target_service_environment_id) REFERENCES service_environments(id) ON "
        "DELETE RESTRICT"
    ),
    ("migration_plans", "pk_migration_plans"): ("PRIMARY KEY (id)"),
    ("migration_plan_logs", "ck_migration_plan_logs_data_is_object"): (
        "CHECK ((jsonb_typeof(data) = 'object'::text))"
    ),
    ("migration_plan_logs", "ck_migration_plan_logs_level_valid"): (
        "CHECK (((level)::text = ANY ((ARRAY['INFO'::character varying, 'WARN'::character "
        "varying, 'ERROR'::character varying])::text[])))"
    ),
    ("migration_plan_logs", "ck_migration_plan_logs_seq_positive"): ("CHECK ((seq >= 1))"),
    ("migration_plan_logs", "ck_migration_plan_logs_step_range"): (
        "CHECK (((step >= 0) AND (step <= 4)))"
    ),
    ("migration_plan_logs", "fk_migration_plan_logs_migration_plan_id_migration_plans"): (
        "FOREIGN KEY (migration_plan_id) REFERENCES migration_plans(id) ON DELETE RESTRICT"
    ),
    ("migration_plan_logs", "pk_migration_plan_logs"): ("PRIMARY KEY (id)"),
    ("migration_plan_logs", "uq_migration_plan_logs_migration_plan_id_seq"): (
        "UNIQUE (migration_plan_id, seq)"
    ),
    # 보안·거버넌스 2: 보안 증거 표
    ("findings", "ck_findings_ai_confirmation_consistency"): (
        "CHECK ((((source)::text = 'AI_REVIEW'::text) = (confirmation IS NOT NULL)))"
    ),
    ("findings", "ck_findings_ai_result_not_pass"): (
        "CHECK ((((source)::text <> 'AI_REVIEW'::text) OR ((result)::text = ANY "
        "((ARRAY['WARN'::character varying, 'FAIL'::character varying, 'UNKNOWN'::character "
        "varying])::text[]))))"
    ),
    ("findings", "ck_findings_confirmation_valid"): (
        "CHECK (((confirmation IS NULL) OR ((confirmation)::text = ANY "
        "((ARRAY['UNCONFIRMED'::character varying, 'ESCALATED'::character varying, "
        "'DISMISSED'::character varying])::text[]))))"
    ),
    ("findings", "ck_findings_control_id_valid"): (
        "CHECK (((control_id)::text = ANY ((ARRAY['C-IMG-1'::character varying, "
        "'C-IMG-2'::character varying, 'C-IMG-3'::character varying, 'C-CFG-1'::character "
        "varying, 'C-TLS-1'::character varying, 'C-TLS-2'::character varying, "
        "'C-NET-1'::character varying, 'C-NET-2'::character varying, 'C-IAM-1'::character "
        "varying, 'C-IAM-2'::character varying, 'C-SEC-1'::character varying, "
        "'C-AUD-1'::character varying, 'C-AUD-2'::character varying, 'C-CI-1'::character "
        "varying, 'C-AI-1'::character varying, 'C-DB-1'::character varying, "
        "'C-DST-1'::character varying, 'C-POST-1'::character varying])::text[])))"
    ),
    ("findings", "ck_findings_expires_after_collected"): "CHECK ((expires_at > collected_at))",
    ("findings", "ck_findings_image_digest_format"): (
        "CHECK (((image_digest IS NULL) OR ((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text)))"
    ),
    ("findings", "ck_findings_items_is_array"): "CHECK ((jsonb_typeof(items) = 'array'::text))",
    ("findings", "ck_findings_manual_review_has_basis"): (
        "CHECK ((((source)::text <> 'MANUAL_REVIEW'::text) OR ((reviewer_user_id IS NOT NULL) "
        "AND (basis_url IS NOT NULL))))"
    ),
    ("findings", "ck_findings_mode_valid"): (
        "CHECK (((mode)::text = ANY ((ARRAY['REAL'::character varying, 'CACHE'::character "
        "varying, 'SIMULATED'::character varying])::text[])))"
    ),
    ("findings", "ck_findings_result_valid"): (
        "CHECK (((result)::text = ANY ((ARRAY['PASS'::character varying, 'WARN'::character "
        "varying, 'FAIL'::character varying, 'UNKNOWN'::character varying, 'NA'::character "
        "varying])::text[])))"
    ),
    ("findings", "ck_findings_source_valid"): (
        "CHECK (((source)::text = ANY ((ARRAY['TRIVY_IMAGE'::character varying, "
        "'TRIVY_CONFIG'::character varying, 'TRIVY_SECRET'::character varying, "
        "'GITLEAKS'::character varying, 'TLS_PROBE'::character varying, "
        "'MANUAL_REVIEW'::character varying, 'WAS_TEST'::character varying, "
        "'WAS_STATE'::character varying, 'AUDIT_LOG'::character varying, "
        "'PRECHECK'::character varying, 'ENTRYPOINT'::character varying, "
        "'DATA_ENGINE'::character varying, 'CLOUD_API'::character varying, "
        "'KUBERNETES_API'::character varying, 'GITHUB_API'::character varying, "
        "'AI_REVIEW'::character varying])::text[])))"
    ),
    ("findings", "ck_findings_total_items_non_negative"): "CHECK ((total_items >= 0))",
    ("findings", "fk_findings_environment_id_environments"): (
        "FOREIGN KEY (environment_id) REFERENCES environments(id) ON DELETE RESTRICT"
    ),
    ("findings", "fk_findings_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("findings", "fk_findings_reviewer_user_id_users"): (
        "FOREIGN KEY (reviewer_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("findings", "fk_findings_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("findings", "pk_findings"): "PRIMARY KEY (id)",
    # --- 보안·AI 기반 스키마(201cf5419f4d) ---
    ("agent_patch_approvals", "ck_agent_patch_approvals_apply_run_only_when_approved"): (
        "CHECK ((((decision)::text = 'APPROVED'::text) OR (apply_run_id IS NULL)))"
    ),
    ("agent_patch_approvals", "ck_agent_patch_approvals_checks_digest_format"): (
        "CHECK (((checks_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text))"
    ),
    ("agent_patch_approvals", "ck_agent_patch_approvals_decision_valid"): (
        "CHECK (((decision)::text = ANY ((ARRAY['APPROVED'::character varying, "
        "'REJECTED'::character varying])::text[])))"
    ),
    ("agent_patch_approvals", "ck_agent_patch_approvals_patch_hash_format"): (
        "CHECK (((patch_hash)::text ~ '^sha256:[0-9a-f]{64}$'::text))"
    ),
    ("agent_patch_approvals", "fk_agent_patch_approvals_apply_run_id_agent_runs"): (
        "FOREIGN KEY (apply_run_id) REFERENCES agent_runs(id) ON DELETE RESTRICT"
    ),
    ("agent_patch_approvals", "fk_agent_patch_approvals_decided_by_user_id_users"): (
        "FOREIGN KEY (decided_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("agent_patch_approvals", "fk_agent_patch_approvals_run_id_agent_runs"): (
        "FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE RESTRICT"
    ),
    ("agent_patch_approvals", "pk_agent_patch_approvals"): "PRIMARY KEY (id)",
    ("agent_patch_approvals", "uq_agent_patch_approvals_run_id_patch_id"): (
        "UNIQUE (run_id, patch_id)"
    ),
    ("agent_run_events", "ck_agent_run_events_data_is_object"): (
        "CHECK (((data IS NULL) OR (jsonb_typeof(data) = 'object'::text)))"
    ),
    ("agent_run_events", "ck_agent_run_events_level_valid"): (
        "CHECK (((level)::text = ANY ((ARRAY['INFO'::character varying, 'WARN'::character "
        "varying, 'ERROR'::character varying])::text[])))"
    ),
    ("agent_run_events", "ck_agent_run_events_seq_positive"): "CHECK ((seq >= 1))",
    ("agent_run_events", "ck_agent_run_events_step_valid"): (
        "CHECK (((step)::text = ANY ((ARRAY['SANDBOX_CREATE'::character varying, "
        "'INPUT_LOAD'::character varying, 'CONFIG_DIFF'::character varying, "
        "'ANALYZE'::character varying, 'PATCH_PROPOSE'::character varying, "
        "'PATCH_VERIFY'::character varying, 'SANDBOX_DELETE'::character "
        "varying])::text[])))"
    ),
    ("agent_run_events", "fk_agent_run_events_run_id_agent_runs"): (
        "FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE RESTRICT"
    ),
    ("agent_run_events", "pk_agent_run_events"): "PRIMARY KEY (id)",
    ("agent_run_events", "uq_agent_run_events_run_id_seq"): "UNIQUE (run_id, seq)",
    ("agent_runners", "ck_agent_runners_name_format"): (
        "CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'::text))"
    ),
    ("agent_runners", "ck_agent_runners_token_hash_format"): (
        "CHECK ((token_hash ~ '^[0-9a-f]{64}$'::text))"
    ),
    ("agent_runners", "pk_agent_runners"): "PRIMARY KEY (id)",
    ("agent_runners", "uq_agent_runners_name"): "UNIQUE (name)",
    ("agent_runners", "uq_agent_runners_token_hash"): "UNIQUE (token_hash)",
    ("agent_runs", "ck_agent_runs_apply_patch_has_parent"): (
        "CHECK ((((kind)::text <> 'APPLY_PATCH'::text) OR (parent_run_id IS NOT NULL)))"
    ),
    ("agent_runs", "ck_agent_runs_claim_consistency"): (
        "CHECK ((((status)::text <> ALL ((ARRAY['CLAIMED'::character varying, "
        "'RUNNING'::character varying])::text[])) OR ((claimed_by_runner_id IS NOT NULL) "
        "AND (run_token_hash IS NOT NULL) AND (lease_until IS NOT NULL))))"
    ),
    ("agent_runs", "ck_agent_runs_deadline_after_created"): "CHECK ((deadline_at > created_at))",
    ("agent_runs", "ck_agent_runs_failure_code_only_when_failed"): (
        "CHECK (((failure_code IS NULL) OR ((status)::text = ANY "
        "((ARRAY['FAILED'::character varying, 'TIMED_OUT'::character varying])::text[]))))"
    ),
    ("agent_runs", "ck_agent_runs_finished_consistency"): (
        "CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, "
        "'FAILED'::character varying, 'TIMED_OUT'::character varying, "
        "'CANCELLED'::character varying])::text[])) = (finished_at IS NOT NULL)))"
    ),
    ("agent_runs", "ck_agent_runs_input_hash_format"): (
        "CHECK (((input_hash)::text ~ '^sha256:[0-9a-f]{64}$'::text))"
    ),
    ("agent_runs", "ck_agent_runs_input_is_object"): (
        "CHECK ((jsonb_typeof(input_snapshot) = 'object'::text))"
    ),
    ("agent_runs", "ck_agent_runs_kind_valid"): (
        "CHECK (((kind)::text = ANY ((ARRAY['PRECHECK'::character varying, "
        "'APPLY_PATCH'::character varying, 'CHANGE_PROPOSAL'::character "
        "varying])::text[])))"
    ),
    ("agent_runs", "ck_agent_runs_precheck_has_target"): (
        "CHECK ((((kind)::text <> 'PRECHECK'::text) OR (target_environment_id IS NOT NULL)))"
    ),
    ("agent_runs", "ck_agent_runs_result_only_when_reported"): (
        "CHECK (((result IS NULL) OR (((status)::text = ANY ((ARRAY['SUCCEEDED'::character "
        "varying, 'FAILED'::character varying])::text[])) AND (jsonb_typeof(result) = "
        "'object'::text))))"
    ),
    ("agent_runs", "ck_agent_runs_run_token_hash_format"): (
        "CHECK (((run_token_hash IS NULL) OR (run_token_hash ~ '^[0-9a-f]{64}$'::text)))"
    ),
    ("agent_runs", "ck_agent_runs_status_valid"): (
        "CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'CLAIMED'::character varying, 'RUNNING'::character varying, "
        "'SUCCEEDED'::character varying, 'FAILED'::character varying, "
        "'TIMED_OUT'::character varying, 'CANCELLED'::character varying])::text[])))"
    ),
    ("agent_runs", "fk_agent_runs_claimed_by_runner_id_agent_runners"): (
        "FOREIGN KEY (claimed_by_runner_id) REFERENCES agent_runners(id) ON DELETE RESTRICT"
    ),
    ("agent_runs", "fk_agent_runs_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("agent_runs", "fk_agent_runs_parent_run_id_agent_runs"): (
        "FOREIGN KEY (parent_run_id) REFERENCES agent_runs(id) ON DELETE RESTRICT"
    ),
    ("agent_runs", "fk_agent_runs_requested_by_user_id_users"): (
        "FOREIGN KEY (requested_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("agent_runs", "fk_agent_runs_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("agent_runs", "fk_agent_runs_source_environment_id_environments"): (
        "FOREIGN KEY (source_environment_id) REFERENCES environments(id) ON DELETE RESTRICT"
    ),
    ("agent_runs", "fk_agent_runs_target_environment_id_environments"): (
        "FOREIGN KEY (target_environment_id) REFERENCES environments(id) ON DELETE RESTRICT"
    ),
    ("agent_runs", "pk_agent_runs"): "PRIMARY KEY (id)",
    ("security_exceptions", "ck_security_exceptions_approval_consistency"): (
        "CHECK (((approved_by_user_id IS NULL) = (approved_at IS NULL)))"
    ),
    ("security_exceptions", "ck_security_exceptions_control_id_valid"): (
        "CHECK (((control_id)::text = ANY ((ARRAY['C-IMG-1'::character varying, "
        "'C-IMG-2'::character varying, 'C-IMG-3'::character varying, 'C-CFG-1'::character "
        "varying, 'C-TLS-1'::character varying, 'C-TLS-2'::character varying, "
        "'C-NET-1'::character varying, 'C-NET-2'::character varying, 'C-IAM-1'::character "
        "varying, 'C-IAM-2'::character varying, 'C-SEC-1'::character varying, "
        "'C-AUD-1'::character varying, 'C-AUD-2'::character varying, 'C-CI-1'::character "
        "varying, 'C-AI-1'::character varying, 'C-DB-1'::character varying, "
        "'C-DST-1'::character varying, 'C-POST-1'::character varying])::text[])))"
    ),
    ("security_exceptions", "ck_security_exceptions_expires_after_created"): (
        "CHECK ((expires_at > created_at))"
    ),
    ("security_exceptions", "ck_security_exceptions_image_digest_format"): (
        "CHECK (((image_digest IS NULL) OR ((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text)))"
    ),
    ("security_exceptions", "ck_security_exceptions_max_duration"): (
        "CHECK ((expires_at <= (created_at + '7 days'::interval)))"
    ),
    ("security_exceptions", "ck_security_exceptions_reason_present"): (
        "CHECK (((char_length(btrim(reason)) >= 1) AND (char_length(btrim(reason)) <= 1000)))"
    ),
    ("security_exceptions", "ck_security_exceptions_revocation_consistency"): (
        "CHECK (((revoked_by_user_id IS NULL) = (revoked_at IS NULL)))"
    ),
    ("security_exceptions", "fk_security_exceptions_approved_by_user_id_users"): (
        "FOREIGN KEY (approved_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("security_exceptions", "fk_security_exceptions_environment_id_environments"): (
        "FOREIGN KEY (environment_id) REFERENCES environments(id) ON DELETE RESTRICT"
    ),
    ("security_exceptions", "fk_security_exceptions_organization_id_organizations"): (
        "FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE RESTRICT"
    ),
    ("security_exceptions", "fk_security_exceptions_requested_by_user_id_users"): (
        "FOREIGN KEY (requested_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("security_exceptions", "fk_security_exceptions_revoked_by_user_id_users"): (
        "FOREIGN KEY (revoked_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"
    ),
    ("security_exceptions", "fk_security_exceptions_service_id_services"): (
        "FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE RESTRICT"
    ),
    ("security_exceptions", "pk_security_exceptions"): "PRIMARY KEY (id)",
    # API 서버 10: 고객 앱 환경 변수(값 칸 없음)
    ("service_environment_variables", "ck_service_environment_variables_key_format"): (
        "CHECK (((key)::text ~ '^[A-Z][A-Z0-9_]{0,63}$'::text))"
    ),
    ("service_environment_variables", "ck_service_environment_variables_latest_version_positive"): (
        "CHECK ((latest_version > 0))"
    ),
    ("service_environment_variables", "ck_service_environment_variables_soft_delete_consistency"): (
        "CHECK ((is_deleted = (deleted_at IS NOT NULL)))"
    ),
    ("service_environment_variables", "fk_service_environment_variables_connection"): (
        "FOREIGN KEY (service_environment_id) REFERENCES service_environments(id) "
        "ON DELETE RESTRICT"
    ),
    (
        "service_environment_variables",
        "fk_service_environment_variables_updated_by_user_id_users",
    ): ("FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE RESTRICT"),
    ("service_environment_variables", "pk_service_environment_variables"): "PRIMARY KEY (id)",
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
    # --- API 서버 7 ---
    "ix_audit_logs_organization_id_target_id": (
        "CREATE INDEX ix_audit_logs_organization_id_target_id ON audit_logs "
        "USING btree (organization_id, target_id)"
    ),
    # --- API 서버 8 ---
    "ix_migration_plans_service_id_created_at": (
        "CREATE INDEX ix_migration_plans_service_id_created_at ON migration_plans USING btree "
        "(service_id, created_at)"
    ),
    "pk_migration_plan_logs": (
        "CREATE UNIQUE INDEX pk_migration_plan_logs ON migration_plan_logs USING btree (id)"
    ),
    "pk_migration_plans": (
        "CREATE UNIQUE INDEX pk_migration_plans ON migration_plans USING btree (id)"
    ),
    "uq_migration_plan_logs_migration_plan_id_seq": (
        "CREATE UNIQUE INDEX uq_migration_plan_logs_migration_plan_id_seq ON migration_plan_logs "
        "USING btree (migration_plan_id, seq)"
    ),
    "uq_migration_plans_service_id_open": (
        "CREATE UNIQUE INDEX uq_migration_plans_service_id_open ON migration_plans USING btree "
        "(service_id) WHERE ((status)::text = ANY ((ARRAY['PENDING_APPROVAL'::character varying, "
        "'APPROVED'::character varying, 'RUNNING'::character varying, 'ROLLING_BACK'::character "
        "varying])::text[]))"
    ),
    # 보안·거버넌스 2: 보안 증거 표
    "ix_findings_organization_id_control_id_collected_at": (
        "CREATE INDEX ix_findings_organization_id_control_id_collected_at ON findings "
        "USING btree (organization_id, control_id, collected_at)"
    ),
    "pk_findings": "CREATE UNIQUE INDEX pk_findings ON findings USING btree (id)",
    # --- 보안·AI 기반 스키마(201cf5419f4d) ---
    "ix_agent_runs_organization_id_created_at": (
        "CREATE INDEX ix_agent_runs_organization_id_created_at ON agent_runs USING btree "
        "(organization_id, created_at)"
    ),
    "ix_agent_runs_queued_created_at": (
        "CREATE INDEX ix_agent_runs_queued_created_at ON agent_runs USING btree "
        "(created_at) WHERE ((status)::text = 'QUEUED'::text)"
    ),
    "ix_security_exceptions_organization_id_control_id_expires_at": (
        "CREATE INDEX ix_security_exceptions_organization_id_control_id_expires_at ON "
        "security_exceptions USING btree (organization_id, control_id, expires_at)"
    ),
    "pk_agent_patch_approvals": (
        "CREATE UNIQUE INDEX pk_agent_patch_approvals ON agent_patch_approvals USING btree (id)"
    ),
    "pk_agent_run_events": (
        "CREATE UNIQUE INDEX pk_agent_run_events ON agent_run_events USING btree (id)"
    ),
    "pk_agent_runners": "CREATE UNIQUE INDEX pk_agent_runners ON agent_runners USING btree (id)",
    "pk_agent_runs": "CREATE UNIQUE INDEX pk_agent_runs ON agent_runs USING btree (id)",
    "pk_security_exceptions": (
        "CREATE UNIQUE INDEX pk_security_exceptions ON security_exceptions USING btree (id)"
    ),
    "uq_agent_patch_approvals_run_id_patch_id": (
        "CREATE UNIQUE INDEX uq_agent_patch_approvals_run_id_patch_id ON "
        "agent_patch_approvals USING btree (run_id, patch_id)"
    ),
    "uq_agent_run_events_run_id_seq": (
        "CREATE UNIQUE INDEX uq_agent_run_events_run_id_seq ON agent_run_events USING "
        "btree (run_id, seq)"
    ),
    "uq_agent_runners_name": (
        "CREATE UNIQUE INDEX uq_agent_runners_name ON agent_runners USING btree (name)"
    ),
    "uq_agent_runners_token_hash": (
        "CREATE UNIQUE INDEX uq_agent_runners_token_hash ON agent_runners USING btree (token_hash)"
    ),
    "uq_agent_runs_run_token_hash": (
        "CREATE UNIQUE INDEX uq_agent_runs_run_token_hash ON agent_runs USING btree "
        "(run_token_hash) WHERE (run_token_hash IS NOT NULL)"
    ),
    "uq_agent_runs_service_id_open": (
        "CREATE UNIQUE INDEX uq_agent_runs_service_id_open ON agent_runs USING btree "
        "(service_id) WHERE ((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
        "'CLAIMED'::character varying, 'RUNNING'::character varying])::text[]))"
    ),
    # API 서버 10: 고객 앱 환경 변수
    "ix_service_environment_variables_connection_key": (
        "CREATE INDEX ix_service_environment_variables_connection_key ON "
        "service_environment_variables USING btree (service_environment_id, key)"
    ),
    "pk_service_environment_variables": (
        "CREATE UNIQUE INDEX pk_service_environment_variables ON service_environment_variables "
        "USING btree (id)"
    ),
    "uq_service_environment_variables_connection_key_live": (
        "CREATE UNIQUE INDEX uq_service_environment_variables_connection_key_live ON "
        "service_environment_variables USING btree (service_environment_id, key) "
        "WHERE (is_deleted = false)"
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
    # 감사 줄은 고치지도 않는다(API 서버 7 소유 함수).
    ("audit_logs", "trg_audit_logs_prevent_update"): (
        "CREATE TRIGGER trg_audit_logs_prevent_update BEFORE UPDATE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION prevent_append_only_update()"
    ),
    # 배포 작업 로그도 지우지 않는다(같은 공용 함수).
    ("deploy_request_logs", "trg_deploy_request_logs_prevent_delete"): (
        "CREATE TRIGGER trg_deploy_request_logs_prevent_delete BEFORE DELETE ON "
        "deploy_request_logs FOR EACH ROW EXECUTE FUNCTION prevent_append_only_delete()"
    ),
    # 이전 계획·실행 로그도 지우지 않는다(같은 공용 함수).
    ("migration_plans", "trg_migration_plans_prevent_delete"): (
        "CREATE TRIGGER trg_migration_plans_prevent_delete BEFORE DELETE ON migration_plans "
        "FOR EACH ROW EXECUTE FUNCTION prevent_append_only_delete()"
    ),
    ("migration_plan_logs", "trg_migration_plan_logs_prevent_delete"): (
        "CREATE TRIGGER trg_migration_plan_logs_prevent_delete BEFORE DELETE ON "
        "migration_plan_logs FOR EACH ROW EXECUTE FUNCTION prevent_append_only_delete()"
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
# 이전 계획을 더하기 전(API 서버 4 head) 시점, 원인 코드 TARGET_NOT_RECEIVING 을 더하기 전
# (API 서버 8 head) 시점, 계획 종류 mode 를 더하기 전(API 서버 8-2 head) 시점과 지금 head.
BEFORE_MIGRATION_PLANS = "33ad047a493c"
BEFORE_TARGET_NOT_RECEIVING = "4cea1ec99f1b"
BEFORE_PLAN_MODE = "6d89c52e39a4"
# 감사 로그 확장(API 서버 7) 리비전과 그 직전(앱 환경 변수 표 = API 서버 10) 시점.
BEFORE_AUDIT_EXTENSION = "48b2126a9c37"
AUDIT_EXTENSION_REVISION = "b7d41e6f2a93"
# 보안 게이트 원인 코드 SECURITY_GATE_BLOCKED 를 더하기 전(감사 로그 확장 = API 서버 7 head) 시점.
BEFORE_SECURITY_GATE_BLOCKED = AUDIT_EXTENSION_REVISION
HEAD_REVISION = "286d9ea982d6"
MODE_CHECK = "ck_migration_plans_mode_valid"
ERROR_CODE_CHECK = "ck_migration_plans_error_code_valid"
MIGRATION_PLAN_TABLES = {"migration_plans", "migration_plan_logs"}
MIGRATION_PLAN_TRIGGERS = {
    "trg_migration_plans_prevent_delete",
    "trg_migration_plan_logs_prevent_delete",
}
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


async def _forget_audit_rows(connection: AsyncConnection) -> None:
    """이 시험의 트랜잭션 안에서만 감사 줄을 비운다(시험 끝에 되돌린다).

    감사 줄이 있으면 그 revision 의 downgrade 가 먼저 멈춰(forward-only), 더 아래 revision 의 거절을
    볼 수 없다.
    """
    await connection.execute(text("TRUNCATE audit_logs"))


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
        "agent_patch_approvals",
        "agent_run_events",
        "agent_runners",
        "agent_runs",
        "agent_tokens",
        "alembic_version",
        "audit_logs",
        "builds",
        "deploy_request_logs",
        "deploy_requests",
        "deployments",
        "environments",
        "findings",
        "invitations",
        "jobs",
        "migration_plan_logs",
        "migration_plans",
        "organization_members",
        "organizations",
        "security_exceptions",
        "service_environment_variables",
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
        await _forget_audit_rows(connection)
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
    assert version == HEAD_REVISION


def _migration_plan_state(connection: Connection) -> tuple[set[str], bool, str, set[str], int]:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names()) & MIGRATION_PLAN_TABLES
    has_switch = "allow_self_approval" in {
        column["name"] for column in inspector.get_columns("organizations")
    }
    kind_check = next(
        check["sqltext"]
        for check in inspector.get_check_constraints("jobs")
        if check["name"] == "ck_jobs_kind_valid"
    )
    triggers = {
        str(row)
        for row in connection.scalars(
            text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'trg_mig%'")
        )
    }
    functions = connection.scalar(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'prevent_append_only_delete'")
    )
    return tables, has_switch, str(kind_check), triggers, int(functions or 0)


async def test_migration_plan_revision_downgrade_removes_tables_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text(
                "WITH u AS (INSERT INTO users (github_id, github_login) "
                "VALUES (2, 'legacy') RETURNING id) "
                "INSERT INTO organizations (slug, name, created_by_user_id) "
                "SELECT 'legacy-org', 'Legacy', id FROM u"
            )
        )
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_MIGRATION_PLANS))
        )
        down = await connection.run_sync(_migration_plan_state)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_migration_plan_state)
        legacy_switch = await connection.scalar(
            text("SELECT allow_self_approval FROM organizations WHERE slug = 'legacy-org'")
        )
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    tables_down, switch_down, kind_down, triggers_down, functions_down = down
    assert (tables_down, switch_down, triggers_down) == (set(), False, set())
    assert "MIGRATION_PLAN" not in kind_down
    # 공용 DELETE 금지 함수는 API 서버 3 소유라 내려도 남는다.
    assert functions_down == 1
    tables_up, switch_up, kind_up, triggers_up, functions_up = up
    assert (tables_up, switch_up, triggers_up) == (
        MIGRATION_PLAN_TABLES,
        True,
        MIGRATION_PLAN_TRIGGERS,
    )
    assert "MIGRATION_PLAN" in kind_up
    assert functions_up == 1
    # 기존 조직은 자기 승인이 막힌 채로 채워진다.
    assert legacy_switch is False


async def test_migration_plan_revision_downgrade_with_plan_jobs_fails_clearly(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text(
                "INSERT INTO jobs (kind, subject_id, run_after) "
                "VALUES ('MIGRATION_PLAN', gen_random_uuid(), now())"
            )
        )
        with pytest.raises(RuntimeError, match="jobs has 1 MIGRATION_PLAN rows") as raised:
            await connection.run_sync(
                run_alembic(lambda config: command.downgrade(config, BEFORE_MIGRATION_PLANS))
            )
        await transaction.rollback()
    async with engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))

    assert "does not delete job history" in str(raised.value)
    assert version == HEAD_REVISION


def _error_code_check(connection: Connection) -> str:
    return str(
        next(
            check["sqltext"]
            for check in inspect(connection).get_check_constraints("migration_plans")
            if check["name"] == ERROR_CODE_CHECK
        )
    )


async def test_target_not_receiving_revision_downgrade_narrows_check_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_TARGET_NOT_RECEIVING))
        )
        down = await connection.run_sync(_error_code_check)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_error_code_check)
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    assert "TARGET_NOT_RECEIVING" not in down
    assert "INTERNAL_ERROR" in down
    assert "TARGET_NOT_RECEIVING" in up


async def test_target_not_receiving_revision_downgrade_with_such_plans_fails_clearly(
    engine: AsyncEngine, migration: Migration
) -> None:
    # 실제 처리기로 TARGET_NOT_RECEIVING 롤백까지 간 계획 한 건(새 CHECK 로 저장된다).
    plan = await migration.create_plan(simulate={"failAtStep": 1, "reason": "TARGET_NOT_RECEIVING"})
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    stored = await migration.plan_row(plan["id"])

    async with engine.connect() as connection:
        transaction = await connection.begin()
        await _forget_audit_rows(connection)
        with pytest.raises(
            RuntimeError, match="migration_plans has 1 TARGET_NOT_RECEIVING rows"
        ) as raised:
            await connection.run_sync(
                run_alembic(lambda config: command.downgrade(config, BEFORE_TARGET_NOT_RECEIVING))
            )
        await transaction.rollback()
    async with engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))

    assert (stored.status, stored.error_code) == ("ROLLED_BACK", "TARGET_NOT_RECEIVING")
    assert "does not rewrite migration plan history" in str(raised.value)
    assert version == HEAD_REVISION


async def test_security_gate_revision_downgrade_narrows_check_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_SECURITY_GATE_BLOCKED))
        )
        down = await connection.run_sync(_error_code_check)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_error_code_check)
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    assert "SECURITY_GATE_BLOCKED" not in down
    assert "TARGET_NOT_RECEIVING" in down
    assert "SECURITY_GATE_BLOCKED" in up


async def test_security_gate_revision_downgrade_with_blocked_plans_fails_clearly(
    engine: AsyncEngine, migration: Migration
) -> None:
    # 실행 시작 때 게이트가 막아 FAILED(SECURITY_GATE_BLOCKED)로 끝난 계획 한 건.
    enable_gate(migration)
    await record_passing_evidence(migration)
    await add_exception(migration, SecurityControlId.DB_1)
    dst_exception = await add_exception(migration, SecurityControlId.DST_1)
    await add_precheck(migration)
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    await revoke_exception(migration, dst_exception)
    await migration.harness.run_until_idle()
    stored = await migration.plan_row(plan["id"])

    async with engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(
            RuntimeError, match="migration_plans has 1 SECURITY_GATE_BLOCKED rows"
        ) as raised:
            await connection.run_sync(
                run_alembic(lambda config: command.downgrade(config, BEFORE_SECURITY_GATE_BLOCKED))
            )
        await transaction.rollback()
    async with engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))

    assert (stored.status, stored.error_code) == ("FAILED", "SECURITY_GATE_BLOCKED")
    assert "does not rewrite migration plan history" in str(raised.value)
    assert version == HEAD_REVISION


def _mode_column_and_check(connection: Connection) -> tuple[bool, bool]:
    inspector = inspect(connection)
    has_column = any(
        column["name"] == "mode" for column in inspector.get_columns("migration_plans")
    )
    has_check = any(
        check["name"] == MODE_CHECK for check in inspector.get_check_constraints("migration_plans")
    )
    return has_column, has_check


async def test_plan_mode_revision_round_trip_fills_existing_plans_with_planned(
    engine: AsyncEngine, migration: Migration
) -> None:
    # 칸이 생기기 전부터 있던 계획 한 건(downgrade 로 칸을 지운 뒤 다시 더한다).
    plan = await migration.create_plan()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        await _forget_audit_rows(connection)
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_PLAN_MODE))
        )
        down = await connection.run_sync(_mode_column_and_check)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_mode_column_and_check)
        mode = await connection.scalar(
            text("SELECT mode FROM migration_plans WHERE id = :id"), {"id": plan["id"]}
        )
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    assert down == (False, False)
    assert up == (True, True)
    assert mode == "PLANNED"


# 보안 증거 표(보안·거버넌스 2) 직전 리비전.
BEFORE_FINDINGS = "a83f2c1d9e47"


def _has_findings_table(connection: Connection) -> bool:
    return "findings" in inspect(connection).get_table_names()


async def test_findings_revision_downgrade_removes_table_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_FINDINGS))
        )
        down = await connection.run_sync(_has_findings_table)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_has_findings_table)
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    assert (down, up) == (False, True)


def _audit_extension_state(connection: Connection) -> tuple[set[str], bool, set[str], int, int]:
    inspector = inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns("audit_logs")}
    new_columns = columns.keys() & AUDIT_NEW_COLUMNS
    triggers = connection.scalar(
        text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_audit_logs_prevent_update'")
    )
    functions = connection.scalar(
        text("SELECT count(*) FROM pg_proc WHERE proname = 'prevent_append_only_update'")
    )
    checks = {check["name"] or "" for check in inspector.get_check_constraints("audit_logs")}
    return (
        set(new_columns),
        bool(columns["path"]["nullable"] and columns["reason"]["nullable"]),
        checks,
        int(triggers or 0),
        int(functions or 0),
    )


AUDIT_NEW_COLUMNS = {
    "seq",
    "component",
    "route",
    "target_type",
    "target_id",
    "target_label",
    "details",
}
# 옛 파드의 거부 기록 INSERT 모양(옛 칸만, result 는 서버 기본값).
OLD_POD_DENIAL = (
    "INSERT INTO audit_logs (organization_id, actor_type, actor_user_id, action, path, reason) "
    "SELECT NULL, 'USER', id, 'POST /api/v1/orgs', '/api/v1/orgs', 'ORIGIN_NOT_ALLOWED' "
    "FROM users LIMIT 1"
)


async def test_audit_extension_revision_round_trip_keeps_old_shape_rows(
    harness: DeployHarness, engine: AsyncEngine
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(text(OLD_POD_DENIAL))
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_AUDIT_EXTENSION))
        )
        down = await connection.run_sync(_audit_extension_state)
        await connection.execute(text(OLD_POD_DENIAL))
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_audit_extension_state)
        await connection.run_sync(run_alembic(command.check))
        rows = (await connection.execute(text("SELECT seq, result, details FROM audit_logs"))).all()
        await transaction.rollback()

    assert (down[0], down[1], down[3], down[4]) == (set(), False, 0, 0)
    assert "ck_audit_logs_target_consistency" not in down[2]
    assert (up[0], up[1], up[3], up[4]) == (AUDIT_NEW_COLUMNS, True, 1, 1)
    assert "ck_audit_logs_target_consistency" in up[2]
    # 옛 행도 seq 가 채워진다.
    assert sorted(row.seq for row in rows) == [1, 2]
    assert {(row.result, str(row.details)) for row in rows} == {("DENIED", "{}")}


async def test_audit_extension_revision_downgrade_with_action_rows_fails_clearly(
    harness: DeployHarness, engine: AsyncEngine
) -> None:
    await harness.create_service()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        with pytest.raises(RuntimeError, match="forward-only"):
            await connection.run_sync(
                run_alembic(lambda config: command.downgrade(config, BEFORE_AUDIT_EXTENSION))
            )
        await transaction.rollback()
    async with engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))

    assert version == HEAD_REVISION


async def test_old_pod_denial_insert_succeeds_on_head(
    harness: DeployHarness, engine: AsyncEngine
) -> None:
    async with engine.begin() as connection:
        result = await connection.execute(text(OLD_POD_DENIAL))

    assert result.rowcount == 1


def _user_line(columns: str = "", values: str = "") -> str:
    return (
        f"INSERT INTO audit_logs (actor_type, actor_user_id, action, result{columns}) "
        f"SELECT 'USER', id, 'X', 'SUCCEEDED'{values} FROM users LIMIT 1"
    )


@pytest.mark.parametrize(
    ("statement", "violated"),
    [
        (_user_line(", component", ", 'api'"), "ck_audit_logs_actor_consistency"),
        (
            "INSERT INTO audit_logs (actor_type, action, result) VALUES ('SYSTEM', 'X', 'FAILED')",
            "ck_audit_logs_actor_consistency",
        ),
        (
            "INSERT INTO audit_logs (actor_type, component, action, result) "
            "VALUES ('ROBOT', 'api', 'X', 'FAILED')",
            # 제약은 이름순으로 검사한다. ROBOT 은 actor_type_valid 도 어긴다.
            "ck_audit_logs_actor_consistency",
        ),
        (_user_line(", target_type", ", 'service'"), "ck_audit_logs_target_consistency"),
        (_user_line(", target_label", ", 'shop-api'"), "ck_audit_logs_target_consistency"),
    ],
)
async def test_audit_logs_checks_reject_inconsistent_rows(
    harness: DeployHarness, engine: AsyncEngine, statement: str, violated: str
) -> None:
    with pytest.raises(IntegrityError, match=violated):
        async with engine.begin() as connection:
            await connection.execute(text(statement))


async def test_audit_logs_system_line_needs_only_component(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO audit_logs (actor_type, component, action, result) "
                "VALUES ('SYSTEM', 'deploy-worker', 'DEPLOY_FINISHED', 'FAILED')"
            )
        )
        count = await connection.scalar(
            text("SELECT count(*) FROM audit_logs WHERE actor_type = 'SYSTEM'")
        )

    assert count == 1


async def test_audit_logs_cannot_be_updated(harness: DeployHarness, engine: AsyncEngine) -> None:
    await harness.create_service()

    with pytest.raises(DBAPIError, match="audit_logs rows are append-only and cannot be updated"):
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE audit_logs SET action = 'TAMPERED'"))
    with pytest.raises(DBAPIError, match="audit_logs rows are append-only"):
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM audit_logs"))


# 고객 앱 환경 변수 표(API 서버 10) 리비전과 그 직전 리비전(보안·AI 기반 스키마). 더하기만 한다.
# 그 뒤에 감사 로그 확장(b7d41e6f2a93), 보안 게이트 원인 코드(286d9ea982d6)가 이어진다.
SERVICE_VARIABLES_REVISION = BEFORE_AUDIT_EXTENSION
BEFORE_SERVICE_VARIABLES = "201cf5419f4d"
SERVICE_VARIABLES_TABLE = "service_environment_variables"


def _table_names(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def test_service_variables_revision_is_single_head_after_security_ai_schema() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)

    # 감사 로그 확장(API 서버 7)이 이 리비전을 잇는다.
    assert scripts.get_heads() == [HEAD_REVISION]
    gate = scripts.get_revision(HEAD_REVISION)
    assert gate is not None and gate.down_revision == AUDIT_EXTENSION_REVISION
    audit = scripts.get_revision(AUDIT_EXTENSION_REVISION)
    assert audit is not None and audit.down_revision == SERVICE_VARIABLES_REVISION
    service_variables = scripts.get_revision(SERVICE_VARIABLES_REVISION)
    assert service_variables is not None
    assert service_variables.down_revision == BEFORE_SERVICE_VARIABLES


async def test_service_variables_revision_only_adds_a_table_and_has_no_value_column(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        up = await connection.run_sync(_table_names)
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_SERVICE_VARIABLES))
        )
        down = await connection.run_sync(_table_names)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        columns = await connection.run_sync(
            lambda sync: {
                column["name"] for column in inspect(sync).get_columns(SERVICE_VARIABLES_TABLE)
            }
        )
        await connection.run_sync(run_alembic(command.check))
        await transaction.rollback()

    assert up - down == {SERVICE_VARIABLES_TABLE}
    assert down - up == set()
    assert columns == {
        "id",
        "service_environment_id",
        "key",
        "secret_name",
        "latest_version",
        "updated_by_user_id",
        "created_at",
        "updated_at",
        "is_deleted",
        "deleted_at",
    }


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


BEFORE_AGENT_RUNS = "eee7a6c8229f"
AGENT_TABLES = {
    "security_exceptions",
    "agent_runners",
    "agent_runs",
    "agent_run_events",
    "agent_patch_approvals",
}


def _agent_tables(connection: Connection) -> set[str]:
    return AGENT_TABLES & set(inspect(connection).get_table_names())


async def test_agent_runs_revision_downgrade_removes_tables_and_upgrade_restores(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(
            run_alembic(lambda config: command.downgrade(config, BEFORE_AGENT_RUNS))
        )
        down = await connection.run_sync(_agent_tables)
        await connection.run_sync(run_alembic(lambda config: command.upgrade(config, "head")))
        up = await connection.run_sync(_agent_tables)

    assert down == set()
    assert up == AGENT_TABLES
