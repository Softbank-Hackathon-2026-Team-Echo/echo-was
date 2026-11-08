"""실제 권한을 읽어 지정된 실행 신원의 범위를 검사한다. 권한을 바꾸지 않는다."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.enums import SecurityFindingSource, SecurityResult
from app.executors.kubernetes.api import KubeApi
from app.security_environment.types import EvidenceCheck, IamSubject

KUBE = SecurityFindingSource.KUBERNETES_API
CLOUD = SecurityFindingSource.CLOUD_API


def check(
    subject: str,
    code: str,
    result: SecurityResult,
    now: datetime,
    source: SecurityFindingSource = KUBE,
) -> EvidenceCheck:
    return EvidenceCheck(id=subject, result=result, reason=code, observed_at=now, source=source)


async def read_all(api: KubeApi, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    continuation = ""
    seen: set[str] = set()
    for _ in range(40):
        body = await api.get(path, {"limit": "500", "continue": continuation})
        if body is None or not isinstance(body.get("items"), list):
            raise ValueError("invalid Kubernetes list")
        page = body["items"]
        if any(not isinstance(item, dict) for item in page):
            raise ValueError("invalid Kubernetes item")
        items.extend(page)
        continuation = body.get("metadata", {}).get("continue", "")
        if not continuation:
            return items
        if not isinstance(continuation, str) or continuation in seen:
            raise ValueError("invalid Kubernetes continuation")
        seen.add(continuation)
    raise ValueError("Kubernetes list exceeds budget")


def judge_rules(
    subject: IamSubject, rules: object, namespace: str | None, now: datetime
) -> list[EvidenceCheck]:
    if not isinstance(rules, list):
        return [check(subject.id, "RULES_UNREADABLE", SecurityResult.UNKNOWN, now)]
    values: list[EvidenceCheck] = []
    for rule in rules:
        if not isinstance(rule, dict):
            values.append(check(subject.id, "RULE_INVALID", SecurityResult.UNKNOWN, now))
            continue
        verbs, resources, groups = rule.get("verbs"), rule.get("resources"), rule.get("apiGroups")
        if rule.get("nonResourceURLs"):
            values.append(check(subject.id, "NON_RESOURCE_PERMISSION", SecurityResult.UNKNOWN, now))
            continue
        if not all(
            isinstance(v, list) and v and all(isinstance(x, str) for x in v)
            for v in (verbs, resources, groups)
        ):
            values.append(check(subject.id, "RULE_INVALID", SecurityResult.UNKNOWN, now))
            continue
        assert isinstance(verbs, list) and isinstance(resources, list) and isinstance(groups, list)
        if "*" in verbs or "*" in resources or "*" in groups:
            values.append(check(subject.id, "WILDCARD_PERMISSION", SecurityResult.WARN, now))
        if namespace is None or namespace not in subject.allowed_namespaces:
            values.append(
                check(subject.id, "PERMISSION_OUTSIDE_NAMESPACE", SecurityResult.WARN, now)
            )
        allowed = set(subject.allowed_actions)
        actual = {
            f"{group or 'core'}:{resource}:{verb}"
            for group in groups
            for resource in resources
            for verb in verbs
        }
        if not allowed:
            values.append(check(subject.id, "ALLOWLIST_MISSING", SecurityResult.UNKNOWN, now))
        elif not actual.issubset(allowed):
            values.append(
                check(subject.id, "PERMISSION_OUTSIDE_ALLOWLIST", SecurityResult.WARN, now)
            )
    return values or [check(subject.id, "PERMISSION_WITHIN_SCOPE", SecurityResult.PASS, now)]


def _subject_matches(value: object, subject: IamSubject) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("kind") == "ServiceAccount":
        return value.get("name") == subject.name and value.get("namespace") == subject.namespace
    if value.get("kind") == "User":
        return value.get("name") == f"system:serviceaccount:{subject.namespace}:{subject.name}"
    if value.get("kind") == "Group":
        return value.get("name") in (
            "system:authenticated",
            "system:serviceaccounts",
            f"system:serviceaccounts:{subject.namespace}",
        )
    return False


async def inspect_kubernetes_subject(
    api: KubeApi, subject: IamSubject, now: datetime
) -> list[EvidenceCheck]:
    values: list[EvidenceCheck] = []
    account = await api.get(
        f"/api/v1/namespaces/{subject.namespace}/serviceaccounts/{subject.name}"
    )
    if account is None:
        return [check(subject.id, "IDENTITY_MISSING", SecurityResult.UNKNOWN, now)]
    for workload in subject.workloads:
        obj = await api.get(f"/apis/apps/v1/namespaces/{subject.namespace}/deployments/{workload}")
        spec = (obj or {}).get("spec", {}).get("template", {}).get("spec", {})
        if not spec:
            values.append(check(subject.id, "WORKLOAD_UNREADABLE", SecurityResult.UNKNOWN, now))
        elif spec.get("serviceAccountName", "default") != subject.name:
            values.append(check(subject.id, "WORKLOAD_IDENTITY_MISMATCH", SecurityResult.WARN, now))
    root = "/apis/rbac.authorization.k8s.io/v1"
    bindings = await read_all(api, root + "/clusterrolebindings")
    bindings += await read_all(api, root + "/rolebindings")
    matched = False
    for binding in bindings:
        if not any(_subject_matches(item, subject) for item in binding.get("subjects", [])):
            continue
        matched = True
        namespace = binding.get("metadata", {}).get("namespace")
        ref = binding.get("roleRef", {})
        kind, name = ref.get("kind"), ref.get("name")
        if kind == "ClusterRole":
            role = await api.get(root + f"/clusterroles/{name}")
        elif kind == "Role" and namespace:
            role = await api.get(root + f"/namespaces/{namespace}/roles/{name}")
        else:
            role = None
        if name in ("cluster-admin", "admin"):
            values.append(check(subject.id, "ADMINISTRATOR_ROLE", SecurityResult.WARN, now))
        values.extend(
            judge_rules(subject, None if role is None else role.get("rules", []), namespace, now)
        )
    if not matched:
        # RBAC 밖 authorizer를 고려해 Binding 0건만으로 권한 없음을 증명하지 않는다.
        values.append(check(subject.id, "NO_BINDING_OBSERVED", SecurityResult.UNKNOWN, now))
    return values


def judge_aws_policy(subject: IamSubject, document: object, now: datetime) -> list[EvidenceCheck]:
    if not isinstance(document, Mapping):
        return [check(subject.id, "POLICY_INVALID", SecurityResult.UNKNOWN, now, CLOUD)]
    statements = document.get("Statement")
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list):
        return [check(subject.id, "POLICY_INVALID", SecurityResult.UNKNOWN, now, CLOUD)]
    values: list[EvidenceCheck] = []
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") not in ("Allow", "Deny"):
            values.append(check(subject.id, "POLICY_INVALID", SecurityResult.UNKNOWN, now, CLOUD))
            continue
        if statement["Effect"] == "Deny":
            # Deny를 일반적으로 해석해 실효 권한을 단정하지 않는다.
            values.append(
                check(
                    subject.id,
                    "DENY_POLICY_REQUIRES_EVALUATION",
                    SecurityResult.UNKNOWN,
                    now,
                    CLOUD,
                )
            )
            continue
        actions = statement.get("Action", [])
        resources = statement.get("Resource", [])
        actions = [actions] if isinstance(actions, str) else actions
        resources = [resources] if isinstance(resources, str) else resources
        if (
            not isinstance(actions, list)
            or not actions
            or not isinstance(resources, list)
            or not resources
            or any(not isinstance(v, str) for v in [*actions, *resources])
        ):
            values.append(check(subject.id, "POLICY_INVALID", SecurityResult.UNKNOWN, now, CLOUD))
            continue
        if any("*" in action or "?" in action for action in actions):
            values.append(check(subject.id, "WILDCARD_PERMISSION", SecurityResult.WARN, now, CLOUD))
        if any(resource == "*" for resource in resources):
            values.append(check(subject.id, "UNSCOPED_RESOURCE", SecurityResult.WARN, now, CLOUD))
        if statement.get("Condition") or "NotAction" in statement or "NotResource" in statement:
            values.append(
                check(subject.id, "CONDITIONAL_POLICY", SecurityResult.UNKNOWN, now, CLOUD)
            )
        if not subject.allowed_actions or not subject.allowed_resources:
            values.append(
                check(subject.id, "ALLOWLIST_MISSING", SecurityResult.UNKNOWN, now, CLOUD)
            )
        elif not set(actions).issubset(subject.allowed_actions) or not set(resources).issubset(
            subject.allowed_resources
        ):
            values.append(
                check(subject.id, "PERMISSION_OUTSIDE_ALLOWLIST", SecurityResult.WARN, now, CLOUD)
            )
    return values or [check(subject.id, "PERMISSION_WITHIN_SCOPE", SecurityResult.PASS, now, CLOUD)]


def judge_gcp_permissions(
    subject: IamSubject, permissions: object, now: datetime
) -> list[EvidenceCheck]:
    if not isinstance(permissions, list) or any(not isinstance(v, str) for v in permissions):
        return [check(subject.id, "ROLE_UNREADABLE", SecurityResult.UNKNOWN, now, CLOUD)]
    if not subject.allowed_actions:
        return [check(subject.id, "ALLOWLIST_MISSING", SecurityResult.UNKNOWN, now, CLOUD)]
    return [
        check(
            subject.id,
            "PERMISSION_WITHIN_SCOPE"
            if set(permissions).issubset(subject.allowed_actions)
            else "PERMISSION_OUTSIDE_ALLOWLIST",
            SecurityResult.PASS
            if set(permissions).issubset(subject.allowed_actions)
            else SecurityResult.WARN,
            now,
            CLOUD,
        )
    ]
