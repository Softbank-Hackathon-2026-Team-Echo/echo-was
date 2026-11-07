"""모델 없는 결정론적 npm 직접 의존성 수정. 승인된 로컬 fixture만 지원한다."""

import asyncio
import copy
import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any

from app.clients.cve_autofix_config import CveFixtureBinding
from app.services.cve_autofix_rules import reject, validate_advisory, validate_rescan

SOURCE_FILES = frozenset({"package.json", "package-lock.json", "app.js", "test.js"})
PATCH_FILES = frozenset({"package.json", "package-lock.json"})


async def run_sandboxed(
    command: list[str],
    cwd: Path,
    profile: str,
    environment: dict[str, str],
    limit_seconds: float,
) -> str:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sandbox-exec",
        "-p",
        profile,
        *command,
        cwd=cwd,
        env=environment,
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=limit_seconds)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        reject("TIMEOUT")
        raise AssertionError("unreachable") from exc
    if process.returncode != 0:
        (cwd / "command-failed.log").write_bytes(output[:262144])
        reject("BUILD_FAILED")
    if len(output) > 262144:
        reject("OUTPUT_LIMIT")
    return output.decode("utf-8", errors="replace")


def validate_source(source: Path, head: str, hashes: dict[str, str], digest: str) -> None:
    if (
        source.is_symlink()
        or not (source / ".git").is_dir()
        or (source / ".git").is_symlink()
        or set(hashes) != SOURCE_FILES
        or not re.fullmatch(r"[0-9a-f]{40}", head)
    ):
        reject("SOURCE_REJECTED")
    actual_head = subprocess.check_output(
        ["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"],
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(source)},
        text=True,
        timeout=5,
    ).strip()
    if actual_head != head:
        reject("HEAD_CHANGED")
    for name, expected in hashes.items():
        path = source / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            reject("SOURCE_REJECTED")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            reject("SOURCE_CHANGED")
        committed = subprocess.check_output(
            ["/usr/bin/git", "-C", str(source), "show", head + ":" + name],
            env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(source)},
            timeout=5,
        )
        if hashlib.sha256(committed).hexdigest() != expected:
            reject("COMMIT_FILE_MISMATCH")
    expected_digest = (
        "sha256:" + hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    )
    if expected_digest != digest:
        reject("DIGEST_MISMATCH")


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        reject("FILE_REJECTED")
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        reject("JSON_NOT_OBJECT")
    return result


def prepare_candidate(
    source: Path,
    target: Path,
    package: str,
    version: str,
    fixed_version: str,
    metadata: dict[str, Any],
) -> None:
    # 한 개·전이 의존성 없는 작은 fixture가 최초 인수 범위다. 범용 npm 프로젝트로 확대하지 않는다.
    if package != "minimist" or not all(
        re.fullmatch(r"\d+\.\d+\.\d+", v) for v in (version, fixed_version)
    ):
        reject("PACKAGE_UNSUPPORTED")
    manifest = read_json(source / "package.json")
    lock = read_json(source / "package-lock.json")
    if (
        manifest.get("dependencies") != {package: version}
        or any(
            manifest.get(k)
            for k in ("scripts", "devDependencies", "optionalDependencies", "workspaces")
        )
        or lock.get("lockfileVersion") != 3
        or set(lock.get("packages", {})) != {"", f"node_modules/{package}"}
        or lock["packages"][""]["dependencies"] != {package: version}
        or lock["packages"][f"node_modules/{package}"].get("version") != version
    ):
        reject("MANIFEST_LOCK_MISMATCH")
    if (
        metadata.get("version") != fixed_version
        or metadata.get("resolved")
        != f"https://registry.npmjs.org/{package}/-/{package}-{fixed_version}.tgz"
        or not str(metadata.get("integrity", "")).startswith("sha512-")
        or metadata.get("hasInstallScript")
        or metadata.get("dependencies")
    ):
        reject("PACKAGE_METADATA_INVALID")
    if any((source / name).is_symlink() or not (source / name).is_file() for name in SOURCE_FILES):
        reject("SOURCE_FILE_REJECTED")
    target.mkdir(parents=True, exist_ok=False)
    manifest = copy.deepcopy(manifest)
    lock = copy.deepcopy(lock)
    manifest["dependencies"][package] = fixed_version
    lock["packages"][""]["dependencies"][package] = fixed_version
    lock["packages"][f"node_modules/{package}"] = metadata
    for name, value in (("package.json", manifest), ("package-lock.json", lock)):
        (target / name).write_text(json.dumps(value, indent=2) + "\n")
    for name in SOURCE_FILES - PATCH_FILES:
        (target / name).write_bytes((source / name).read_bytes())


def sandbox_profile(work: Path, tools: list[Path]) -> str:
    profile = """(version 1)
(deny default)
(allow process-exec process-fork signal sysctl-read mach-lookup)
(allow file-read* (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share")
 (subpath "/Library/Apple") (subpath "/dev") (literal "/"))
(allow file-write* (literal "/dev/null"))
"""
    for root in [work, *tools]:
        profile += f"(allow file-read* (subpath {json.dumps(str(root))}))\n"
        for parent in root.parents:
            profile += f"(allow file-read-metadata (literal {json.dumps(str(parent))}))\n"
    return profile + f"(allow file-write* (subpath {json.dumps(str(work))}))\n"


