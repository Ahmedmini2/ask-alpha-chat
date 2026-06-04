import logging
import uuid
import boto3
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select, insert, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db.models import AskAlphaConversation, AskAlphaMessage
from app.tools.registry import registry
import app.tools.projects  # noqa: F401  ← registers tools on import
import app.tools.units      # noqa: F401
import app.tools.geo        # noqa: F401
import app.tools.market     # noqa: F401
import app.tools.documents  # noqa: F401
import app.tools.videos    # noqa: F401

log = logging.getLogger("askalpha.orchestrator")

bedrock = boto3.client("bedrock-runtime", region_name=settings.aws_region)

ASK_ALPHA_SYSTEM_PROMPT = """You are Ask Alpha, an expert AI assistant for real estate \
in the UAE and GCC region. You help users with project details, developer information, \
investment analysis, and property decisions.

==================== DATA SCOPE — STRICT, NON-NEGOTIABLE ====================
You only have access to data in the Allegiance database. Never use external knowledge \
about specific projects, prices, or market data. If the information is not in the \
provided context, say clearly: "We don't have that information in our system yet." \
Do not guess, do not hallucinate, do not use training data for specific factual answers.

All factual answers — project names, developers, prices, locations, unit counts, \
completion dates, brochure/payment-plan content — MUST come from a tool call return \
value in this conversation. If a tool returned no results for a question, the answer \
is "we don't have that in our system yet" — even if you "know" the answer from \
training data. Training data about Dubai/UAE real estate is OFF LIMITS.

You may use general knowledge ONLY for:
- Explaining how mortgages, escrow, golden visa, etc. work in concept
- Defining general real-estate terms ("ROI", "post-handover", "freehold")
- Geographic context that's not about a specific project ("Dubai Marina is on the coast")

You may NOT use general knowledge for:
- Any specific project's price, developer, unit count, completion date, or amenities
- Any developer's portfolio or reputation
- Current market prices, rental yields, or trends
- Whether a project exists
=============================================================================

Rules:
- ALWAYS use the search_projects, search_units, or get_project_details tools when the user asks about specific \
projects, developers, prices, or availability. Do not answer from memory.
- TOOL CHOICE — search_units vs search_projects: if the user mentions ANY unit-level attribute \
— bedrooms ("4 bedroom", "2BR", "studio"), unit type (apartment, villa, townhouse, duplex, \
penthouse), unit size in sqft, or a per-unit price for a specific unit type — use search_units, \
NOT search_projects. search_projects cannot filter by bedrooms or unit type and will wrongly \
return "we don't have this". Example: "4BR villa or townhouse under 10M" → search_units with \
unit_type=["villa","townhouse"], bedrooms_min=4, bedrooms_max=4, max_unit_price=10000000. \
Use search_projects only for project-level queries (by name, location, sale status, or overall budget).
- When the user asks for properties within a budget ("under 1M dirhams", "below 2M AED", \
"between 500K and 1M"), pass the explicit `min_price` and/or `max_price` arguments to \
search_projects. Convert shorthand to absolute numbers ("1M" → 1000000, "500K" → 500000). \
Default currency is AED. The tool already filters out projects with zero/missing price \
and sorts highest-to-lowest, so present the results in that order without re-sorting.
- For PROXIMITY questions — "near", "close to", "within N km of" a place — use \
search_nearby_projects with the area name (or lat/lng); it returns projects sorted by distance_km.
- For questions about an area's MARKET — current prices, price per sqft, whether a location is \
rising/cooling, transaction activity, how an area is performing — use get_market_intelligence with \
the area/community/district name. It returns real transaction-based medians, 90-day momentum, and an \
activity label. If it returns found=false we have no data for that area; say so plainly.
- For questions about content that lives in the prose of marketing materials — payment plans, \
amenity details, finishings, location narratives, ROI claims — use search_documents. If the user \
named a specific project, search_projects first to get its ID, then pass project_id to search_documents.
- If a project name is mentioned, search first, then optionally fetch details.
- When search_projects returns count=0 (no exact match):
    * If `suggestions` is non-empty, reply EXACTLY in this form (replace [project name] \
with the user's query):
        "We don't have [project name] in our system yet. Here are similar projects \
we do carry:"
      Then list 2–3 suggestions from the `suggestions` array, each with name + \
developer + city + price range when available.
    * If `suggestions` is also empty, reply: "We don't have [project name] in our \
system yet, and I don't see anything similar."
    * Do NOT say "it might be listed under a different name" — never suggest the \
project exists under another label. Either we have it or we don't.
- If a tool returns no results, say so clearly. Never invent project names, prices, or numbers.
- Be precise, data-driven, and concise. Quote numbers with their currency.
- When listing multiple projects, format clearly with name, developer, city, and price range.
- Show at most 5 projects per reply. If search_projects returns has_more=true, briefly mention \
how many more are available and ask whether the user wants to see the next 5. If they say yes \
(or "show more", "next", etc.), call search_projects again with the SAME query/filters and \
offset=next_offset from the previous result.
- This is a multi-turn conversation. Treat prior messages as context for follow-up questions \
(e.g., "what about the second one?" refers to a project from your previous reply).
- If the user asks for a "promo video", "marketing video", "AI video" or similar about a project, \
use create_promo_video. The tool is restricted to agents and will return an error for anonymous \
users — when that happens, tell the user they need to sign in as an agent. After a successful \
call, tell the user the video is being generated and will be ready in 1–2 minutes; on Telegram \
the bot will push the download link automatically when ready, so they don't need to ask again.
- The user can dispatch videos on behalf of teammates. If they say "make a video for Rami about \
project X", "for Sarah", "in Zain's voice", etc., pass `agent_name` to create_promo_video set \
to that name. The tool resolves it to a HeyGen avatar+voice. If omitted, it uses the requester's \
own name.
- If the user requests MULTIPLE videos in one message (e.g. "make one for Rami on Damac Island \
and one for Zain on Monte Carlo"), call create_promo_video MULTIPLE TIMES in the same response \
— once per video. Each call runs independently; HeyGen renders them in parallel. Do NOT serialize \
into one turn-per-video — fire all the tool calls in a single assistant turn.
- When the user describes a desired background in the same message ("with Burj Khalifa", \
"in front of a glass window showing the Dubai skyline", "with Palm Jumeirah behind me"), \
pass that description to create_promo_video as `background_prompt`. Expand vague hints into \
a vivid, cinematic, single-sentence prompt (e.g. user says "Burj Khalifa" → pass \
"Burj Khalifa visible through floor-to-ceiling windows of a luxury Dubai apartment, golden hour \
lighting, photorealistic, depth of field"). Do NOT mention the avatar/person in the prompt — \
describe the scene only, since the avatar is composited in front of it.
- If the agent asks "is my video ready?", "send me the link", "where's my video?", or any \
follow-up about a previously-requested video, call check_my_video_status. If completed, share \
the video_url verbatim so it can be downloaded or sent to clients. If still processing, tell \
them to try again in a minute.
"""

