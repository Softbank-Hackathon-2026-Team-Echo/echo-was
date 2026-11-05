"""재난 판정 순수 규칙: 규모 기준·반경·거리·분류·도착 고르기(API 서버 9)."""

import uuid
from decimal import Decimal

import pytest

from app.core.places import (
    AWS_REGION_COORDINATES,
    GCP_REGION_COORDINATES,
    Coordinates,
)
from app.enums import DisasterPlanOutcome, DisasterType, MigrationPlanStatus
from app.services.disaster_rules import (
    EnvironmentLocation,
    TargetCandidate,
    choose_target,
    classify_environments,
    distance_km,
    meets_threshold,
    radius_km,
    summarize_plan,
)

NEAR_TOKYO = Coordinates(35.2, 139.4)
TOKYO = AWS_REGION_COORDINATES["ap-northeast-1"]
SEOUL = GCP_REGION_COORDINATES["asia-northeast3"]
OSAKA = AWS_REGION_COORDINATES["ap-northeast-3"]
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("magnitude", "expected"),
    [
        # 식 값 302.48 을 10km 단위로 반올림한다(계약 예시·AC-1 의 300).
        ("7.2", 300),
        ("7.0", 250),
        ("6.9", 230),
        ("5.2", 50),
        ("10.0", 3760),
        ("0.0", 0),
    ],
)
def test_radius_km_rounds_formula_to_ten_km(magnitude: str, expected: int) -> None:
    assert radius_km(Decimal(magnitude)) == expected


@pytest.mark.parametrize(
    ("disaster_type", "magnitude", "expected"),
    [
        (DisasterType.EARTHQUAKE, "7.0", True),
        (DisasterType.EARTHQUAKE, "7.2", True),
        (DisasterType.EARTHQUAKE, "6.9", False),
        (DisasterType.TYPHOON, "0.0", True),
        (DisasterType.TYPHOON, "6.9", True),
    ],
)
def test_meets_threshold_earthquake_needs_seven_typhoon_always(
    disaster_type: DisasterType, magnitude: str, expected: bool
) -> None:
    assert meets_threshold(disaster_type, Decimal(magnitude)) is expected


def test_distance_km_matches_haversine_examples() -> None:
    assert distance_km(TOKYO, TOKYO) == 0
    # 서울-도쿄 약 1,150km, 좌표 순서와 상관없다.
    assert distance_km(SEOUL, TOKYO) == pytest.approx(1153, abs=5)
    assert distance_km(SEOUL, TOKYO) == distance_km(TOKYO, SEOUL)
    # 적도에서 경도 1도는 약 111.2km 다.
    assert distance_km(Coordinates(0, 0), Coordinates(0, 1)) == pytest.approx(111.19, abs=0.01)


def test_classify_near_tokyo_m72_affects_tokyo_and_keeps_seoul_safe_and_unknown_apart() -> None:
    tokyo, seoul, nowhere = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    result = classify_environments(
        [
            EnvironmentLocation(tokyo, TOKYO),
            EnvironmentLocation(seoul, SEOUL),
            EnvironmentLocation(nowhere, None),
        ],
        NEAR_TOKYO,
        radius_km(Decimal("7.2")),
    )

    assert (result.affected, result.safe, result.unknown) == ((tokyo,), (seoul,), (nowhere,))


def test_classify_environment_exactly_on_radius_is_affected() -> None:
    environment_id = uuid.uuid4()
    far = Coordinates(0, 1)
    exact = distance_km(far, Coordinates(0, 0))

    just_inside = classify_environments(
        [EnvironmentLocation(environment_id, far)], Coordinates(0, 0), int(exact)
    )
    exactly = classify_environments(
        [EnvironmentLocation(environment_id, far)], Coordinates(0, 0), exact
    )

    assert just_inside.safe == (environment_id,)
    assert exactly.affected == (environment_id,)


def _candidate(
    coordinates: Coordinates,
    digest: str = DIGEST_A,
    *,
    failed: bool = False,
    entrypoint: bool = True,
    environment_id: uuid.UUID | None = None,
) -> TargetCandidate:
    return TargetCandidate(
        environment_id=environment_id or uuid.uuid4(),
        coordinates=coordinates,
        image_digest=digest,
        is_connection_failed=failed,
        has_entrypoint_name=entrypoint,
    )


def test_choose_target_picks_farthest_same_image() -> None:
    near, far = _candidate(OSAKA), _candidate(SEOUL)

    assert choose_target([near, far], DIGEST_A, NEAR_TOKYO) == far


def test_choose_target_prefers_same_image_over_distance() -> None:
    near_same, far_other = _candidate(OSAKA), _candidate(SEOUL, DIGEST_B)

    assert choose_target([far_other, near_same], DIGEST_A, NEAR_TOKYO) == near_same


def test_choose_target_without_same_image_returns_farthest_for_plan_to_judge() -> None:
    near_other, far_other = _candidate(OSAKA, DIGEST_B), _candidate(SEOUL, DIGEST_B)

    assert choose_target([near_other, far_other], DIGEST_A, NEAR_TOKYO) == far_other


def test_choose_target_drops_failed_and_nameless_environments() -> None:
    failed = _candidate(SEOUL, failed=True)
    nameless = _candidate(SEOUL, entrypoint=False)
    usable = _candidate(OSAKA)

    assert choose_target([failed, nameless, usable], DIGEST_A, NEAR_TOKYO) == usable
    assert choose_target([failed, nameless], DIGEST_A, NEAR_TOKYO) is None
    assert choose_target([], DIGEST_A, NEAR_TOKYO) is None


def test_choose_target_same_distance_takes_smaller_environment_id() -> None:
    small, large = sorted((uuid.uuid4(), uuid.uuid4()))
    first = _candidate(SEOUL, environment_id=large)
    second = _candidate(SEOUL, environment_id=small)

    chosen = choose_target([first, second], DIGEST_A, NEAR_TOKYO)

    assert chosen is not None and chosen.environment_id == small


@pytest.mark.parametrize(
    ("status", "is_approved", "expected"),
    [
        (MigrationPlanStatus.PENDING_APPROVAL, False, DisasterPlanOutcome.PENDING_APPROVAL),
        (MigrationPlanStatus.APPROVED, True, DisasterPlanOutcome.APPROVED),
        (MigrationPlanStatus.RUNNING, True, DisasterPlanOutcome.APPROVED),
        (MigrationPlanStatus.SUCCEEDED, True, DisasterPlanOutcome.APPROVED),
        (MigrationPlanStatus.ROLLED_BACK, True, DisasterPlanOutcome.APPROVED),
        (MigrationPlanStatus.REJECTED, False, DisasterPlanOutcome.SKIPPED),
        (MigrationPlanStatus.CANCELLED, False, DisasterPlanOutcome.SKIPPED),
        (MigrationPlanStatus.EXPIRED, False, DisasterPlanOutcome.SKIPPED),
        (MigrationPlanStatus.STALE, False, DisasterPlanOutcome.SKIPPED),
    ],
)
def test_summarize_plan_maps_status_to_outcome(
    status: MigrationPlanStatus, is_approved: bool, expected: DisasterPlanOutcome
) -> None:
    assert summarize_plan(status, is_approved) is expected
