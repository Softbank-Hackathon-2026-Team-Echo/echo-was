"""환경 관측의 범위·만료·불완전 결과가 정상으로 승격되지 않는지 검사한다."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.clients.environment_iam import CLOUD, KUBE, check, judge_aws_policy, judge_rules
from app.clients.environment_transport import TLS, safe_address, select_observation
from app.core.config import Settings
from app.enums import SecurityCheckPhase
from app.enums import SecurityControlId as C
from app.enums import SecurityResult as R
from app.security_environment.types import (
    EVIDENCE_TTL,
    EnvironmentSecurityProfile,
    IamSubject,
    TransportObservation,
    TransportTarget,
)
from app.services.environment_security_collector import normalize_environment_checks
from app.services.security_rules import SecurityScope, SecurityTarget, evaluate_security

NOW = datetime(2026, 11, 8, tzinfo=UTC)


def profile() -> EnvironmentSecurityProfile:
    return EnvironmentSecurityProfile(
        organization_id=uuid4(),
        environment_id=uuid4(),
        subjects=(
            IamSubject(
                id="worker",
                kind="kubernetes",
                name="worker",
                namespace="echo-was",
                allowed_actions=("core:pods:get",),
                allowed_namespaces=("echo-was",),
            ),
        ),
        transports=(TransportTarget(id="origin", kind="http", url="https://10.0.0.1"),),
    )


def complete(p: EnvironmentSecurityProfile, result: R = R.PASS):
    return {
        C.IAM_1: [check("worker", "OBSERVED", result, NOW, KUBE)],
        C.TLS_2: [check("origin", "OBSERVED", result, NOW, TLS)],
    }


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1"]
)
def test_unsafe_internal_address_rejected(address: str) -> None:
    assert not safe_address(address)


@pytest.mark.parametrize("address", ["10.0.0.1", "172.16.0.1", "192.168.1.3"])
def test_private_targets_are_distinct_from_public_tls_checker(address: str) -> None:
    assert safe_address(address)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "https://metadata.google.internal",
        "https://user:pass@10.0.0.1",
        "https://10.0.0.1/?secret=x",
    ],
)
def test_profile_rejects_unsafe_url(url: str) -> None:
    with pytest.raises(ValidationError):
        TransportTarget(id="origin", kind="http", url=url)


def test_read_token_requires_https_fixed_ip() -> None:
    for url in ("http://10.0.0.1", "https://entry.example.com"):
        with pytest.raises(ValidationError):
            TransportTarget(
                id="entry",
                kind="entrypoint",
                url=url,
                route="aws",
                token_ref="env:ECHO_SECRET_READ_TOKEN",
            )


def test_subject_path_injection_rejected() -> None:
    with pytest.raises(ValidationError):
        IamSubject(id="worker", kind="kubernetes", name="a/../../secrets", namespace="echo-was")


def test_duplicate_profile_rejected() -> None:
    p = profile()
    with pytest.raises(ValidationError):
        Settings(security_environment_profiles=(p, p))


def test_revision_binds_scope_config_and_reference() -> None:
    p = profile()
    revision = p.revision({"cluster_name": "one"}, "env:A")
    assert revision != p.revision({"cluster_name": "two"}, "env:A")
    assert revision != p.revision({"cluster_name": "one"}, "env:B")
    assert revision != p.model_copy(update={"environment_id": uuid4()}).revision(
        {"cluster_name": "one"}, "env:A"
    )


def test_missing_target_does_not_get_hidden_by_other_pass() -> None:
    p = profile()
    checks = complete(p)
    checks[C.TLS_2] = [check("different", "OBSERVED", R.PASS, NOW, TLS)]
    found = normalize_environment_checks(checks, p, "rev", NOW)
    assert next(n for n in found if n.finding.control_id == C.TLS_2).finding.result == R.UNKNOWN


def test_stale_snapshot_not_given_fresh_pass() -> None:
    p = profile()
    found = normalize_environment_checks(complete(p), p, "rev", NOW + EVIDENCE_TTL)
    assert all(n.finding.result == R.UNKNOWN for n in found)
    assert all(n.items[0].item_id == "OBSERVATION_EXPIRED" for n in found)


def test_original_observation_time_preserved() -> None:
    p = profile()
    found = normalize_environment_checks(complete(p), p, "rev", NOW + timedelta(minutes=10))
    assert all(n.finding.collected_at == NOW for n in found)
    assert all(n.finding.expires_at == NOW + EVIDENCE_TTL for n in found)


def test_item_budget_keeps_worst_result_and_original_total() -> None:
    p = profile()
    checks = complete(p)
    checks[C.IAM_1] = [check("worker", "OBSERVED", R.PASS, NOW, KUBE) for _ in range(80)] + [
        check("worker", "PERMISSION_OUTSIDE_ALLOWLIST", R.FAIL, NOW, KUBE)
    ]
    found = normalize_environment_checks(checks, p, "rev", NOW)
    iam = next(n for n in found if n.finding.control_id == C.IAM_1)
    assert iam.finding.result == R.FAIL
    assert iam.total_items == 81 and len(iam.items) == 50
    assert iam.items[0].severity == "FAIL"


@pytest.mark.parametrize("phase", [SecurityCheckPhase.PREPARATION, SecurityCheckPhase.EXECUTION])
def test_revision_and_environment_required_in_all_phases(phase) -> None:
    p = profile()
    findings = [n.finding for n in normalize_environment_checks(complete(p), p, "rev", NOW)]
    scope = SecurityScope(p.organization_id, environment_id=p.environment_id)
    for revision, expected in (("rev", R.PASS), ("changed", R.UNKNOWN), (None, R.UNKNOWN)):
        target = SecurityTarget(scope=scope, environment_kind="aws", environment_revision=revision)
        decision = evaluate_security(target, phase, findings, (), NOW)
        controls = [c for c in decision.controls if c.control_id in (C.IAM_1, C.TLS_2)]
        assert len(controls) == 2
        assert all(c.result == expected and not c.is_blocking for c in controls)


def test_old_observation_or_wrong_revision_is_unknown() -> None:
    p = profile()
    target = p.transports[0]
    for revision, at in (("wrong", NOW), ("v1", NOW - EVIDENCE_TTL)):
        observation = TransportObservation(
            revision=revision,
            observed_at=at,
            checks=(check("origin", "TLS_VERIFIED", R.PASS, at, TLS),),
        )
        assert select_observation(observation, target, NOW, TLS)[0].result == R.UNKNOWN


def test_namespace_and_wildcard_permissions_reported() -> None:
    p = profile()
    checks = judge_rules(
        p.subjects[0], [{"apiGroups": [""], "resources": ["*"], "verbs": ["*"]}], None, NOW
    )
    reasons = {v.reason for v in checks}
    assert {
        "WILDCARD_PERMISSION",
        "PERMISSION_OUTSIDE_NAMESPACE",
        "PERMISSION_OUTSIDE_ALLOWLIST",
    } <= reasons


def test_allowed_rbac_rule_passes() -> None:
    p = profile()
    checks = judge_rules(
        p.subjects[0],
        [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}],
        "echo-was",
        NOW,
    )
    assert all(c.result == R.PASS for c in checks)


def test_aws_admin_policy_is_not_normalized_to_pass() -> None:
    subject = IamSubject(id="executor", kind="aws", name="arn:aws:iam::123456789012:role/worker")
    checks = judge_aws_policy(
        subject, {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}, NOW
    )
    assert any(c.reason == "WILDCARD_PERMISSION" for c in checks)
    assert all(c.source == CLOUD for c in checks)


async def test_entrypoint_uses_service_route_and_receipt_time(monkeypatch):
    import httpx
    from pydantic import SecretStr

    from app.clients import environment_transport as module

    route = "svc_" + "a" * 32 + "_" + "b" * 32
    target = TransportTarget(
        id="origin",
        kind="entrypoint",
        url="https://10.0.0.1",
        token_ref="env:ECHO_SECRET_READ_TOKEN",
        route=route,
    )

    class Clock:
        value = NOW

        def now(self):
            return self.value

    clock = Clock()

    class Secrets:
        async def get_secret(self, reference):
            return SecretStr("local-test")

    def respond(request):
        assert request.url.params["route"] == route
        assert request.method == "GET"
        clock.value = NOW + timedelta(seconds=1)
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "revision": "v1",
                "observed_at": clock.now().isoformat(),
                "checks": [
                    {
                        "id": route,
                        "result": "WARN",
                        "reason": "PLAINTEXT_TRANSPORT",
                        "source": "TLS_PROBE",
                        "observed_at": clock.now().isoformat(),
                    }
                ],
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    values = await module.inspect_entrypoint(target, Secrets(), clock)
    assert values[0].result == R.WARN
    assert values[0].id == "origin"
    assert values[0].observed_at == clock.now()


def test_future_snapshot_and_cross_route_never_pass():
    target = TransportTarget(
        id="origin",
        kind="entrypoint",
        url="https://10.0.0.1",
        token_ref="env:ECHO_SECRET_READ_TOKEN",
        route="aws",
    )
    for at, route in ((NOW + timedelta(seconds=1), "aws"), (NOW, "gcp")):
        observation = TransportObservation(
            revision="v1", observed_at=at, checks=(check(route, "TLS_VERIFIED", R.PASS, at, TLS),)
        )
        assert select_observation(observation, target, NOW, TLS)[0].result == R.UNKNOWN
