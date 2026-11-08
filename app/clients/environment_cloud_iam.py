"""클라우드 신원에 부여된 정책을 읽는다. 실효 권한을 추측해 PASS로 만들지 않는다."""

import asyncio
from datetime import datetime
from typing import Any

import httpx
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.session import Session

from app.adapters.aws import AwsEnvironmentConfig
from app.clients.environment_iam import CLOUD, check, judge_aws_policy, judge_gcp_permissions
from app.enums import SecurityResult
from app.security_environment.types import EvidenceCheck, IamSubject


def _aws_inspect(
    credentials: Credentials, config: AwsEnvironmentConfig, subject: IamSubject, now: datetime
) -> list[EvidenceCheck]:
    kwargs: dict[str, Any] = {
        "aws_access_key_id": credentials.access_key,
        "aws_secret_access_key": credentials.secret_key,
        "aws_session_token": credentials.token,
        "config": Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 0}),
    }
    iam: Any = Session().create_client("iam", region_name="us-east-1", **kwargs)
    eks: Any = Session().create_client("eks", region_name=config.region, **kwargs)
    values: list[EvidenceCheck] = []
    try:
        name = subject.name.rsplit("/", 1)[-1]
        role = iam.get_role(RoleName=name)["Role"]
        if role.get("Arn") != subject.name:
            raise ValueError("role scope mismatch")
        if role.get("PermissionsBoundary"):
            values.append(
                check(subject.id, "PERMISSIONS_BOUNDARY", SecurityResult.UNKNOWN, now, CLOUD)
            )
        policies: list[object] = []
        for page in iam.get_paginator("list_role_policies").paginate(
            RoleName=name, PaginationConfig={"MaxItems": 100}
        ):
            if page.get("IsTruncated"):
                values.append(
                    check(subject.id, "POLICY_LIST_INCOMPLETE", SecurityResult.UNKNOWN, now, CLOUD)
                )
            for policy_name in page["PolicyNames"]:
                policies.append(
                    iam.get_role_policy(RoleName=name, PolicyName=policy_name)["PolicyDocument"]
                )
        for page in iam.get_paginator("list_attached_role_policies").paginate(
            RoleName=name, PaginationConfig={"MaxItems": 100}
        ):
            if page.get("IsTruncated"):
                values.append(
                    check(subject.id, "POLICY_LIST_INCOMPLETE", SecurityResult.UNKNOWN, now, CLOUD)
                )
            for attached in page["AttachedPolicies"]:
                arn = attached["PolicyArn"]
                if attached.get("PolicyName") in ("AdministratorAccess", "PowerUserAccess"):
                    values.append(
                        check(subject.id, "ADMINISTRATOR_ROLE", SecurityResult.WARN, now, CLOUD)
                    )
                metadata = iam.get_policy(PolicyArn=arn)["Policy"]
                policies.append(
                    iam.get_policy_version(PolicyArn=arn, VersionId=metadata["DefaultVersionId"])[
                        "PolicyVersion"
                    ]["Document"]
                )
        for policy in policies:
            values.extend(judge_aws_policy(subject, policy, now))
        entry = eks.describe_access_entry(
            clusterName=config.cluster_name, principalArn=subject.name
        )["accessEntry"]
        if entry.get("principalArn") != subject.name:
            raise ValueError("EKS principal mismatch")
        token: str | None = None
        for _ in range(10):
            args: dict[str, Any] = {
                "clusterName": config.cluster_name,
                "principalArn": subject.name,
            }
            if token:
                args["nextToken"] = token
            page = eks.list_associated_access_policies(**args)
            for policy in page.get("associatedAccessPolicies", []):
                if policy.get("policyArn", "").endswith(
                    ("AmazonEKSClusterAdminPolicy", "AmazonEKSAdminPolicy")
                ):
                    values.append(
                        check(subject.id, "CLUSTER_ADMIN_ACCESS", SecurityResult.WARN, now, CLOUD)
                    )
                else:
                    values.append(
                        check(
                            subject.id,
                            "EKS_POLICY_REQUIRES_EVALUATION",
                            SecurityResult.UNKNOWN,
                            now,
                            CLOUD,
                        )
                    )
            token = page.get("nextToken")
            if not token:
                break
        else:
            values.append(
                check(subject.id, "POLICY_LIST_INCOMPLETE", SecurityResult.UNKNOWN, now, CLOUD)
            )
        # IAM policy만으로 SCP/세션 정책 등 전체 실효 권한을 증명하지 않는다.
        values.append(
            check(subject.id, "EFFECTIVE_PERMISSION_NOT_PROVEN", SecurityResult.UNKNOWN, now, CLOUD)
        )
        return values
    finally:
        iam.close()
        eks.close()