class LocalCveSandboxClient:
    """고정 public fixture 전용. 원본 저장소·다른 tenant·운영 배포를 쓰는 메서드가 없다."""

    def __init__(self, binding: dict[str, Any]) -> None:
        self.binding = CveFixtureBinding.model_validate(binding)

    @property
    def input_hash(self) -> str:
        raw = json.dumps(self.binding.model_dump(mode="json"), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def _validate_inputs(self) -> dict[str, Any]:
        binding = self.binding
        validate_source(
            binding.source_dir, binding.source_head, binding.files_sha256, binding.image_digest
        )
        for path, expected in (
            (binding.osv_path, binding.osv_sha256),
            (binding.rescan_path, binding.rescan_sha256),
        ):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
                reject("EVIDENCE_REJECTED")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                reject("EVIDENCE_HASH_MISMATCH")
        response = read_json(binding.osv_path)
        validate_advisory(
            response, binding.package, binding.from_version, binding.cve_id, binding.fixed_version
        )
        return response

    async def execute(self, run_id: str) -> dict[str, Any]:
        binding = self.binding
        # UUID는 출력 디렉터리 이름에만 쓴다. HTTP 값으로 경로·명령·repo를 받지 않는다.
        if str(uuid.UUID(run_id)) != run_id:
            reject("RUN_ID_INVALID")
        await asyncio.to_thread(self._validate_inputs)
        work = binding.output_root / run_id / "candidate"
        await asyncio.to_thread(
            prepare_candidate,
            binding.source_dir,
            work,
            binding.package,
            binding.from_version,
            binding.fixed_version,
            binding.fixed_metadata,
        )
        profile = sandbox_profile(work, [binding.node.parent.parent, binding.npm_cli.parents[3]])
        (work.parent / "sandbox.sb").write_text(profile)
        # 캐시는 이번 공개 두 패키지만 가진 전용 디렉터리다. 링크·큰 캐시는 거절한다.
        await asyncio.to_thread(self._copy_cache, work)
        for name in ("home", "tmp"):
            (work / name).mkdir()
        for name in ("user.npmrc", "global.npmrc"):
            (work / name).write_text("")
        environment = {
            "PATH": str(binding.node.parent),
            "HOME": str(work / "home"),
            "TMPDIR": str(work / "tmp"),
            "NODE_OPTIONS": "",
        }
        commands = [
            (
                "INSTALL",
                [
                    str(binding.node),
                    str(binding.npm_cli),
                    "ci",
                    "--offline",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--cache=" + str(work / "cache"),
                    "--userconfig=" + str(work / "user.npmrc"),
                    "--globalconfig=" + str(work / "global.npmrc"),
                ],
            ),
            ("BUILD", [str(binding.node), "--check", "app.js"]),
            ("TEST", [str(binding.node), "test.js"]),
        ]
        checks = []
        for name, command in commands:
            output = await run_sandboxed(
                command, work, profile, environment, binding.timeout_seconds
            )
            (work.parent / (name.lower() + ".log")).write_text(output)
            checks.append(name)
        installed = read_json(work / "node_modules/minimist/package.json")
        if installed.get("version") != binding.fixed_version:
            reject("INSTALLED_VERSION_MISMATCH")
        # 원문 재검사는 수집 시각에 고정된 전체 응답이다. 최신 전 세계 DB/이미지 스캔 주장이 아니다.
        await asyncio.to_thread(self._validate_inputs)
        validate_rescan(read_json(binding.rescan_path))
        checks.append("RESCAN")
        diff = ""
        changed = set()
        for name in sorted(SOURCE_FILES):
            before = (binding.source_dir / name).read_text()
            after = (work / name).read_text()
            if before != after:
                changed.add(name)
                diff += "".join(
                    difflib.unified_diff(
                        before.splitlines(True),
                        after.splitlines(True),
                        fromfile="a/" + name,
                        tofile="b/" + name,
                    )
                )
        if changed != PATCH_FILES:
            reject("PATCH_SCOPE_OR_NO_CHANGE")
        receipt = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "READY_FOR_REVIEW",
            "executor": "DETERMINISTIC_DEPENDENCY_BUMP",
            "ai_reviewed": False,
            "event_simulated": True,
            "execution_simulated": False,
            "model_mocked": False,
            "source_finding_id": str(binding.source_finding_id),
            "cve_id": binding.cve_id,
            "source_head": binding.source_head,
            "source_artifact_kind": binding.image_digest_kind,
            "source_artifact_digest": binding.image_digest,
            "real_image_digest": None,
            "real_image_verified": False,
            "deployment_ready": False,
            "vulnerabilities_resolved": False,
            "input_hash": self.input_hash,
            "files": sorted(changed),
            "patch_hash": "sha256:" + hashlib.sha256(diff.encode()).hexdigest(),
            "checks": checks,
            "rescan_sha256": binding.rescan_sha256,
            "scan_scope": "captured_osv_package_version_response",
            "manifest_sha256": hashlib.sha256((work / "package.json").read_bytes()).hexdigest(),
            "lock_sha256": hashlib.sha256((work / "package-lock.json").read_bytes()).hexdigest(),
            "merge_performed": False,
            "deploy_performed": False,
        }
        (work.parent / "patch.diff").write_text(diff)
        (work.parent / "receipt.json").write_text(json.dumps(receipt, sort_keys=True, indent=2))
        return receipt

    def _copy_cache(self, work: Path) -> None:
        cache = self.binding.cache_dir
        if cache.is_symlink() or not cache.is_dir():
            reject("CACHE_REJECTED")
        size = 0
        for path in cache.rglob("*"):
            if path.is_symlink():
                reject("CACHE_REJECTED")
            if path.is_file():
                size += path.stat().st_size
        if size > 20_000_000:
            reject("CACHE_TOO_LARGE")
        shutil.copytree(cache, work / "cache")
