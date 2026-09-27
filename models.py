from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class ComposedMessage(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    send_as: Literal["vera", "merchant_on_behalf"]
    trigger_id: str
    template_name: Optional[str] = None
    template_params: List[str] = Field(default_factory=list)
    body: str
    cta: str
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: List[ComposedMessage] = Field(default_factory=list)


class ReplyResponse(BaseModel):
    action: Literal["send", "wait", "end"]
    body: Optional[str] = None
    cta: Optional[str] = None
    wait_seconds: Optional[int] = None
    rationale: str


# These aliases keep the public Python surface compatible with the original
# submission while deliberately allowing the challenge's category-specific
# payloads to evolve. Context validation is handled at the HTTP boundary in
# storage.py; raw fields are preserved instead of being rejected.
CategoryContext = Dict[str, Any]
MerchantContext = Dict[str, Any]
TriggerContext = Dict[str, Any]
CustomerContext = Dict[str, Any]
