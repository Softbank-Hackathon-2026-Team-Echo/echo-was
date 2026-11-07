import hashlib
import json
from pathlib import Path

from app.core.config import Settings
from app.services.disaster_history_service import DisasterHistoryService
from app.services.live_disaster_feed_rules import read_usgs_feed


def test_disaster_automation_defaults_remain_off():
    settings = Settings(database_url="postgresql+asyncpg://test@localhost/test", _env_file=None)
    assert not settings.live_disaster_feed_enabled
    assert not settings.auto_dr_enabled
    assert settings.auto_dr_consent_ids == []
    assert not settings.disaster_history_demo_enabled
    assert not settings.migration_db_cutover_on_disaster


def test_historical_provenance_matches_original_bytes_and_explicit_demo():
    root = Path(__file__).parents[1] / "app/data/disaster_history"
    response = DisasterHistoryService().get_history(enabled=True)
    assert len(response.items) == 2
    for item, name in zip(response.items, ["tohoku", "kumamoto"], strict=True):
        raw = (root / f"{name}.geojson").read_bytes()
        assert item.event.raw_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
        assert item.event.event_id == json.loads(raw)["features"][0]["id"]
        parsed = read_usgs_feed(json.loads(raw))
        assert len(parsed.events) == 1 and not parsed.skipped_count
        assert item.event.occurred_at == parsed.events[0].occurred_at
        detail = (root / f"{name}-detail.geojson").read_bytes()
        assert item.event.detail_raw_sha256 == "sha256:" + hashlib.sha256(detail).hexdigest()
        assert item.event.shake_map_url in detail.decode()
        assert item.is_simulated and item.source == "DEMO"
        assert item.event.origin_source == "REAL_HISTORICAL"
        assert item.radius_source == "DEMO_ASSUMPTION"
