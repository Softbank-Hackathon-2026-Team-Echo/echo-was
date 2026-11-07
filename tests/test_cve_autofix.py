"""실제 공개 OSV 응답을 재생하며 네트워크는 사용하지 않는다."""

import importlib
import importlib.util
import json
from pathlib import Path

import pytest


def rules():
    assert importlib.util.find_spec("app.services.cve_autofix_rules") is not None
    return importlib.import_module("app.services.cve_autofix_rules")


def test_cve_autofix_uses_osv_fixed_version_not_a_requested_version() -> None:
    module = rules()
    raw = json.loads((Path(__file__).parent / "fixtures/cve-osv-minimist.json").read_text())
    match = module.validate_advisory(raw, "minimist", "1.2.5", "CVE-2021-44906", "1.2.6")
    assert match.fixed_version == "1.2.6"
    with pytest.raises(module.CveAutofixRejectedError, match="FIX_VERSION_MISMATCH"):
        module.validate_advisory(raw, "minimist", "1.2.5", "CVE-2021-44906", "9.9.9")


def context():
    from types import SimpleNamespace
    from uuid import UUID

    binding = {
        "organization_id": "00000000-0000-0000-0000-000000000001",
        "service_id": "00000000-0000-0000-0000-000000000002",
        "environment_id": "00000000-0000-0000-0000-000000000003",
        "source_finding_id": "00000000-0000-0000-0000-000000000004",
        "image_digest": "sha256:" + "a" * 64,
        "image_digest_kind": "FIXTURE_SOURCE_TREE",
        "repository": "fixture/minimist",
        "cve_id": "CVE-2021-44906",
        "ecosystem": "npm",
        "package": "minimist",
        "from_version": "1.2.5",
        "fixed_version": "1.2.6",
        "fixture": True,
    }
    finding = SimpleNamespace(
        id=UUID(binding["source_finding_id"]),
        organization_id=UUID(binding["organization_id"]),
        service_id=UUID(binding["service_id"]),
        environment_id=UUID(binding["environment_id"]),
        image_digest=binding["image_digest"],
        source="CVE_WATCH",
        mode="SIMULATED",
        remediation={
            "kind": "DEPENDENCY_BUMP",
            "ecosystem": "npm",
            "package": "minimist",
            "fromVersion": "1.2.5",
            "toVersion": "1.2.6",
            "advisoryId": "GHSA-xvch-5gv4-984h",
            "aliases": ["CVE-2021-44906"],
        },
    )
    packages = [{"ecosystem": "npm", "name": "minimist", "version": "1.2.5"}]
    return finding, binding, packages


def test_context_binds_source_finding_and_tenant_and_package():
    module = rules()
    finding, binding, packages = context()
    module.validate_context(
        finding, binding["organization_id"], binding, packages, "fixture/minimist"
    )
    for field, bad in [
        ("organization_id", "other"),
        ("service_id", "other"),
        ("environment_id", "other"),
        ("source_finding_id", "other"),
        ("image_digest", "sha256:" + "b" * 64),
        ("repository", "arbitrary/repo"),
        ("ecosystem", "PyPI"),
        ("package", "other"),
        ("fixed_version", "9.9.9"),
    ]:
        with pytest.raises(module.CveAutofixRejectedError):
            module.validate_context(
                finding,
                binding["organization_id"],
                {**binding, field: bad},
                packages,
                "fixture/minimist",
            )
    with pytest.raises(module.CveAutofixRejectedError):
        module.validate_context(finding, "other", binding, packages, "fixture/minimist")
    with pytest.raises(module.CveAutofixRejectedError):
        module.validate_context(
            finding, binding["organization_id"], binding, [], "fixture/minimist"
        )
    finding.mode = "REAL"
    with pytest.raises(module.CveAutofixRejectedError, match="FIXTURE_ONLY"):
        module.validate_context(
            finding, binding["organization_id"], binding, packages, "fixture/minimist"
        )