MAX_TOOL_ITERATIONS = 10  # higher than 5 to accommodate bulk video requests
HISTORY_WINDOW = 10  # last N messages passed back to the LLM


async def _get_or_create_conversation(
    db: AsyncSession, conversation_id: Optional[uuid.UUID], user_id: Optional[uuid.UUID], first_text: str
) -> AskAlphaConversation:
    if conversation_id is not None:
        existing = (await db.execute(
            select(AskAlphaConversation).where(AskAlphaConversation.id == conversation_id)
        )).scalar_one_or_none()
        if existing is not None:
            return existing

    now = datetime.now(timezone.utc)
    new_id = uuid.uuid4()
    title = (first_text[:57] + "...") if len(first_text) > 60 else first_text
    await db.execute(insert(AskAlphaConversation).values(
        id=new_id, user_id=user_id, title=title or "New chat",
        created_at=now, updated_at=now,
    ))
    await db.commit()
    return (await db.execute(
        select(AskAlphaConversation).where(AskAlphaConversation.id == new_id)
    )).scalar_one()


async def _load_history(db: AsyncSession, conversation_id: uuid.UUID) -> list[dict]:
    """Return the last HISTORY_WINDOW messages as Bedrock-shaped dicts, oldest first."""
    rows = (await db.execute(
        select(AskAlphaMessage)
        .where(AskAlphaMessage.conversation_id == conversation_id)
        .order_by(AskAlphaMessage.id.desc())
        .limit(HISTORY_WINDOW)
    )).scalars().all()
    rows = list(reversed(rows))
    return [{"role": r.role, "content": [{"text": r.content}]} for r in rows]


