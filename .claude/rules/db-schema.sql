-- 물리 DB 스키마(DDL)의 단일 출처. 직접 고치지 않는다.
-- 생성: scripts/dump_schema.sh (pg_dump --schema-only --no-owner --no-privileges)
-- 마이그레이션 버전 표(alembic_version)는 뺀다.


CREATE FUNCTION public.prevent_append_only_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION '% rows are append-only and cannot be deleted', TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$;

CREATE FUNCTION public.prevent_deployment_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'deployments rows are append-only and cannot be deleted'
                USING ERRCODE = 'restrict_violation';
        END;
        $$;

CREATE TABLE public.agent_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    token_hash character(64) NOT NULL,
    last4 character(4) NOT NULL,
    created_by_user_id uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.audit_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid,
    actor_type character varying(16) NOT NULL,
    actor_user_id uuid,
    actor_agent_token_id uuid,
    action character varying(300) NOT NULL,
    path text NOT NULL,
    reason character varying(64) NOT NULL,
    result character varying(16) DEFAULT 'DENIED'::character varying NOT NULL,
    request_id character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_audit_logs_actor_consistency CHECK (((((actor_type)::text = 'USER'::text) AND (actor_user_id IS NOT NULL) AND (actor_agent_token_id IS NULL)) OR (((actor_type)::text = 'AGENT'::text) AND (actor_agent_token_id IS NOT NULL) AND (actor_user_id IS NULL)))),
    CONSTRAINT ck_audit_logs_actor_type_valid CHECK (((actor_type)::text = ANY ((ARRAY['USER'::character varying, 'AGENT'::character varying])::text[]))),
    CONSTRAINT ck_audit_logs_result_valid CHECK (((result)::text = 'DENIED'::text))
);

CREATE TABLE public.builds (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    service_id uuid NOT NULL,
    repo_url character varying(300) NOT NULL,
    commit_sha character(40) NOT NULL,
    status character varying(16) DEFAULT 'QUEUED'::character varying NOT NULL,
    image_repository character varying(512),
    image_digest character varying(71),
    start_requested_at timestamp with time zone,
    external_ref character varying(255),
    error_code character varying(32),
    simulated boolean NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_builds_commit_sha_format CHECK ((commit_sha ~ '^[0-9a-f]{40}$'::text)),
    CONSTRAINT ck_builds_error_code_consistency CHECK ((((status)::text = 'FAILED'::text) = (error_code IS NOT NULL))),
    CONSTRAINT ck_builds_finished_at_consistency CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, 'FAILED'::character varying])::text[])) = (finished_at IS NOT NULL))),
    CONSTRAINT ck_builds_image_consistency CHECK ((((status)::text = 'SUCCEEDED'::text) = ((image_digest IS NOT NULL) AND (image_repository IS NOT NULL)))),
    CONSTRAINT ck_builds_image_digest_format CHECK (((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_builds_status_valid CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, 'RUNNING'::character varying, 'SUCCEEDED'::character varying, 'FAILED'::character varying])::text[])))
);

CREATE TABLE public.deploy_request_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    deploy_request_id uuid NOT NULL,
    seq integer NOT NULL,
    logged_at timestamp with time zone NOT NULL,
    level character varying(8) NOT NULL,
    stage character varying(16) NOT NULL,
    code character varying(48) NOT NULL,
    message character varying(500) NOT NULL,
    simulated boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_deploy_request_logs_level_valid CHECK (((level)::text = ANY ((ARRAY['INFO'::character varying, 'WARN'::character varying, 'ERROR'::character varying])::text[]))),
    CONSTRAINT ck_deploy_request_logs_seq_positive CHECK ((seq >= 1))
);

