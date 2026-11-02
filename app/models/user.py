from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin

GITHUB_LOGIN_MAX_LENGTH = 39
USER_NAME_MAX_LENGTH = 255
AVATAR_URL_MAX_LENGTH = 500


class User(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """GitHub 로 로그인한 사람. GitHub 숫자 번호(github_id)로 같은 사람을 가린다.

    github_login·name·avatar_url 은 로그인할 때마다 GitHub 값으로 갱신한다(아이디는 바뀔 수 있다).
    GitHub 사용자 토큰은 저장하지 않는다.
    """

    __tablename__ = "users"

    github_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    github_login: Mapped[str] = mapped_column(String(GITHUB_LOGIN_MAX_LENGTH), nullable=False)
    name: Mapped[str | None] = mapped_column(String(USER_NAME_MAX_LENGTH), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(AVATAR_URL_MAX_LENGTH), nullable=True)
