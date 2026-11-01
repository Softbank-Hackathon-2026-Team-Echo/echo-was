import os

# Settings 가 DATABASE_URL 을 필수로 요구한다. 단위 테스트는 실제 DB 에 붙지 않는다.
# 환경변수가 .env 보다 우선하므로, .env 가 있어도 앱 엔진은 이 닿지 않는 주소를 쓴다.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