CREATE TABLE public.deploy_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    service_id uuid NOT NULL,
    environment_id uuid NOT NULL,
    service_environment_id uuid NOT NULL,
    trigger character varying(16) NOT NULL,
    source_request_id uuid,
    ref_type character varying(8) NOT NULL,
    ref_value character varying(255) NOT NULL,
    repo_url character varying(300) NOT NULL,
    commit_sha character(40) NOT NULL,
    status character varying(16) DEFAULT 'QUEUED'::character varying NOT NULL,
    cancel_reason character varying(16),
    superseded_by_id uuid,
    error_code character varying(32),
    build_id uuid,
    build_reused boolean,
    deployment_id uuid,
    deploy_requested_at timestamp with time zone,
    deploy_external_ref character varying(255),
    revert_status character varying(16),
    revert_external_ref character varying(255),
    last_health_result character varying(16),
    config_snapshot jsonb NOT NULL,
    simulate jsonb,
    simulated boolean NOT NULL,
    building_at timestamp with time zone,
    deploying_at timestamp with time zone,
    verifying_at timestamp with time zone,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    rollback_of_request_id uuid,
    rollback_base_deployment_id uuid,
    CONSTRAINT ck_deploy_requests_build_reused_consistency CHECK (((build_id IS NULL) = (build_reused IS NULL))),
    CONSTRAINT ck_deploy_requests_cancel_reason_consistency CHECK ((((status)::text = 'CANCELLED'::text) = (cancel_reason IS NOT NULL))),
    CONSTRAINT ck_deploy_requests_cancel_reason_valid CHECK (((cancel_reason)::text = ANY ((ARRAY['SUPERSEDED'::character varying, 'SERVICE_DELETED'::character varying, 'BASE_CHANGED'::character varying])::text[]))),
    CONSTRAINT ck_deploy_requests_commit_sha_format CHECK ((commit_sha ~ '^[0-9a-f]{40}$'::text)),
    CONSTRAINT ck_deploy_requests_config_snapshot_is_object CHECK ((jsonb_typeof(config_snapshot) = 'object'::text)),
    CONSTRAINT ck_deploy_requests_error_code_consistency CHECK ((((status)::text = 'FAILED'::text) = (error_code IS NOT NULL))),
    CONSTRAINT ck_deploy_requests_finished_at_consistency CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, 'FAILED'::character varying, 'CANCELLED'::character varying])::text[])) = (finished_at IS NOT NULL))),
    CONSTRAINT ck_deploy_requests_last_health_result_valid CHECK (((last_health_result)::text = ANY ((ARRAY['OK'::character varying, 'HTTP_ERROR'::character varying, 'UNREACHABLE'::character varying])::text[]))),
    CONSTRAINT ck_deploy_requests_ref_type_valid CHECK (((ref_type)::text = ANY ((ARRAY['BRANCH'::character varying, 'COMMIT'::character varying])::text[]))),
    CONSTRAINT ck_deploy_requests_revert_status_valid CHECK (((revert_status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, 'FAILED'::character varying])::text[]))),
    CONSTRAINT ck_deploy_requests_rollback_base_consistency CHECK ((((trigger)::text = 'ROLLBACK'::text) = (rollback_base_deployment_id IS NOT NULL))),
    CONSTRAINT ck_deploy_requests_rollback_of_request_consistency CHECK ((((trigger)::text = 'ROLLBACK'::text) = (rollback_of_request_id IS NOT NULL))),
    CONSTRAINT ck_deploy_requests_rollback_of_request_not_self CHECK ((rollback_of_request_id <> id)),
    CONSTRAINT ck_deploy_requests_simulate_is_object CHECK ((jsonb_typeof(simulate) = 'object'::text)),
    CONSTRAINT ck_deploy_requests_source_request_consistency CHECK ((((trigger)::text = 'STANDBY_SYNC'::text) = (source_request_id IS NOT NULL))),
    CONSTRAINT ck_deploy_requests_status_valid CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, 'BUILDING'::character varying, 'DEPLOYING'::character varying, 'VERIFYING'::character varying, 'SUCCEEDED'::character varying, 'FAILED'::character varying, 'CANCELLED'::character varying])::text[]))),
    CONSTRAINT ck_deploy_requests_trigger_valid CHECK (((trigger)::text = ANY ((ARRAY['MANUAL'::character varying, 'STANDBY_SYNC'::character varying, 'ROLLBACK'::character varying])::text[])))
);

CREATE TABLE public.deployments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    service_environment_id uuid NOT NULL,
    image_repository character varying(512) NOT NULL,
    image_digest character varying(71) NOT NULL,
    config_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(16) DEFAULT 'PENDING'::character varying NOT NULL,
    rollback_of_id uuid,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_deployments_config_snapshot_is_object CHECK ((jsonb_typeof(config_snapshot) = 'object'::text)),
    CONSTRAINT ck_deployments_image_digest_format CHECK (((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_deployments_rollback_of_not_self CHECK ((rollback_of_id <> id)),
    CONSTRAINT ck_deployments_status_valid CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'IN_PROGRESS'::character varying, 'SUCCEEDED'::character varying, 'FAILED'::character varying, 'CANCELLED'::character varying])::text[])))
);

