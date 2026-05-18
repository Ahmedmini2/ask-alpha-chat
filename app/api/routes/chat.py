import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from app.schemas.chat import ChatRequest, ChatResponse, ConversationOut, MessageOut
from app.config import settings
from app.db.session import get_db
from app.db.models import AskAlphaConversation, AskAlphaMessage
from app.core.orchestrator import chat_turn

log = logging.getLogger("askalpha.chat")

router = APIRouter(prefix="/v1", tags=["chat"])


def _truncate(s: str, n: int = 200) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, db: AsyncSession = Depends(get_db)):
    log.info("chat request  conv=%s user=%s  msg=%r",
             req.conversation_id, req.user_id, _truncate(req.message, 160))
    try:
        result = await chat_turn(
            db,
            user_message=req.message,
            conversation_id=req.conversation_id,
            user_id=req.user_id,
            channel=req.channel,
        )
        log.info("chat response conv=%s msg_id=%s cards=%s reply=%r",
                 result["conversation_id"], result["message_id"],
                 [c.get("type") for c in result["cards"]],
                 _truncate(result["reply"], 240))
        return ChatResponse(
            reply=result["reply"],
            model=settings.bedrock_model_id,
            channel=req.channel,
            conversation_id=result["conversation_id"],
            message_id=result["message_id"],
            cards=result["cards"],
        )
    except Exception as e:
        log.exception("chat error: %s", e)
        raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    user_id: UUID | None = Query(None, description="Filter by user (UUID). Omit to list anonymous (user_id IS NULL)."),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    stmt = select(AskAlphaConversation).order_by(AskAlphaConversation.updated_at.desc()).limit(limit).offset(offset)
    if user_id is not None:
        stmt = stmt.where(AskAlphaConversation.user_id == user_id)
    else:
        stmt = stmt.where(AskAlphaConversation.user_id.is_(None))
    return (await db.execute(stmt)).scalars().all()


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(conversation_id: UUID, db: AsyncSession = Depends(get_db)):
    conv = (await db.execute(
        select(AskAlphaConversation).where(AskAlphaConversation.id == conversation_id)
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def get_messages(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(200, ge=1, le=500),
):
    conv_exists = (await db.execute(
        select(AskAlphaConversation.id).where(AskAlphaConversation.id == conversation_id)
    )).scalar_one_or_none()
    if conv_exists is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    rows = (await db.execute(
        select(AskAlphaMessage)
        .where(AskAlphaMessage.conversation_id == conversation_id)
        .order_by(AskAlphaMessage.id.asc())
        .limit(limit)
    )).scalars().all()
    return rows
