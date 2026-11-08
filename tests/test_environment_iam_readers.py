"""외부 IAM 읽기 경계·페이지 누락·권한 거부를 자동 관측 결과와 대조한다."""

from datetime import UTC, datetime

import httpx
import pytest

from app.clients.environment_cloud_iam import inspect_gcp_subject
from app.clients.environment_iam import inspect_kubernetes_subject, read_all
from app.enums import SecurityResult as R
from app.security_environment.types import IamSubject

NOW = datetime(2026, 11, 8, tzinfo=UTC)


class Kube:
    def __init__(self):
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        if path.endswith("/serviceaccounts/worker"):
            return {"metadata": {"name": "worker"}}
        if path.endswith("/deployments/api"):
            return {"spec": {"template": {"spec": {"serviceAccountName": "other"}}}}
        if path.endswith("/clusterrolebindings"):
            return {
                "items": [
                    {
                        "subjects": [{"kind": "Group", "name": "system:serviceaccounts"}],
                        "roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
                    }
                ]
            }
        if path.endswith("/rolebindings"):
            return {"items": []}
        if path.endswith("/clusterroles/cluster-admin"):
            return {"rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]}
        raise AssertionError(path)


async def test_actual_bindings_and_workload_identity_are_read() -> None:
    api = Kube()
    subject = IamSubject(
        id="worker", kind="kubernetes", namespace="echo-was", name="worker", workloads=("api",)
    )
    values = await inspect_kubernetes_subject(api, subject, NOW)
    reasons = {v.reason for v in values}
    assert {"WORKLOAD_IDENTITY_MISMATCH", "ADMINISTRATOR_ROLE", "WILDCARD_PERMISSION"} <= reasons
    assert all("/secrets" not in path and "/exec" not in path for path, _ in api.calls)


async def test_binding_pagination_does_not_silently_truncate() -> None:
    class Pages:
        async def get(self, path, params):
            if not params["continue"]:
                return {"items": [{"id": "one"}], "metadata": {"continue": "page2"}}
            return {"items": [{"id": "two"}], "metadata": {}}

    assert await read_all(Pages(), "/bindings") == [{"id": "one"}, {"id": "two"}]


async def test_repeating_binding_cursor_is_failure() -> None:
    class Repeats:
        async def get(self, path, params):
            return {"items": [], "metadata": {"continue": "same"}}

    with pytest.raises(ValueError):
        await read_all(Repeats(), "/bindings")


async def test_gcp_ancestor_binding_and_role_expansion() -> None:
    requests = []
    subject = IamSubject(
        id="gsa",
        kind="gcp",
        name="worker@echo-project.iam.gserviceaccount.com",
        project_id="echo-project",
        allowed_actions=("container.pods.get",),
    )

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path == "/v3/projects/echo-project:getIamPolicy":
            return httpx.Response(200, json={"bindings": []})
        if path == "/v3/projects/echo-project":
            return httpx.Response(200, json={"parent": "organizations/123"})
        if path == "/v3/organizations/123:getIamPolicy":
            return httpx.Response(
                200,
                json={
                    "bindings": [
                        {"role": "roles/owner", "members": ["serviceAccount:" + subject.name]}
                    ]
                },
            )
        if path == "/v3/organizations/123":
            return httpx.Response(200, json={})
        if path == "/v1/roles/owner":
            return httpx.Response(200, json={"includedPermissions": ["container.pods.delete"]})
        raise AssertionError(path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        checks = await inspect_gcp_subject(http, "test-token", subject, NOW)
    assert any(c.reason == "ADMINISTRATOR_ROLE" for c in checks)
    assert any(c.result == R.WARN for c in checks)
    assert any(c.reason == "EFFECTIVE_PERMISSION_NOT_PROVEN" for c in checks)
    assert all(r.method == "GET" or r.url.path.endswith(":getIamPolicy") for r in requests)


async def test_gcp_denied_read_never_passes() -> None:
    subject = IamSubject(
        id="gsa",
        kind="gcp",
        name="worker@echo-project.iam.gserviceaccount.com",
        project_id="echo-project",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403))
    ) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await inspect_gcp_subject(http, "test-token", subject, NOW)