CREATE TABLE public.environments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(63) NOT NULL,
    kind character varying(32) NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    secret_ref character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    connection_status character varying(16) DEFAULT 'UNCHECKED'::character varying NOT NULL,
    connection_checked_at timestamp with time zone,
    connection_error character varying(32),
    organization_id uuid NOT NULL,
    CONSTRAINT ck_environments_config_is_object CHECK ((jsonb_typeof(config) = 'object'::text)),
    CONSTRAINT ck_environments_connection_checked_at_consistency CHECK ((((connection_status)::text = 'UNCHECKED'::text) = (connection_checked_at IS NULL))),
    CONSTRAINT ck_environments_connection_error_consistency CHECK ((((connection_status)::text = 'FAILED'::text) = (connection_error IS NOT NULL))),
    CONSTRAINT ck_environments_connection_status_valid CHECK (((connection_status)::text = ANY ((ARRAY['UNCHECKED'::character varying, 'CONNECTED'::character varying, 'FAILED'::character varying])::text[]))),
    CONSTRAINT ck_environments_name_format CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'::text)),
    CONSTRAINT ck_environments_soft_delete_consistency CHECK ((is_deleted = (deleted_at IS NOT NULL)))
);

CREATE TABLE public.findings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    service_id uuid,
    environment_id uuid,
    image_digest character varying(71),
    config_commit character varying(40),
    was_version character varying(64),
    control_id character varying(16) NOT NULL,
    result character varying(16) NOT NULL,
    mode character varying(16) NOT NULL,
    source character varying(32) NOT NULL,
    reason character varying(64) NOT NULL,
    policy_version character varying(32) NOT NULL,
    tool_version character varying(32),
    source_ref character varying(500),
    items jsonb DEFAULT '[]'::jsonb NOT NULL,
    total_items integer DEFAULT 0 NOT NULL,
    reviewer_user_id uuid,
    basis_url text,
    confirmation character varying(16),
    collected_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_findings_ai_confirmation_consistency CHECK ((((source)::text = 'AI_REVIEW'::text) = (confirmation IS NOT NULL))),
    CONSTRAINT ck_findings_ai_result_not_pass CHECK ((((source)::text <> 'AI_REVIEW'::text) OR ((result)::text = ANY ((ARRAY['WARN'::character varying, 'FAIL'::character varying, 'UNKNOWN'::character varying])::text[])))),
    CONSTRAINT ck_findings_confirmation_valid CHECK (((confirmation IS NULL) OR ((confirmation)::text = ANY ((ARRAY['UNCONFIRMED'::character varying, 'ESCALATED'::character varying, 'DISMISSED'::character varying])::text[])))),
    CONSTRAINT ck_findings_control_id_valid CHECK (((control_id)::text = ANY ((ARRAY['C-IMG-1'::character varying, 'C-IMG-2'::character varying, 'C-IMG-3'::character varying, 'C-CFG-1'::character varying, 'C-TLS-1'::character varying, 'C-TLS-2'::character varying, 'C-NET-1'::character varying, 'C-NET-2'::character varying, 'C-IAM-1'::character varying, 'C-IAM-2'::character varying, 'C-SEC-1'::character varying, 'C-AUD-1'::character varying, 'C-AUD-2'::character varying, 'C-CI-1'::character varying, 'C-AI-1'::character varying, 'C-DB-1'::character varying, 'C-DST-1'::character varying, 'C-POST-1'::character varying])::text[]))),
    CONSTRAINT ck_findings_expires_after_collected CHECK ((expires_at > collected_at)),
    CONSTRAINT ck_findings_image_digest_format CHECK (((image_digest IS NULL) OR ((image_digest)::text ~ '^sha256:[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_findings_items_is_array CHECK ((jsonb_typeof(items) = 'array'::text)),
    CONSTRAINT ck_findings_manual_review_has_basis CHECK ((((source)::text <> 'MANUAL_REVIEW'::text) OR ((reviewer_user_id IS NOT NULL) AND (basis_url IS NOT NULL)))),
    CONSTRAINT ck_findings_mode_valid CHECK (((mode)::text = ANY ((ARRAY['REAL'::character varying, 'CACHE'::character varying, 'SIMULATED'::character varying])::text[]))),
    CONSTRAINT ck_findings_result_valid CHECK (((result)::text = ANY ((ARRAY['PASS'::character varying, 'WARN'::character varying, 'FAIL'::character varying, 'UNKNOWN'::character varying, 'NA'::character varying])::text[]))),
    CONSTRAINT ck_findings_source_valid CHECK (((source)::text = ANY ((ARRAY['TRIVY_IMAGE'::character varying, 'TRIVY_CONFIG'::character varying, 'TRIVY_SECRET'::character varying, 'GITLEAKS'::character varying, 'TLS_PROBE'::character varying, 'MANUAL_REVIEW'::character varying, 'WAS_TEST'::character varying, 'WAS_STATE'::character varying, 'AUDIT_LOG'::character varying, 'PRECHECK'::character varying, 'ENTRYPOINT'::character varying, 'DATA_ENGINE'::character varying, 'CLOUD_API'::character varying, 'KUBERNETES_API'::character varying, 'GITHUB_API'::character varying, 'AI_REVIEW'::character varying])::text[]))),
    CONSTRAINT ck_findings_total_items_non_negative CHECK ((total_items >= 0))
);

CREATE TABLE public.invitations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    github_id bigint NOT NULL,
    github_login character varying(39) NOT NULL,
    role character varying(16) NOT NULL,
    status character varying(16) NOT NULL,
    invited_by_user_id uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    accepted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_invitations_accepted_at_consistency CHECK ((((status)::text = 'ACCEPTED'::text) = (accepted_at IS NOT NULL))),
    CONSTRAINT ck_invitations_role_valid CHECK (((role)::text = ANY ((ARRAY['ADMIN'::character varying, 'DEVELOPER'::character varying, 'VIEWER'::character varying])::text[]))),
    CONSTRAINT ck_invitations_status_valid CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'ACCEPTED'::character varying, 'EXPIRED'::character varying, 'REVOKED'::character varying])::text[])))
);