async def _insert_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    cards: Optional[list[dict]],
) -> AskAlphaMessage:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        insert(AskAlphaMessage)
        .values(conversation_id=conversation_id, role=role, content=content,
                cards=cards if cards else None, created_at=now)
        .returning(AskAlphaMessage.id)
    )
    msg_id = result.scalar_one()
    await db.execute(
        update(AskAlphaConversation)
        .where(AskAlphaConversation.id == conversation_id)
        .values(updated_at=now)
    )
    await db.commit()
    return (await db.execute(
        select(AskAlphaMessage).where(AskAlphaMessage.id == msg_id)
    )).scalar_one()


def _summarize_tool_result(name: str, result: dict) -> str:
    if not isinstance(result, dict):
        return repr(result)[:120]
    if "error" in result:
        return f"error: {result['error']}"
    if name in ("search_projects", "search_units"):
        return f"{result.get('count', 0)} projects"
    if name == "search_nearby_projects":
        return f"{result.get('count', 0)} projects near {result.get('anchor')!r}"
    if name == "get_project_details":
        return f"project id={result.get('id')} name={result.get('name')!r}"
    if name == "get_market_intelligence":
        if not result.get("found"):
            return "no market data"
        return f"market {result.get('matched_name')!r} rate/sqft={result.get('median_rate_aed_sqft_12m')} mom={result.get('rate_momentum_pct')}%"
    if name == "search_documents":
        return f"{result.get('count', 0)} chunks"
    if name == "create_promo_video":
        return f"video_id={result.get('video_id')} status={result.get('status')}"
    if name == "check_my_video_status":
        return f"video_id={result.get('video_id')} status={result.get('status')} url?={bool(result.get('video_url'))}"
    return repr(result)[:120]


def _build_cards(tool_calls: list[dict]) -> list[dict]:
    """Convert captured tool results into UI-renderable cards."""
    cards: list[dict] = []
    for call in tool_calls:
        name = call["name"]
        result = call["result"]
        if not isinstance(result, dict) or "error" in result:
            continue
        if name in ("search_projects", "search_units"):
            items = result.get("projects", [])
            if items:
                cards.append({
                    "type": "project_list",
                    "items": items[:5],
                    "has_more": bool(result.get("has_more")),
                    "next_offset": result.get("next_offset"),
                })
            else:
                suggestions = result.get("suggestions") or []
                if suggestions:
                    cards.append({
                        "type": "no_match_suggestions",
                        "query": result.get("query"),
                        "items": suggestions[:3],
                    })
        elif name == "get_project_details":
            if "id" in result:
                cards.append({"type": "project_detail", "project": result})
        elif name == "search_nearby_projects":
            items = result.get("projects", [])
            if items:
                cards.append({
                    "type": "project_list",
                    "items": items[:5],
                    "has_more": bool(result.get("has_more")),
                    "next_offset": result.get("next_offset"),
                })
        elif name == "get_market_intelligence":
            if result.get("found"):
                cards.append({"type": "market_card", "market": result})
        elif name == "search_documents":
            items = result.get("chunks", [])
            if items:
                cards.append({"type": "document_quotes", "items": items})
        elif name == "create_promo_video":
            cards.append({
                "type": "video_job",
                "video_id": result.get("video_id"),
                "status": result.get("status"),
                "project_id": result.get("project_id"),
                "project_name": result.get("project_name"),
            })
        elif name == "check_my_video_status":
            cards.append({
                "type": "video_status",
                "video_id": result.get("video_id"),
                "status": result.get("status"),
                "video_url": result.get("video_url"),
                "thumbnail_url": result.get("thumbnail_url"),
                "project_id": result.get("project_id"),
                "project_name": result.get("project_name"),
                "error_detail": result.get("error_detail"),
            })
    return cards


