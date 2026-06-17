import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from app.schemas.chat import ChatRequest, ChatResponse, ConversationOut, MessageOut
from app.config import settings
from app.core.auth import authed_user_id, resolve_user_id, auth_enabled
from app.db.session import get_db
from app.db.models import AskAlphaConversation, AskAlphaMessage
from app.core.orchestrator import chat_turn

log = logging.getLogger("askalpha.chat")

router = APIRouter(prefix="/v1", tags=["chat"])


def _truncate(s: str, n: int = 200) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    authed: UUID | None = Depends(authed_user_id),
):
    # The verified token (when auth is enabled) is authoritative; the body user_id is no longer
    # trusted on its own. resolve_user_id raises 403 if a body user_id contradicts the token.
    user_id = resolve_user_id(authed, req.user_id)
    # IDOR guard: a client-supplied conversation_id must not let one user read/write another
    # user's conversation. _get_or_create_conversation loads it by id alone, so enforce ownership
    # here before the turn runs.
    await _assert_may_use_conversation(db, req.conversation_id, user_id)
    log.info("chat request  conv=%s user=%s authed=%s  msg=%r",
             req.conversation_id, user_id, authed is not None, _truncate(req.message, 160))
    try:
        result = await chat_turn(
            db,
            user_message=req.message,
            conversation_id=req.conversation_id,
            user_id=user_id,
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
    except HTTPException:
        raise
    except Exception as e:
        log.exception("chat error: %s", e)
        raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    authed: UUID | None = Depends(authed_user_id),
    user_id: UUID | None = Query(None, description="Filter by user (UUID). Ignored when auth is enabled (scoped to the token)."),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    # When auth is enabled, conversations are scoped to the authenticated user — the query param
    # is ignored (it was a free cross-user enumeration vector). A caller with no token must sign in.
    if auth_enabled():
        if authed is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_id = authed
    stmt = select(AskAlphaConversation).order_by(AskAlphaConversation.updated_at.desc()).limit(limit).offset(offset)
    if user_id is not None:
        stmt = stmt.where(AskAlphaConversation.user_id == user_id)
    else:
        stmt = stmt.where(AskAlphaConversation.user_id.is_(None))
    return (await db.execute(stmt)).scalars().all()


async def _assert_may_use_conversation(
    db: AsyncSession, conversation_id: UUID | None, resolved_user_id: UUID | None
) -> None:
    """IDOR guard for the write path (POST /chat). When auth is enabled, a caller may continue an
    EXISTING conversation only if it's unowned (anonymous, user_id IS NULL — a bearer-by-UUID
    chat) or owned by their resolved identity. A user-owned conversation is invisible (404) to
    everyone else, closing cross-tenant read (history leaks into the model context) and write
    (pollution of the victim's thread). No-op when auth is disabled, when no conversation_id was
    sent, or when the id is unknown (the orchestrator then mints a fresh conversation)."""
    if not auth_enabled() or conversation_id is None:
        return
    row = (await db.execute(
        select(AskAlphaConversation.id, AskAlphaConversation.user_id)
        .where(AskAlphaConversation.id == conversation_id)
    )).one_or_none()
    if row is None:
        return
    if row.user_id is not None and row.user_id != resolved_user_id:
        raise HTTPException(status_code=404, detail="Conversation not found")


async def _load_owned_conversation(
    db: AsyncSession, conversation_id: UUID, authed: UUID | None
) -> AskAlphaConversation:
    """Fetch a conversation, enforcing ownership when auth is enabled. Returns 404 (not 403) for
    a conversation owned by someone else so existence isn't leaked."""
    conv = (await db.execute(
        select(AskAlphaConversation).where(AskAlphaConversation.id == conversation_id)
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if auth_enabled():
        if authed is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if conv.user_id != authed:
            raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    authed: UUID | None = Depends(authed_user_id),
):
    return await _load_owned_conversation(db, conversation_id, authed)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def get_messages(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    authed: UUID | None = Depends(authed_user_id),
    limit: int = Query(200, ge=1, le=500),
):
    await _load_owned_conversation(db, conversation_id, authed)  # 404/401 if not owned
    rows = (await db.execute(
        select(AskAlphaMessage)
        .where(AskAlphaMessage.conversation_id == conversation_id)
        .order_by(AskAlphaMessage.id.asc())
        .limit(limit)
    )).scalars().all()
    return rows