CREATE TABLE public.jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind character varying(32) NOT NULL,
    subject_id uuid NOT NULL,
    status character varying(16) DEFAULT 'QUEUED'::character varying NOT NULL,
    run_after timestamp with time zone NOT NULL,
    lease_token uuid,
    lease_owner character varying(128),
    lease_expires_at timestamp with time zone,
    lease_losses integer DEFAULT 0 NOT NULL,
    last_error_code character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_jobs_kind_valid CHECK (((kind)::text = ANY ((ARRAY['DEPLOY_REQUEST'::character varying, 'SERVICE_TEARDOWN'::character varying, 'MIGRATION_PLAN'::character varying])::text[]))),
    CONSTRAINT ck_jobs_lease_consistency CHECK ((((status)::text = 'RUNNING'::text) = ((lease_token IS NOT NULL) AND (lease_expires_at IS NOT NULL)))),
    CONSTRAINT ck_jobs_lease_losses_non_negative CHECK ((lease_losses >= 0)),
    CONSTRAINT ck_jobs_status_valid CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, 'RUNNING'::character varying, 'DONE'::character varying, 'FAILED'::character varying, 'CANCELLED'::character varying])::text[])))
);

CREATE TABLE public.migration_plan_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    migration_plan_id uuid NOT NULL,
    seq integer NOT NULL,
    step smallint,
    logged_at timestamp with time zone NOT NULL,
    level character varying(8) NOT NULL,
    code character varying(48) NOT NULL,
    message character varying(500) NOT NULL,
    data jsonb,
    simulated boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_migration_plan_logs_data_is_object CHECK ((jsonb_typeof(data) = 'object'::text)),
    CONSTRAINT ck_migration_plan_logs_level_valid CHECK (((level)::text = ANY ((ARRAY['INFO'::character varying, 'WARN'::character varying, 'ERROR'::character varying])::text[]))),
    CONSTRAINT ck_migration_plan_logs_seq_positive CHECK ((seq >= 1)),
    CONSTRAINT ck_migration_plan_logs_step_range CHECK (((step >= 0) AND (step <= 4)))
);

