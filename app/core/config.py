from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ENVIRONMENT_CHECK_TIMEOUT_SECONDS = 5.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # 환경 접속 확인(비밀값 조회 + 접속 확인 + 클러스터 정보 읽기) 전체에 거는 제한 시간.
    environment_check_timeout_seconds: float = Field(
        default=DEFAULT_ENVIRONMENT_CHECK_TIMEOUT_SECONDS, gt=0
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
