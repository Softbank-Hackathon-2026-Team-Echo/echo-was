"""선택 수신 receipt 변환. 저장형식·SQL·계획 쌍의 성공 판정은 여기서 정하지 않는다."""

import json
import re
from typing import Any

_RECEIPT_PATTERNS = {
    "planId": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    "sourceDbId": r"[a-z][a-z0-9-]{0,62}",
    "targetDbId": r"[a-z][a-z0-9-]{0,62}",
    "contractVersion": r"db-pair/v1",
    "registrySha256": r"[0-9a-f]{64}",
    "writerDbId": r"[a-z][a-z0-9-]{0,62}",
}


def decode_pair_receipt(payload: object) -> dict[str, Any] | None:
    """6필드 원본만 허용한다. 보정·기본값·요청 값으로 증거를 채우지 않는다.

    형식 통과는 계획/쌍/writer 일치나 성공을 뜻하지 않는다. 그 판정은 기존 JUDGE가 한다.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            return None
    if not isinstance(payload, dict) or payload.keys() != _RECEIPT_PATTERNS.keys():
        return None
    for name, pattern in _RECEIPT_PATTERNS.items():
        value = payload[name]
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            return None
    return dict(payload)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            # json.loads 의 last-value-wins로 오염된 증거가 복구되지 않게 한다.
            raise ValueError("duplicate receipt key")
        result[name] = value
    return result