async def inspect_aws_subject(
    credentials: Credentials, config: AwsEnvironmentConfig, subject: IamSubject, now: datetime
) -> list[EvidenceCheck]:
    return await asyncio.to_thread(_aws_inspect, credentials, config, subject, now)


async def inspect_gcp_subject(
    http: httpx.AsyncClient, token: str, subject: IamSubject, now: datetime
) -> list[EvidenceCheck]:
    headers = {"Authorization": f"Bearer {token}"}
    resource = f"projects/{subject.project_id}"
    visited: set[str] = set()
    values: list[EvidenceCheck] = []
    matched = False
    for _ in range(12):
        if resource in visited:
            raise ValueError("policy ancestry cycle")
        visited.add(resource)
        url = "https://cloudresourcemanager.googleapis.com/v3/" + resource
        response = await http.post(
            url + ":getIamPolicy", headers=headers, json={"options": {"requestedPolicyVersion": 3}}
        )
        response.raise_for_status()
        policy = response.json()
        if not isinstance(policy, dict) or not isinstance(policy.get("bindings", []), list):
            raise ValueError("invalid policy")
        for binding in policy.get("bindings", []):
            members = binding.get("members", [])
            member = f"serviceAccount:{subject.name}"
            if member not in members and not {"allUsers", "allAuthenticatedUsers"}.intersection(
                members
            ):
                continue
            matched = True
            role_name = binding.get("role")
            if not isinstance(role_name, str) or not role_name.startswith(
                ("roles/", "projects/", "organizations/")
            ):
                raise ValueError("invalid role name")
            if binding.get("condition"):
                values.append(
                    check(subject.id, "CONDITIONAL_POLICY", SecurityResult.UNKNOWN, now, CLOUD)
                )
            if role_name in (
                "roles/owner",
                "roles/editor",
                "roles/container.admin",
                "roles/iam.securityAdmin",
            ):
                values.append(
                    check(subject.id, "ADMINISTRATOR_ROLE", SecurityResult.WARN, now, CLOUD)
                )
            role = await http.get("https://iam.googleapis.com/v1/" + role_name, headers=headers)
            role.raise_for_status()
            values.extend(
                judge_gcp_permissions(subject, role.json().get("includedPermissions"), now)
            )
        metadata = await http.get(url, headers=headers)
        metadata.raise_for_status()
        parent = metadata.json().get("parent")
        if not parent:
            break
        if not isinstance(parent, str) or not parent.startswith(("folders/", "organizations/")):
            raise ValueError("invalid parent")
        resource = parent
    else:
        values.append(
            check(subject.id, "POLICY_ANCESTRY_INCOMPLETE", SecurityResult.UNKNOWN, now, CLOUD)
        )
    if not matched:
        values.append(check(subject.id, "NO_BINDING_OBSERVED", SecurityResult.UNKNOWN, now, CLOUD))
    # 리소스별 정책·deny/PAB 등을 아직 확인하지 못한 범위를 명시한다.
    values.append(
        check(subject.id, "EFFECTIVE_PERMISSION_NOT_PROVEN", SecurityResult.UNKNOWN, now, CLOUD)
    )
    return values
