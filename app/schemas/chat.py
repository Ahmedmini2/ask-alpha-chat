from datetime import datetime
from typing import Optional, Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    channel: str = Field(default="website")


class ChatResponse(BaseModel):
    reply: str
    model: str
    channel: str
    conversation_id: UUID
    message_id: int
    cards: list[dict] = []


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    user_id: Optional[UUID] = None
    title: str
    project_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    conversation_id: UUID
    role: str
    content: str
    cards: Optional[Any] = None
    created_at: datetime
