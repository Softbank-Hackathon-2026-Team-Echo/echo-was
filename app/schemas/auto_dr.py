import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CreateAutoDrConsentRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    service_id: uuid.UUID
    source_environment_id: uuid.UUID
    target_environment_id: uuid.UUID
    expires_at: datetime
    acknowledge_automatic_execution: Literal[True]


class AutoDrConsentResponse(BaseModel):
    consent_id: uuid.UUID = Field(alias="consentId")