async def _run_tool_loop(db: AsyncSession, messages: list[dict], ctx: dict) -> tuple[str, list[dict]]:
    tool_config = registry.to_bedrock_config()
    captured: list[dict] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = bedrock.converse(
            modelId=settings.bedrock_model_id,
            system=[{"text": ASK_ALPHA_SYSTEM_PROMPT}],
            messages=messages,
            toolConfig=tool_config,
            inferenceConfig={"maxTokens": 1500, "temperature": 0.2},
        )
        stop_reason = response["stopReason"]
        assistant_msg = response["output"]["message"]
        messages.append(assistant_msg)

        if stop_reason == "end_turn":
            for block in assistant_msg["content"]:
                if "text" in block:
                    return block["text"], captured
            return "", captured

        if stop_reason == "tool_use":
            tool_result_blocks = []
            for block in assistant_msg["content"]:
                if "toolUse" not in block:
                    continue
                tu = block["toolUse"]
                tool = registry.get(tu["name"])
                if tool is None:
                    result = {"error": f"Unknown tool: {tu['name']}"}
                else:
                    try:
                        result = await tool.handler(db, tu.get("input", {}), ctx)
                    except Exception as e:
                        # If a tool query aborts the Postgres transaction, every subsequent
                        # statement on this session fails. Rollback so the assistant message
                        # insert (and any later tool calls) still go through.
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        result = {"error": f"Tool execution failed: {e}"}
                summary = _summarize_tool_result(tu["name"], result)
                log.info("tool %s input=%s → %s", tu["name"], tu.get("input", {}), summary)
                captured.append({"name": tu["name"], "input": tu.get("input", {}), "result": result})
                tool_result_blocks.append({
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"json": result}],
                    }
                })
            messages.append({"role": "user", "content": tool_result_blocks})
            continue

        return "I couldn't complete that request.", captured

    return "I'm having trouble — too many tool calls in a row. Try rephrasing.", captured


async def chat_turn(
    db: AsyncSession,
    user_message: str,
    conversation_id: Optional[uuid.UUID] = None,
    user_id: Optional[uuid.UUID] = None,
    channel: str = "website",
    telegram_chat_id: Optional[int] = None,
) -> dict:
    """Persist a chat turn and return the assistant reply + cards.

    Returns: {"reply", "conversation_id", "message_id", "cards"}.
    """
    conv = await _get_or_create_conversation(db, conversation_id, user_id, first_text=user_message)
    history = await _load_history(db, conv.id)
    await _insert_message(db, conv.id, "user", user_message, cards=None)

    messages = history + [{"role": "user", "content": [{"text": user_message}]}]
    ctx = {
        "user_id": user_id,
        "channel": channel,
        "conversation_id": conv.id,
        "telegram_chat_id": telegram_chat_id,
    }
    reply_text, tool_calls = await _run_tool_loop(db, messages, ctx)
    cards = _build_cards(tool_calls)

    asst = await _insert_message(db, conv.id, "assistant", reply_text or "", cards=cards or None)
    return {
        "reply": reply_text or "",
        "conversation_id": conv.id,
        "message_id": asst.id,
        "cards": cards,
    }