def test_dependency_bump_updates_manifest_and_lockfile(tmp_path):
    assert importlib.util.find_spec("app.clients.cve_autofix") is not None
    client = importlib.import_module("app.clients.cve_autofix")
    source = tmp_path / "source"
    source.mkdir()
    (source / "package.json").write_text(json.dumps({"dependencies": {"minimist": "1.2.5"}}))
    old = {
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"minimist": "1.2.5"}},
            "node_modules/minimist": {"version": "1.2.5", "integrity": "old"},
        },
    }
    (source / "package-lock.json").write_text(json.dumps(old))
    for name in ["app.js", "test.js"]:
        (source / name).write_text("// pinned benign fixture\n")
    metadata = {
        "version": "1.2.6",
        "resolved": "https://registry.npmjs.org/minimist/-/minimist-1.2.6.tgz",
        "integrity": "sha512-test",
    }
    target = tmp_path / "candidate"
    client.prepare_candidate(source, target, "minimist", "1.2.5", "1.2.6", metadata)
    assert json.loads((target / "package.json").read_text())["dependencies"]["minimist"] == "1.2.6"
    assert (
        json.loads((target / "package-lock.json").read_text())["packages"]["node_modules/minimist"]
        == metadata
    )
    assert (source / "package-lock.json").read_text() == json.dumps(old)
    assert (target / "app.js").read_bytes() == (source / "app.js").read_bytes()
    with pytest.raises(rules().CveAutofixRejectedError):
        client.prepare_candidate(
            source, tmp_path / "bad", "minimist;touch pwn", "1.2.5", "1.2.6", metadata
        )


def test_rescan_never_accepts_retained_or_new_vulnerabilities():
    module = rules()
    module.validate_rescan({})
    for body in [
        {"vulns": [{"id": "CVE-2021-44906"}]},
        {"vulns": [{"id": "GHSA-new-problem"}]},
        {"next_page_token": "pending"},
        {"vulns": "invalid"},
        {"error": "timeout"},
    ]:
        with pytest.raises(module.CveAutofixRejectedError):
            module.validate_rescan(body)


async def test_sandbox_command_failure_timeout_and_cancel_are_fail_closed(tmp_path):
    import asyncio
    import sys

    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt required; not OCI execution evidence")
    assert importlib.util.find_spec("app.clients.cve_autofix") is not None
    client = importlib.import_module("app.clients.cve_autofix")
    assert hasattr(client, "run_sandboxed"), "real sandbox command runner missing"
    # 真正なローカルプロセス。外部へ接続しない。
    profile = "(version 1)(allow default)"
    with pytest.raises(rules().CveAutofixRejectedError, match="BUILD_FAILED"):
        await client.run_sandboxed(["/usr/bin/false"], tmp_path, profile, {}, 3)
    with pytest.raises(rules().CveAutofixRejectedError, match="TIMEOUT"):
        await client.run_sandboxed(["/bin/sleep", "10"], tmp_path, profile, {}, 0.01)
    task = asyncio.create_task(
        client.run_sandboxed(["/bin/sleep", "10"], tmp_path, profile, {}, 30)
    )
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_source_pin_rejects_head_files_and_artifact_changes(tmp_path):
    import hashlib
    import subprocess

    client = importlib.import_module("app.clients.cve_autofix")
    assert hasattr(client, "validate_source"), "pinned checkout validation missing"
    source = tmp_path / "source"
    source.mkdir()
    for name in client.SOURCE_FILES:
        (source / name).write_text("pinned\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    hashes = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in client.SOURCE_FILES
    }
    digest = "sha256:" + hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    client.validate_source(source, head, hashes, digest)
    with pytest.raises(rules().CveAutofixRejectedError, match="HEAD_CHANGED"):
        client.validate_source(source, "f" * 40, hashes, digest)
    with pytest.raises(rules().CveAutofixRejectedError, match="DIGEST_MISMATCH"):
        client.validate_source(source, head, hashes, "sha256:" + "f" * 64)
    (source / "app.js").write_text("changed")
    with pytest.raises(rules().CveAutofixRejectedError, match="SOURCE_CHANGED"):
        client.validate_source(source, head, hashes, digest)
    dirty_hashes = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in client.SOURCE_FILES
    }
    dirty_digest = (
        "sha256:" + hashlib.sha256(json.dumps(dirty_hashes, sort_keys=True).encode()).hexdigest()
    )
    with pytest.raises(rules().CveAutofixRejectedError, match="COMMIT_FILE_MISMATCH"):
        client.validate_source(source, head, dirty_hashes, dirty_digest)
