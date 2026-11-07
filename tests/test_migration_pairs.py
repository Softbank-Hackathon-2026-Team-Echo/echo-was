from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.services import migration_pair_registry as pairs

NOW = datetime(2026, 11, 7, 5, 0, tzinfo=UTC)
ORG, SERVICE, AWS, GCP, ONPREM = [UUID(int=n) for n in range(1, 6)]
DEP_A, DEP_B = UUID(int=11), UUID(int=12)


def registry_data() -> dict:
    evidence = {
        "status": "VERIFIED",
        "observed_at": NOW.isoformat(),
        "evidence_id": "local-fixture",
    }
    return {
        "revision": "fixture-1",
        "databases": [
            {
                "id": "aws-a-app",
                "organization_id": str(ORG),
                "service_id": str(SERVICE),
                "environment_id": str(AWS),
                "deployment_id": str(DEP_A),
                "environment_kind": "aws",
                "adapter_slot": "A",
                "database_name": "app",
            },
            {
                "id": "gcp-b-app",
                "organization_id": str(ORG),
                "service_id": str(SERVICE),
                "environment_id": str(GCP),
                "deployment_id": str(DEP_B),
                "environment_kind": "gcp",
                "adapter_slot": "B",
                "database_name": "app",
            },
        ],
        "pairs": [
            {
                "source_db_id": "aws-a-app",
                "target_db_id": "gcp-b-app",
                "adapter": "legacy-ab-v1",
                "route": evidence,
                "writer_path": evidence,
                "data_sync": evidence,
                "job_contract": evidence,
                "writer_db_id": "aws-a-app",
                "writer": evidence,
            }
        ],
    }


def context(**changes) -> dict:
    return dict(
        organization_id=ORG,
        service_id=SERVICE,
        source_environment_id=AWS,
        target_environment_id=GCP,
        source_deployment_id=DEP_A,
        target_deployment_id=DEP_B,
        **changes,
    )


def test_unregistered_pair_rejects_before_submit() -> None:
    registry = pairs.MigrationPairRegistry.from_config({})
    calls = []
    with pytest.raises(pairs.MigrationPairBlockedError) as exc:
        registry.dispatch(context(), "dbCutover", None, NOW, calls.append)
    assert exc.value.code == "MIGRATION_PAIR_UNSUPPORTED"
    assert calls == []


def test_registered_pair_snapshot_is_pinned_and_dispatches_same_pair() -> None:
    registry = pairs.MigrationPairRegistry.from_config(registry_data())
    snapshot = registry.snapshot(context(), "dbCutover", NOW)
    calls = []
    registry.dispatch(context(), "dbCutover", snapshot, NOW, calls.append)
    assert len(calls) == 1
    assert calls[0].source_db_id == "aws-a-app"
    assert calls[0].target_db_id == "gcp-b-app"
    assert calls[0].contract_version == "db-pair/v1"
    assert calls[0].job_kind == "cutover"


