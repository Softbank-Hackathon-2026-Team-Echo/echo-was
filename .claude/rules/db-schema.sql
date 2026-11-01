-- 물리 DB 스키마(DDL)의 단일 출처. 직접 고치지 않는다.
-- 생성: scripts/dump_schema.sh (pg_dump --schema-only --no-owner --no-privileges)
-- 마이그레이션 버전 표(alembic_version)는 뺀다.


CREATE FUNCTION public.prevent_deployment_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'deployments rows are append-only and cannot be deleted'
                USING ERRCODE = 'restrict_violation';
        END;
        $$;

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
    CONSTRAINT ck_environments_config_is_object CHECK ((jsonb_typeof(config) = 'object'::text)),
    CONSTRAINT ck_environments_name_format CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'::text)),
    CONSTRAINT ck_environments_soft_delete_consistency CHECK ((is_deleted = (deleted_at IS NOT NULL)))
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
    CONSTRAINT ck_services_name_format CHECK (((name)::text ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'::text)),
    CONSTRAINT ck_services_soft_delete_consistency CHECK ((is_deleted = (deleted_at IS NOT NULL)))
);

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT pk_deployments PRIMARY KEY (id);

ALTER TABLE ONLY public.environments
    ADD CONSTRAINT pk_environments PRIMARY KEY (id);

ALTER TABLE ONLY public.service_environments
    ADD CONSTRAINT pk_service_environments PRIMARY KEY (id);

ALTER TABLE ONLY public.services
    ADD CONSTRAINT pk_services PRIMARY KEY (id);

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT uq_deployments_id_service_environment_id UNIQUE (id, service_environment_id);

CREATE INDEX ix_deployments_service_environment_id_created_at ON public.deployments USING btree (service_environment_id, created_at);

CREATE UNIQUE INDEX uq_environments_name_live ON public.environments USING btree (name) WHERE (is_deleted = false);

CREATE UNIQUE INDEX uq_service_environments_service_id_environment_id_live ON public.service_environments USING btree (service_id, environment_id) WHERE (is_deleted = false);

CREATE UNIQUE INDEX uq_services_name_live ON public.services USING btree (name) WHERE (is_deleted = false);

CREATE TRIGGER trg_deployments_prevent_delete BEFORE DELETE ON public.deployments FOR EACH ROW EXECUTE FUNCTION public.prevent_deployment_delete();

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT fk_deployments_rollback_of_id_deployments FOREIGN KEY (rollback_of_id) REFERENCES public.deployments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT fk_deployments_service_environment_id_service_environments FOREIGN KEY (service_environment_id) REFERENCES public.service_environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.service_environments
    ADD CONSTRAINT fk_service_environments_current_deployment_id_id_deployments FOREIGN KEY (current_deployment_id, id) REFERENCES public.deployments(id, service_environment_id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.service_environments
    ADD CONSTRAINT fk_service_environments_environment_id_environments FOREIGN KEY (environment_id) REFERENCES public.environments(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.service_environments
    ADD CONSTRAINT fk_service_environments_service_id_services FOREIGN KEY (service_id) REFERENCES public.services(id) ON DELETE RESTRICT;

