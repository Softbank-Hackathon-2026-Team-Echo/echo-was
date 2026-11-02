"""로그 응답 직전에 알려진 비밀값 모양을 가린다.

실행기 계약은 비밀값을 가려서 돌려주는 것이다. 이 함수는 그 위에 한 번 더 거르는 방어용이며
실행기의 마스킹 책임을 대신하지 않는다. 가린 자리는 MASK 로 바꾼다(Bearer 는 낱말을 남긴다).
"""

import re

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
)


def mask_secrets(text: str) -> str:
    masked = _PRIVATE_KEY_BLOCK.sub(MASK, text)
    masked = _BEARER.sub(rf"\1 {MASK}", masked)
    return _TOKENS.sub(MASK, masked)
