"""읽기 전용 test dataset. 현재 DB·계획·감사·worker를 import하거나 호출하지 않는다."""

import json
from pathlib import Path

from app.schemas.disaster_history import DisasterHistoryResponse


class DisasterHistoryService:
    def get_history(self, *, enabled: bool) -> DisasterHistoryResponse:
        if not enabled:
            return DisasterHistoryResponse(enabled=False, items=[])
        path = Path(__file__).resolve().parents[1] / "data/disaster_history/scenarios.json"
        return DisasterHistoryResponse.model_validate(
            {
                "enabled": True,
                "isSimulated": True,
                "source": "DEMO",
                "items": json.loads(path.read_text()),
            }
        )