CREATE TABLE public.migration_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    service_id uuid NOT NULL,
    source_service_environment_id uuid NOT NULL,
    target_service_environment_id uuid NOT NULL,
    status character varying(16) NOT NULL,
    steps jsonb NOT NULL,
    step_results jsonb NOT NULL,
    rollback_conditions jsonb NOT NULL,
    observe_seconds integer NOT NULL,
    expected_seconds integer NOT NULL,
    fingerprint jsonb NOT NULL,
    simulate jsonb,
    simulated boolean NOT NULL,
    current_step smallint DEFAULT 0 NOT NULL,
    step_phase character varying(8),
    phase_started_at timestamp with time zone,
    observe_until timestamp with time zone,
    traffic_requested_at timestamp with time zone,
    rollback_requested_at timestamp with time zone,
    rollback_attempts smallint DEFAULT 0 NOT NULL,
    error_code character varying(32),
    rollback_status character varying(16),
    created_by_user_id uuid NOT NULL,
    approved_by_user_id uuid,
    rejected_by_user_id uuid,
    cancelled_by_user_id uuid,
    aborted_by_user_id uuid,
    self_approved boolean DEFAULT false NOT NULL,
    approved_at timestamp with time zone,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    abort_requested_at timestamp with time zone,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    mode character varying(16) DEFAULT 'PLANNED'::character varying NOT NULL,
    CONSTRAINT ck_migration_plans_approved_consistency CHECK (((approved_by_user_id IS NULL) = (approved_at IS NULL))),
    CONSTRAINT ck_migration_plans_current_step_range CHECK (((current_step >= 0) AND (current_step <= 4))),
    CONSTRAINT ck_migration_plans_error_code_consistency CHECK ((((status)::text = ANY ((ARRAY['ROLLING_BACK'::character varying, 'ROLLED_BACK'::character varying, 'FAILED'::character varying])::text[])) = (error_code IS NOT NULL))),
    CONSTRAINT ck_migration_plans_error_code_valid CHECK (((error_code)::text = ANY ((ARRAY['PREFLIGHT_FAILED'::character varying, 'TRAFFIC_APPLY_FAILED'::character varying, 'HEALTH_CHECK_FAILED'::character varying, 'METRICS_UNAVAILABLE'::character varying, 'THRESHOLD_EXCEEDED'::character varying, 'DATA_LAG_EXCEEDED'::character varying, 'ABORTED'::character varying, 'WORKER_LOST'::character varying, 'INTERNAL_ERROR'::character varying, 'TARGET_NOT_RECEIVING'::character varying])::text[]))),
    CONSTRAINT ck_migration_plans_expected_seconds_non_negative CHECK ((expected_seconds >= 0)),
    CONSTRAINT ck_migration_plans_fingerprint_is_object CHECK ((jsonb_typeof(fingerprint) = 'object'::text)),
    CONSTRAINT ck_migration_plans_finished_at_consistency CHECK ((((status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, 'ROLLED_BACK'::character varying, 'FAILED'::character varying, 'REJECTED'::character varying, 'CANCELLED'::character varying, 'EXPIRED'::character varying, 'STALE'::character varying])::text[])) = (finished_at IS NOT NULL))),
    CONSTRAINT ck_migration_plans_mode_valid CHECK (((mode)::text = ANY ((ARRAY['PLANNED'::character varying, 'EMERGENCY'::character varying, 'FAILBACK'::character varying])::text[]))),
    CONSTRAINT ck_migration_plans_observe_seconds_range CHECK (((observe_seconds >= 1) AND (observe_seconds <= 600))),
    CONSTRAINT ck_migration_plans_rollback_attempts_non_negative CHECK ((rollback_attempts >= 0)),
    CONSTRAINT ck_migration_plans_rollback_conditions_is_object CHECK ((jsonb_typeof(rollback_conditions) = 'object'::text)),
    CONSTRAINT ck_migration_plans_rollback_status_valid CHECK (((rollback_status)::text = ANY ((ARRAY['SUCCEEDED'::character varying, 'FAILED'::character varying, 'NOT_NEEDED'::character varying])::text[]))),
    CONSTRAINT ck_migration_plans_simulate_is_object CHECK ((jsonb_typeof(simulate) = 'object'::text)),
    CONSTRAINT ck_migration_plans_status_valid CHECK (((status)::text = ANY ((ARRAY['PENDING_APPROVAL'::character varying, 'APPROVED'::character varying, 'RUNNING'::character varying, 'ROLLING_BACK'::character varying, 'SUCCEEDED'::character varying, 'ROLLED_BACK'::character varying, 'FAILED'::character varying, 'REJECTED'::character varying, 'CANCELLED'::character varying, 'EXPIRED'::character varying, 'STALE'::character varying])::text[]))),
    CONSTRAINT ck_migration_plans_step_phase_valid CHECK (((step_phase)::text = ANY ((ARRAY['APPLY'::character varying, 'OBSERVE'::character varying, 'JUDGE'::character varying])::text[]))),
    CONSTRAINT ck_migration_plans_step_results_is_array CHECK ((jsonb_typeof(step_results) = 'array'::text)),
    CONSTRAINT ck_migration_plans_steps_is_array CHECK ((jsonb_typeof(steps) = 'array'::text)),
    CONSTRAINT ck_migration_plans_target_not_source CHECK ((target_service_environment_id <> source_service_environment_id))
);

CREATE TABLE public.organization_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role character varying(16) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_organization_members_role_valid CHECK (((role)::text = ANY ((ARRAY['ADMIN'::character varying, 'DEVELOPER'::character varying, 'VIEWER'::character varying])::text[])))
);

CREATE TABLE public.organizations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug character varying(39) NOT NULL,
    name character varying(100) NOT NULL,
    personal boolean DEFAULT false NOT NULL,
    created_by_user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    allow_self_approval boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_organizations_name_not_empty CHECK ((char_length((name)::text) >= 1)),
    CONSTRAINT ck_organizations_slug_format CHECK (((slug)::text ~ '^[a-z0-9](-?[a-z0-9]){0,38}$'::text))
);

