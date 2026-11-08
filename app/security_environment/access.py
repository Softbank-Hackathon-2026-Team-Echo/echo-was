"""검사 전용 접속 설정. 기존 배포 실행기의 환경·자격을 변경하지 않는다."""

from dataclasses import replace

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.gcp import GcpEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.executors.base import ClusterEnvironment
from app.security_environment.types import EnvironmentSecurityProfile


def reader_environment(
    profile: EnvironmentSecurityProfile,
    environment: ClusterEnvironment,
    executor_aws_role_arn: str | None,
) -> ClusterEnvironment:
    access = profile.reader
    if access is None:
        raise ValueError("dedicated reader identity required")
    config = environment.config
    if isinstance(config, (AwsEnvironmentConfig, GcpEnvironmentConfig)):
        if not access.google_service_account:
            raise ValueError("dedicated Google workload identity required")
        if any(
            s.kind == "gcp" and s.name == access.google_service_account for s in profile.subjects
        ):
            raise ValueError("reader must differ from inspected execution identity")
    if isinstance(config, AwsEnvironmentConfig):
        role = access.aws_role_arn
        if not role or role in (config.role_arn, executor_aws_role_arn):
            raise ValueError("dedicated AWS reader role required")
        if any(s.kind == "aws" and s.name == role for s in profile.subjects):
            raise ValueError("reader must differ from inspected execution role")
        return replace(environment, config=config.model_copy(update={"role_arn": role}))
    if isinstance(config, OnpremEnvironmentConfig):
        if not access.onprem_token_ref or access.onprem_token_ref == environment.secret_ref:
            raise ValueError("dedicated onprem reader token required")
        return replace(environment, secret_ref=access.onprem_token_ref)
    if not isinstance(config, GcpEnvironmentConfig):
        raise ValueError("unsupported reader environment")
    return environment