@pytest.mark.parametrize(
    "case",
    [
        "same",
        "crossorg",
        "wrong-service",
        "undeployed",
        "redeployed",
        "stale",
        "future",
        "unknown",
        "writer",
        "adapter",
        "b2",
    ],
)
def test_invalid_pair_refused_before_every_side_effect(case: str) -> None:
    config, ctx = registry_data(), context()
    if case == "same":
        ctx["target_environment_id"] = AWS
    elif case == "crossorg":
        ctx["organization_id"] = UUID(int=99)
    elif case == "wrong-service":
        ctx["service_id"] = UUID(int=99)
    elif case == "undeployed":
        ctx["target_deployment_id"] = None
    elif case == "redeployed":
        ctx["target_deployment_id"] = UUID(int=99)
    elif case == "stale":
        config["pairs"][0]["writer"]["observed_at"] = (NOW - timedelta(seconds=301)).isoformat()
    elif case == "future":
        config["pairs"][0]["writer"]["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif case == "unknown":
        config["pairs"][0]["writer"]["status"] = "UNKNOWN"
    elif case == "writer":
        config["pairs"][0]["writer_db_id"] = "gcp-b-app"
    elif case == "adapter":
        config["pairs"][0]["adapter"] = "unimplemented"
    elif case == "b2":
        config["databases"][1].update(adapter_slot="GCP_B2", database_name="demo")
    registry = pairs.MigrationPairRegistry.from_config(config)
    calls = []
    with pytest.raises(pairs.MigrationPairBlockedError):
        registry.dispatch(ctx, "dbCutover", None, NOW, calls.append)
    assert calls == []
    with pytest.raises(pairs.MigrationPairBlockedError):
        registry.snapshot(ctx, "dbCutover", NOW)


def test_changed_registry_or_reversed_snapshot_fails_closed() -> None:
    config = registry_data()
    snapshot = pairs.MigrationPairRegistry.from_config(config).snapshot(context(), "dbCutover", NOW)
    for changed in [
        snapshot.model_copy(update={"source_db_id": "gcp-b-app", "target_db_id": "aws-a-app"}),
        snapshot.model_copy(update={"contract_version": "wrong"}),
        snapshot.model_copy(update={"registry_revision": "changed"}),
    ]:
        calls = []
        with pytest.raises(pairs.MigrationPairBlockedError):
            pairs.MigrationPairRegistry.from_config(config).dispatch(
                context(), "dbCutover", changed, NOW, calls.append
            )
        assert calls == []


@pytest.mark.parametrize(
    "source,target,expected",
    [
        (AWS, GCP, (True, True, True, False)),
        (GCP, AWS, (True, True, False, True)),
        (AWS, ONPREM, (False, False, False, False)),
        (ONPREM, AWS, (False, False, False, False)),
        (GCP, ONPREM, (False, False, False, False)),
        (ONPREM, GCP, (False, False, False, False)),
    ],
)
def test_six_directions_are_capabilities_not_provider_guesses(source, target, expected) -> None:
    config = registry_data()
    if source == GCP:
        config["pairs"][0].update(
            source_db_id="gcp-b-app", target_db_id="aws-a-app", writer_db_id="gcp-b-app"
        )
    ctx = context()
    ctx.update(
        source_environment_id=source,
        target_environment_id=target,
        source_deployment_id=DEP_A if source == AWS else DEP_B,
        target_deployment_id=DEP_A if target == AWS else DEP_B,
    )
    result = pairs.MigrationPairRegistry.from_config(config).capabilities(ctx, NOW)
    assert (
        tuple(
            getattr(result, key).supported
            for key in ["app_traffic", "db_cutover", "disaster_promote", "failback"]
        )
        == expected
    )


def test_missing_and_stale_evidence_are_distinct() -> None:
    config = registry_data()
    config["pairs"][0]["writer"] = {"status": "UNKNOWN"}
    unknown = pairs.MigrationPairRegistry.from_config(config).capabilities(context(), NOW)
    assert unknown.db_cutover.status == "UNKNOWN"
    assert unknown.db_cutover.evidence_freshness == "UNKNOWN"
    config["pairs"][0]["writer"] = {
        "status": "VERIFIED",
        "observed_at": (NOW - timedelta(seconds=301)).isoformat(),
        "evidence_id": "stale-fixture",
    }
    stale = pairs.MigrationPairRegistry.from_config(config).capabilities(context(), NOW)
    assert stale.db_cutover.status == "UNKNOWN"
    assert stale.db_cutover.evidence_freshness == "STALE"


@pytest.mark.parametrize("injection", ["host", "command", "credentials"])
def test_registry_rejects_unrecognized_connection_or_command_fields(injection) -> None:
    from pydantic import ValidationError

    config = registry_data()
    config["databases"][0][injection] = "not-accepted"
    with pytest.raises(ValidationError):
        pairs.MigrationPairRegistry.from_config(config)


@pytest.mark.parametrize("kind", ["unknown-provider", "onprem", "aws"])
def test_unknown_provider_does_not_inherit_legacy_adapter_support(kind) -> None:
    config = registry_data()
    config["databases"][1]["environment_kind"] = kind
    registry = pairs.MigrationPairRegistry.from_config(config)
    assert registry.capabilities(context(), NOW).db_cutover.supported is False


def test_registry_digest_is_cross_repository_canonical_json():
    import hashlib
    import json

    from app.schemas.migration_capability import MigrationPairConfiguration

    config = registry_data()
    canonical = json.dumps(
        MigrationPairConfiguration.model_validate(config).model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    snapshot = pairs.MigrationPairRegistry.from_config(config).snapshot(context(), "dbCutover", NOW)
    assert snapshot.registry_sha256 == hashlib.sha256(canonical.encode()).hexdigest()


def test_duplicate_db_identity_or_environment_binding_is_invalid() -> None:
    from pydantic import ValidationError

    config = registry_data()
    config["databases"].append(dict(config["databases"][0]))
    with pytest.raises(ValidationError):
        pairs.MigrationPairRegistry.from_config(config)
