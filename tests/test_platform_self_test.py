"""플랫폼 자기 시험(보안 5-3) 순수 계산: 경로 채우기·쓰기 경로 목록·C-IAM-2·C-AUD-1 판정·간격."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.audit.actions import RUNNER_ROUTE_ACTIONS, WRITE_ROUTE_ACTIONS
from app.audit.route_coverage import LOGOUT_ROUTE, agent_probe_routes, unnamed_write_routes
from app.core.config import Settings
from app.enums import (
    SecurityControlId,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.main import app
from app.services.platform_self_test import (
    EVIDENCE_TTL,
    FAIL_RENEW_MARGIN,
    AuditCheck,
    RouteProbe,
    fill_route,
    judge_agent_writes,
    judge_audit_trail,
)
from app.services.security_rules import POLICY_VERSION, SecurityScope, get_rule

DATABASE_URL = "postgresql+asyncpg://x:y@127.0.0.1:1/z"
NOW = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)
ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
SCOPE = SecurityScope(ORG, was_version="2026.11.07")
APPROVE = "/api/v1/orgs/{org_id}/migration-plans/{plan_id}/approve"


def _denied(route: str = APPROVE) -> RouteProbe:
    return RouteProbe("POST", route, 403, "AGENT_READ_ONLY")


# --- 경로 ---


def test_fill_route_uses_organization_fixed_key_and_random_ids() -> None:
    route = (
        "/api/v1/orgs/{org_id}/services/{service_id}/environments/{environment_id}/variables/{key}"
    )

    filled = fill_route(route, ORG)

    parts = filled.split("/")
    assert parts[4] == str(ORG)
    assert parts[-1] == "SELF_TEST_KEY"
    uuid.UUID(parts[6])
    uuid.UUID(parts[8])
    assert "{" not in filled


def test_agent_probe_routes_cover_every_human_write_route_including_approvals() -> None:
    routes = set(agent_probe_routes(app.routes))

    assert routes == set(WRITE_ROUTE_ACTIONS)
    assert ("POST", APPROVE) in routes
    assert ("POST", "/api/v1/orgs/{org_id}/security/exceptions/{exception_id}/approve") in routes
    assert (
        "POST",
        "/api/v1/orgs/{org_id}/prechecks/{precheck_id}/patches/{patch_id}/approve",
    ) in routes
    assert not routes & set(RUNNER_ROUTE_ACTIONS)
    assert LOGOUT_ROUTE not in routes


def test_every_app_write_route_is_named() -> None:
    assert unnamed_write_routes(app.routes) == ()


# --- C-IAM-2 ---


def test_all_denied_writes_pass_iam() -> None:
    probes = [_denied(), _denied("/api/v1/orgs/{org_id}/services")]

    normalized = judge_agent_writes(probes, SCOPE, NOW)

    finding = normalized.finding
    assert finding.control_id == SecurityControlId.IAM_2
    assert finding.source == SecurityFindingSource.WAS_TEST
    assert finding.result == SecurityResult.PASS
    assert normalized.reason == SecurityFindingReason.AGENT_WRITES_DENIED
    assert finding.policy_version == POLICY_VERSION
    assert finding.scope == SCOPE
    assert finding.expires_at == NOW + EVIDENCE_TTL
    assert normalized.total_items == 2


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(202, None), (404, "MIGRATION_PLAN_NOT_FOUND"), (403, "ROLE_FORBIDDEN"), (500, None)],
    ids=["accepted", "not-found", "other-403", "server-error"],
)
def test_any_write_that_is_not_agent_read_only_fails_iam(
    status_code: int, code: str | None
) -> None:
    leaky = RouteProbe("POST", APPROVE, status_code, code)
    probes = [_denied("/api/v1/orgs/{org_id}/services"), leaky]

    normalized = judge_agent_writes(probes, SCOPE, NOW)

    assert normalized.finding.result == SecurityResult.FAIL
    assert normalized.reason == SecurityFindingReason.AGENT_WRITE_NOT_DENIED
    first = normalized.items[0]
    assert (first.item_id, first.reason) == (
        f"POST {APPROVE}",
        SecurityFindingReason.AGENT_WRITE_NOT_DENIED,
    )


def test_no_probe_is_unknown_not_pass() -> None:
    assert judge_agent_writes([], SCOPE, NOW).finding.result == SecurityResult.UNKNOWN


# --- C-AUD-1 ---


def test_complete_audit_trail_passes_and_counts_each_kind() -> None:
    checks = [
        AuditCheck("approval:MIGRATION_PLAN_APPROVED", ("a", "b"), ()),
        AuditCheck("agent:AGENT_RUN_CLAIMED", (), ()),
    ]

    normalized = judge_audit_trail((), checks, SCOPE, NOW)

    assert normalized.finding.control_id == SecurityControlId.AUD_1
    assert normalized.finding.source == SecurityFindingSource.AUDIT_LOG
    assert normalized.finding.result == SecurityResult.PASS
    assert [(item.item_id, item.target) for item in normalized.items] == [
        ("approval:MIGRATION_PLAN_APPROVED", "2/2"),
        ("agent:AGENT_RUN_CLAIMED", "0/0"),
    ]


def test_missing_audit_row_fails_with_target_id() -> None:
    checks = [AuditCheck("approval:MIGRATION_PLAN_APPROVED", ("a", "b"), ("b",))]

    normalized = judge_audit_trail((), checks, SCOPE, NOW)

    assert normalized.finding.result == SecurityResult.FAIL
    assert normalized.reason == SecurityFindingReason.AUDIT_RECORD_MISSING
    assert (normalized.items[0].item_id, normalized.items[0].target) == (
        "approval:MIGRATION_PLAN_APPROVED",
        "b",
    )


def test_unnamed_write_route_fails_audit_trail() -> None:
    normalized = judge_audit_trail((("POST", "/api/v1/new"),), [], SCOPE, NOW)

    assert normalized.finding.result == SecurityResult.FAIL
    assert normalized.reason == SecurityFindingReason.AUDIT_ROUTE_UNNAMED


def test_fail_evidence_uses_short_ttl_but_pass_keeps_full_ttl() -> None:
    short = timedelta(hours=6) + FAIL_RENEW_MARGIN
    missing = [AuditCheck("approval:MIGRATION_PLAN_APPROVED", ("a",), ("a",))]

    failed = judge_audit_trail((), missing, SCOPE, NOW, short).finding
    passed = judge_audit_trail((), [], SCOPE, NOW, short).finding
    leaked = judge_agent_writes([RouteProbe("POST", APPROVE, 202, "OK")], SCOPE, NOW, short)

    assert (failed.result, failed.expires_at) == (SecurityResult.FAIL, NOW + short)
    assert (passed.result, passed.expires_at) == (SecurityResult.PASS, NOW + EVIDENCE_TTL)
    assert leaked.finding.expires_at == NOW + short
    # 24시간보다 긴 값은 24시간으로 자른다.
    capped = judge_audit_trail((), missing, SCOPE, NOW, timedelta(days=2)).finding
    assert capped.expires_at == NOW + EVIDENCE_TTL


# --- 간격 ---


def test_default_interval_runs_at_least_three_times_per_evidence_lifetime() -> None:
    interval = Settings(database_url=DATABASE_URL).security_platform_self_test_interval_seconds

    assert timedelta(seconds=interval) * 3 <= EVIDENCE_TTL
    assert EVIDENCE_TTL <= get_rule(SecurityControlId.IAM_2).max_age
    assert EVIDENCE_TTL <= get_rule(SecurityControlId.AUD_1).max_age


@pytest.mark.parametrize("value", [299, 28801])
def test_interval_outside_range_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=DATABASE_URL, security_platform_self_test_interval_seconds=value)
