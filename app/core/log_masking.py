"""로그 응답 직전에 알려진 비밀값 모양을 가린다.

실행기 계약은 비밀값을 가려서 돌려주는 것이다. 이 함수는 그 위에 한 번 더 거르는 방어용이며
실행기의 마스킹 책임을 대신하지 않는다. 가린 자리는 MASK 로 바꾼다(Bearer 는 낱말을 남긴다).
"""

import re
from typing import Any

MASK = "***"

# 개인 키 블록은 END 줄이 없어도(잘린 로그) BEGIN 부터 끝까지 가린다.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----|.*\Z)",
    re.DOTALL,
)
# 토큰은 8자 이상만 본다("the bearer of ..." 같은 문장을 가리지 않게).
_BEARER = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKENS = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub 토큰(설치 토큰 ghs_ 등)
    r"|AKIA[0-9A-Z]{16}"  # AWS 액세스 키
    r"|sk-[A-Za-z0-9_-]{20,}"  # API 비밀 키
    r"|echo_(?:agt|rnr|run)_[A-Za-z0-9_-]{20,}"  # Echo 에이전트·러너·실행 토큰
    # 단독 JWT(헤더.본문.서명, `Bearer` 없이 찍힌 세션·ID 토큰). 서명이 잘린 꼴도 가린다.
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"
)


def mask_secrets(text: str) -> str:
    masked = _PRIVATE_KEY_BLOCK.sub(MASK, text)
    masked = _BEARER.sub(rf"\1 {MASK}", masked)
    return _TOKENS.sub(MASK, masked)


def mask_secrets_in_json(value: Any) -> Any:
    """JSON 값의 모든 문자열(키 포함)을 가린 사본.

    직렬화한 글자 전체에 mask_secrets 를 걸면 잘린 개인 키 블록이 뒤따르는 JSON 구조까지 지워
    다시 읽을 수 없게 되므로 문자열마다 건다.
    """
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [mask_secrets_in_json(item) for item in value]
    if isinstance(value, dict):
        return {mask_secrets(key): mask_secrets_in_json(item) for key, item in value.items()}
    return value
