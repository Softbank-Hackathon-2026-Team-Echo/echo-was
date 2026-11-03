"""시험 DB 이름 규칙(통합 시험 conftest 가 쓴다)."""

import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def checkout_database_name(name: str, root: Path = PROJECT_ROOT) -> str:
    """작업 폴더마다 다른 시험 DB 이름. `_test` 로 끝나는 규칙은 그대로 지킨다."""
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return f"{name.removesuffix('_test')}_{digest}_test"