CREATE TABLE public.service_environments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    service_id uuid NOT NULL,
    environment_id uuid NOT NULL,
    current_deployment_id uuid,
    traffic_weight smallint DEFAULT 0 NOT NULL,
    traffic_applied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    public_host character varying(253) NOT NULL,
    teardown_attempt integer DEFAULT 0 NOT NULL,
    teardown_requested_at timestamp with time zone,
    teardown_external_ref character varying(255),
    teardown_error character varying(32),
    CONSTRAINT ck_service_environments_soft_delete_consistency CHECK ((is_deleted = (deleted_at IS NOT NULL))),
    CONSTRAINT ck_service_environments_traffic_weight_range CHECK (((traffic_weight >= 0) AND (traffic_weight <= 100)))
);

CREATE TABLE public.services (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(63) NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    repo_url character varying(300) NOT NULL,
    repo_private boolean NOT NULL,
    health_check_path character varying(255) DEFAULT '/healthz'::character varying NOT NULL,
    health_check_timeout_seconds integer DEFAULT 300 NOT NULL,
    container_port integer DEFAULT 8080 NOT NULL,
    public_host character varying(253) NOT NULL,
    status character varying(16) DEFAULT 'ACTIVE'::character varying NOT NULL,
    delete_error character varying(32),
    simulated boolean DEFAULT false NOT NULL,
    organization_id uuid NOT NULL,
    host_suffix character(8) NOT NULL,
    CONSTRAINT ck_services_container_port_range CHECK (((container_port >= 1) AND (container_port <= 65535))),
    CONSTRAINT ck_services_delete_error_only_when_deleting CHECK (((delete_error IS NULL) OR ((status)::text = 'DELETING'::text))),
    CONSTRAINT ck_services_health_check_path_format CHECK (((health_check_path)::text ~~ '/%'::text)),
    CONSTRAINT ck_services_health_check_timeout_range CHECK (((health_check_timeout_seconds >= 10) AND (health_check_timeout_seconds <= 1800))),
    CONSTRAINT ck_services_host_suffix_format CHECK ((host_suffix ~ '^[a-z0-9]{8}$'::text)),
    CONSTRAINT ck_services_name_format CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'::text)),
    CONSTRAINT ck_services_soft_delete_consistency CHECK ((is_deleted = (deleted_at IS NOT NULL))),
    CONSTRAINT ck_services_status_valid CHECK (((status)::text = ANY ((ARRAY['ACTIVE'::character varying, 'DELETING'::character varying])::text[])))
);

CREATE TABLE public.sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    github_id bigint NOT NULL,
    github_login character varying(39) NOT NULL,
    name character varying(255),
    avatar_url character varying(500),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT pk_agent_tokens PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT pk_audit_logs PRIMARY KEY (id);

ALTER TABLE ONLY public.builds
    ADD CONSTRAINT pk_builds PRIMARY KEY (id);

ALTER TABLE ONLY public.deploy_request_logs
    ADD CONSTRAINT pk_deploy_request_logs PRIMARY KEY (id);

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT pk_deploy_requests PRIMARY KEY (id);

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT pk_deployments PRIMARY KEY (id);

ALTER TABLE ONLY public.environments
    ADD CONSTRAINT pk_environments PRIMARY KEY (id);

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT pk_findings PRIMARY KEY (id);

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT pk_invitations PRIMARY KEY (id);

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT pk_jobs PRIMARY KEY (id);

ALTER TABLE ONLY public.migration_plan_logs
    ADD CONSTRAINT pk_migration_plan_logs PRIMARY KEY (id);

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT pk_migration_plans PRIMARY KEY (id);

ALTER TABLE ONLY public.organization_members
    ADD CONSTRAINT pk_organization_members PRIMARY KEY (id);

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT pk_organizations PRIMARY KEY (id);

ALTER TABLE ONLY public.service_environments
    ADD CONSTRAINT pk_service_environments PRIMARY KEY (id);