@pytest.mark.parametrize("failure", [None, "denied", "scope"])
async def test_aws_real_sdk_read_contract_and_error_propagation(monkeypatch, failure):
    from botocore.credentials import Credentials
    from botocore.exceptions import ClientError
    from botocore.session import Session
    from botocore.stub import Stubber

    from app.adapters.aws import AwsEnvironmentConfig
    from app.clients import environment_cloud_iam as module

    subject = IamSubject(id="executor", kind="aws", name="arn:aws:iam::123456789012:role/worker")
    clients = {
        name: Session().create_client(
            name,
            region_name="us-east-1",
            aws_access_key_id="local-test",
            aws_secret_access_key="local-test",
        )
        for name in ("iam", "eks")
    }
    iam, eks = Stubber(clients["iam"]), Stubber(clients["eks"])
    role = {
        "Path": "/",
        "RoleName": "worker",
        "RoleId": "A" * 16,
        "Arn": subject.name if failure != "scope" else subject.name + "-wrong",
        "CreateDate": NOW,
    }
    if failure == "denied":
        iam.add_client_error(
            "get_role", service_error_code="AccessDenied", expected_params={"RoleName": "worker"}
        )
    else:
        iam.add_response("get_role", {"Role": role}, {"RoleName": "worker"})
    if failure is None:
        iam.add_response(
            "list_role_policies",
            {"PolicyNames": ["wide"], "IsTruncated": False},
            {"RoleName": "worker"},
        )
        iam.add_response(
            "get_role_policy",
            {
                "RoleName": "worker",
                "PolicyName": "wide",
                "PolicyDocument": (
                    '{"Version":"2012-10-17","Statement":'
                    '[{"Effect":"Allow","Action":"*","Resource":"*"}]}'
                ),
            },
            {"RoleName": "worker", "PolicyName": "wide"},
        )
        iam.add_response(
            "list_attached_role_policies",
            {"AttachedPolicies": [], "IsTruncated": False},
            {"RoleName": "worker"},
        )
        eks.add_response(
            "describe_access_entry",
            {"accessEntry": {"principalArn": subject.name}},
            {"clusterName": "test", "principalArn": subject.name},
        )
        eks.add_response(
            "list_associated_access_policies",
            {"associatedAccessPolicies": [], "nextToken": "next"},
            {"clusterName": "test", "principalArn": subject.name},
        )
        eks.add_response(
            "list_associated_access_policies",
            {
                "associatedAccessPolicies": [
                    {
                        "policyArn": (
                            "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
                        ),
                        "accessScope": {"type": "cluster"},
                    }
                ]
            },
            {"clusterName": "test", "principalArn": subject.name, "nextToken": "next"},
        )

    class Factory:
        def create_client(self, name, **kwargs):
            return clients[name]

    monkeypatch.setattr(module, "Session", Factory)
    with iam, eks:
        if failure:
            with pytest.raises(ClientError if failure == "denied" else ValueError):
                await module.inspect_aws_subject(
                    Credentials("local", "local"),
                    AwsEnvironmentConfig(region="us-east-1", cluster_name="test"),
                    subject,
                    NOW,
                )
        else:
            values = await module.inspect_aws_subject(
                Credentials("local", "local"),
                AwsEnvironmentConfig(region="us-east-1", cluster_name="test"),
                subject,
                NOW,
            )
            assert {
                "WILDCARD_PERMISSION",
                "CLUSTER_ADMIN_ACCESS",
                "EFFECTIVE_PERMISSION_NOT_PROVEN",
            } <= {v.reason for v in values}
        iam.assert_no_pending_responses()
        eks.assert_no_pending_responses()