ALTER TABLE ONLY public.services
    ADD CONSTRAINT pk_services PRIMARY KEY (id);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT pk_sessions PRIMARY KEY (id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT uq_agent_tokens_token_hash UNIQUE (token_hash);

ALTER TABLE ONLY public.deploy_request_logs
    ADD CONSTRAINT uq_deploy_request_logs_deploy_request_id_seq UNIQUE (deploy_request_id, seq);

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT uq_deployments_id_service_environment_id UNIQUE (id, service_environment_id);

ALTER TABLE ONLY public.migration_plan_logs
    ADD CONSTRAINT uq_migration_plan_logs_migration_plan_id_seq UNIQUE (migration_plan_id, seq);

ALTER TABLE ONLY public.organization_members
    ADD CONSTRAINT uq_organization_members_organization_id_user_id UNIQUE (organization_id, user_id);

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT uq_organizations_slug UNIQUE (slug);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_github_id UNIQUE (github_id);

CREATE INDEX ix_agent_tokens_organization_id_created_at ON public.agent_tokens USING btree (organization_id, created_at);

CREATE INDEX ix_audit_logs_organization_id_created_at ON public.audit_logs USING btree (organization_id, created_at);

CREATE INDEX ix_deploy_requests_build_id ON public.deploy_requests USING btree (build_id);

CREATE INDEX ix_deploy_requests_rollback_of_request_id ON public.deploy_requests USING btree (rollback_of_request_id);

CREATE INDEX ix_deploy_requests_service_environment_id_created_at ON public.deploy_requests USING btree (service_environment_id, created_at);

CREATE INDEX ix_deploy_requests_service_id_created_at ON public.deploy_requests USING btree (service_id, created_at);

CREATE INDEX ix_deployments_service_environment_id_created_at ON public.deployments USING btree (service_environment_id, created_at);

CREATE INDEX ix_findings_organization_id_control_id_collected_at ON public.findings USING btree (organization_id, control_id, collected_at);

CREATE INDEX ix_invitations_github_id ON public.invitations USING btree (github_id);

CREATE INDEX ix_jobs_status_run_after ON public.jobs USING btree (status, run_after);

CREATE INDEX ix_migration_plans_service_id_created_at ON public.migration_plans USING btree (service_id, created_at);

CREATE INDEX ix_organization_members_user_id ON public.organization_members USING btree (user_id);

CREATE INDEX ix_services_organization_id ON public.services USING btree (organization_id);

CREATE UNIQUE INDEX uq_builds_service_id_repo_url_commit_sha_not_failed ON public.builds USING btree (service_id, repo_url, commit_sha) WHERE ((status)::text <> 'FAILED'::text);

CREATE UNIQUE INDEX uq_deploy_requests_service_environment_id_active ON public.deploy_requests USING btree (service_environment_id) WHERE ((status)::text = ANY ((ARRAY['BUILDING'::character varying, 'DEPLOYING'::character varying, 'VERIFYING'::character varying])::text[]));

CREATE UNIQUE INDEX uq_deploy_requests_service_environment_id_queued ON public.deploy_requests USING btree (service_environment_id) WHERE ((status)::text = 'QUEUED'::text);

CREATE UNIQUE INDEX uq_environments_org_name_live ON public.environments USING btree (organization_id, name) WHERE (is_deleted = false);

CREATE UNIQUE INDEX uq_invitations_organization_id_github_id_pending ON public.invitations USING btree (organization_id, github_id) WHERE ((status)::text = 'PENDING'::text);

CREATE UNIQUE INDEX uq_jobs_subject_id_open ON public.jobs USING btree (subject_id) WHERE ((status)::text = ANY ((ARRAY['QUEUED'::character varying, 'RUNNING'::character varying])::text[]));

CREATE UNIQUE INDEX uq_migration_plans_service_id_open ON public.migration_plans USING btree (service_id) WHERE ((status)::text = ANY ((ARRAY['PENDING_APPROVAL'::character varying, 'APPROVED'::character varying, 'RUNNING'::character varying, 'ROLLING_BACK'::character varying])::text[]));

CREATE UNIQUE INDEX uq_service_environments_public_host_live ON public.service_environments USING btree (public_host) WHERE (is_deleted = false);

CREATE UNIQUE INDEX uq_service_environments_service_id_environment_id_live ON public.service_environments USING btree (service_id, environment_id) WHERE (is_deleted = false);

CREATE UNIQUE INDEX uq_services_org_name_live ON public.services USING btree (organization_id, name) WHERE (is_deleted = false);

CREATE UNIQUE INDEX uq_services_public_host_live ON public.services USING btree (public_host) WHERE (is_deleted = false);

CREATE TRIGGER trg_audit_logs_prevent_delete BEFORE DELETE ON public.audit_logs FOR EACH ROW EXECUTE FUNCTION public.prevent_append_only_delete();

CREATE TRIGGER trg_deploy_request_logs_prevent_delete BEFORE DELETE ON public.deploy_request_logs FOR EACH ROW EXECUTE FUNCTION public.prevent_append_only_delete();

CREATE TRIGGER trg_deploy_requests_prevent_delete BEFORE DELETE ON public.deploy_requests FOR EACH ROW EXECUTE FUNCTION public.prevent_append_only_delete();

CREATE TRIGGER trg_deployments_prevent_delete BEFORE DELETE ON public.deployments FOR EACH ROW EXECUTE FUNCTION public.prevent_deployment_delete();

CREATE TRIGGER trg_migration_plan_logs_prevent_delete BEFORE DELETE ON public.migration_plan_logs FOR EACH ROW EXECUTE FUNCTION public.prevent_append_only_delete();

CREATE TRIGGER trg_migration_plans_prevent_delete BEFORE DELETE ON public.migration_plans FOR EACH ROW EXECUTE FUNCTION public.prevent_append_only_delete();

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT fk_agent_tokens_created_by_user_id_users FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT fk_agent_tokens_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT fk_audit_logs_actor_agent_token_id_agent_tokens FOREIGN KEY (actor_agent_token_id) REFERENCES public.agent_tokens(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT fk_audit_logs_actor_user_id_users FOREIGN KEY (actor_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT fk_audit_logs_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.builds
    ADD CONSTRAINT fk_builds_service_id_services FOREIGN KEY (service_id) REFERENCES public.services(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_request_logs
    ADD CONSTRAINT fk_deploy_request_logs_deploy_request_id_deploy_requests FOREIGN KEY (deploy_request_id) REFERENCES public.deploy_requests(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_build_id_builds FOREIGN KEY (build_id) REFERENCES public.builds(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_deployment_id_deployments FOREIGN KEY (deployment_id) REFERENCES public.deployments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_environment_id_environments FOREIGN KEY (environment_id) REFERENCES public.environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_rollback_base_deployment_id_deployments FOREIGN KEY (rollback_base_deployment_id) REFERENCES public.deployments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_rollback_of_request_id_deploy_requests FOREIGN KEY (rollback_of_request_id) REFERENCES public.deploy_requests(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_service_environment_id_service_environments FOREIGN KEY (service_environment_id) REFERENCES public.service_environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_service_id_services FOREIGN KEY (service_id) REFERENCES public.services(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_source_request_id_deploy_requests FOREIGN KEY (source_request_id) REFERENCES public.deploy_requests(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deploy_requests
    ADD CONSTRAINT fk_deploy_requests_superseded_by_id_deploy_requests FOREIGN KEY (superseded_by_id) REFERENCES public.deploy_requests(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT fk_deployments_rollback_of_id_deployments FOREIGN KEY (rollback_of_id) REFERENCES public.deployments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT fk_deployments_service_environment_id_service_environments FOREIGN KEY (service_environment_id) REFERENCES public.service_environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.environments
    ADD CONSTRAINT fk_environments_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT fk_findings_environment_id_environments FOREIGN KEY (environment_id) REFERENCES public.environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT fk_findings_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT fk_findings_reviewer_user_id_users FOREIGN KEY (reviewer_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT fk_findings_service_id_services FOREIGN KEY (service_id) REFERENCES public.services(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT fk_invitations_invited_by_user_id_users FOREIGN KEY (invited_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT fk_invitations_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plan_logs
    ADD CONSTRAINT fk_migration_plan_logs_migration_plan_id_migration_plans FOREIGN KEY (migration_plan_id) REFERENCES public.migration_plans(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT fk_migration_plans_aborted_by_user_id_users FOREIGN KEY (aborted_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT fk_migration_plans_approved_by_user_id_users FOREIGN KEY (approved_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT fk_migration_plans_cancelled_by_user_id_users FOREIGN KEY (cancelled_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT fk_migration_plans_created_by_user_id_users FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT fk_migration_plans_rejected_by_user_id_users FOREIGN KEY (rejected_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT fk_migration_plans_service_id_services FOREIGN KEY (service_id) REFERENCES public.services(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT fk_migration_plans_source_service_environment_id_servic_067d FOREIGN KEY (source_service_environment_id) REFERENCES public.service_environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.migration_plans
    ADD CONSTRAINT fk_migration_plans_target_service_environment_id_servic_a900 FOREIGN KEY (target_service_environment_id) REFERENCES public.service_environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.organization_members
    ADD CONSTRAINT fk_organization_members_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.organization_members
    ADD CONSTRAINT fk_organization_members_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT fk_organizations_created_by_user_id_users FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.service_environments
    ADD CONSTRAINT fk_service_environments_current_deployment_id_id_deployments FOREIGN KEY (current_deployment_id, id) REFERENCES public.deployments(id, service_environment_id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.service_environments
    ADD CONSTRAINT fk_service_environments_environment_id_environments FOREIGN KEY (environment_id) REFERENCES public.environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.service_environments
    ADD CONSTRAINT fk_service_environments_service_id_services FOREIGN KEY (service_id) REFERENCES public.services(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.services
    ADD CONSTRAINT fk_services_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT fk_sessions_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

