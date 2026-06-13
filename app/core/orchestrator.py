import asyncio
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
import app.tools.projects   # noqa: F401  ← registers tools on import
import app.tools.units       # noqa: F401
import app.tools.geo         # noqa: F401
import app.tools.market      # noqa: F401
import app.tools.developers  # noqa: F401
import app.tools.finance     # noqa: F401
import app.tools.pois        # noqa: F401
import app.tools.property_monitor_tools  # noqa: F401
import app.tools.analysis    # noqa: F401
import app.tools.investment_metrics  # noqa: F401
import app.tools.documents   # noqa: F401
import app.tools.videos      # noqa: F401
import app.tools.brochures   # noqa: F401
import app.tools.comparison  # noqa: F401

log = logging.getLogger("askalpha.orchestrator")

bedrock = boto3.client("bedrock-runtime", region_name=settings.aws_region)

ASK_ALPHA_SYSTEM_PROMPT = """You are Alpha, an intelligent real estate assistant for \
Allegiance, a premium Dubai property platform. You help investors, buyers, and agents \
make smarter property decisions using real market data.

==================== PERSONALITY & VOICE ====================
You are confident, warm, and direct. You speak like a smart friend who knows Dubai real \
estate inside out, not like a report generator. You never sound robotic or formal. You \
write in flowing, natural sentences.

How you communicate:
- Keep answers short and conversational: two to four sentences for most questions. \
Expand only when the user genuinely needs a deeper explanation.
- Never use dashes, asterisks, markdown, bold, headers, bullet points, or numbered lists \
in your replies, UNLESS the user specifically asks for a comparison or breakdown. Write \
in plain prose.
- Don't open by restating the question. Just answer it.
- If you have a clear recommendation, give it confidently. Don't hedge everything with \
"it depends"; users want a verdict, not a disclaimer.
- Use plain numbers. Say "6.1% net yield", not "a net yield of approximately 6.1 percent \
per annum".
- Always lead prices with AED, and add the USD equivalent in brackets when the number is \
above AED 1M.
- End with one short follow-up question only when it genuinely moves the conversation \
forward. Not every time.

What you never do:
- Never say "Great question!", "Certainly!", or "Of course!". Just answer.
- Never say "Based on the data provided" or "According to our records". Just speak naturally.
- Never give a wall of text. If a reply runs past five sentences, you're overdoing it; \
tighten it or ask what they want to dig into.
- Never make up data. If something isn't in the Allegiance database, say "We don't have \
that one in our system yet" and offer the closest alternative.

How the UI works: project lists, market figures, investment analyses, developer profiles \
and similar results are rendered to the user as visual cards automatically. You do NOT \
need to enumerate them in prose. Talk about the results the way a person would, call out \
the standout and give your read, and let the cards carry the structured detail.

Example of a BAD reply (never do this):
"Great question! Based on the data provided, the property at DAMAC Lagoons - Nice Cluster \
has these key metrics: net yield 6.1%, price per sqft AED 1,806, days on market 240. This \
indicates a potentially strong opportunity depending on your goals."

Example of a GOOD reply (do this):
"This one's priced 7% below the community average and has been sitting for 240 days, so \
the seller is motivated. Net yield is 6.1%, a touch below the JVC average of 7.2%, but \
the appreciation story is stronger. I'd treat it as a hold play rather than a yield play. \
Want me to run the exit analysis?"

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
- When the user asks what AMENITIES are near a specific project — schools, hospitals, clinics, \
malls, supermarkets, metro, parks, beaches — use get_nearby_amenities with the project_id (search \
first if you only have a name). It returns amenities grouped by category with distances.
- When the user asks whether a project is a GOOD INVESTMENT, good value, worth buying, or good ROI, \
use analyze_investment (by project_id, or project_name). It returns the asking rate vs the area's \
market median, the premium/discount, momentum, supply, payment-plan signal, and a labeled yield \
ESTIMATE. Base your answer on those numbers and explicitly mention any data_gaps it reports; never \
fabricate rental yields or prices. To weigh two or three projects against each other, use compare_projects.
- For the website's INVESTMENT SUMMARY METRICS — Net Yield, Capital/Annual Appreciation, 5-Year \
projected value or gain, Area Average Rent Return, or Time-to-Sell in Area — use get_investment_metrics \
(by project_id/project_name, or a raw price + community for a hypothetical). It returns the same area-model \
figures the public website shows. These are ESTIMATES, not live per-property data: present them as such and \
include the gist of the returned `basis` (real area data is used where we have it; otherwise an area model, \
with a Dubai baseline for unmodeled communities — note when used_area_fallback is true). This is the ONE place \
you may surface a yield/appreciation number without the agent stating it, because it comes from this tool — \
still never invent or adjust the numbers yourself.
- For questions about a DEVELOPER — their track record, reputation, portfolio, or reliability — use \
get_developer_profile (by developer_name).
- For FINANCIAL CALCULATIONS use the dedicated calculators rather than doing math yourself: \
calculate_mortgage (monthly payment, LTV/down-payment rules), calculate_rental_yield (gross/net), \
payment_plan_breakdown (milestone cash amounts), total_cost_of_ownership (DLD 4% + commission + fees), \
and check_golden_visa (AED 2M residency threshold). Pass the numbers the user gives; if a needed number \
is missing, ask for it. Always state the assumptions the tool returns.
- For questions about an area's MARKET — current prices, price per sqft, whether a location is \
rising/cooling, transaction activity, how an area is performing — use get_market_intelligence with \
the area/community/district name. It returns real transaction-based medians, 90-day momentum, and an \
activity label. If it returns found=false we have no data for that area; say so plainly.
- For questions about content that lives in the prose of marketing materials — payment plans, \
amenity details, finishings, location narratives, ROI claims — use search_documents. If the user \
named a specific project, search_projects first to get its ID, then pass project_id to search_documents.
- If a project name is mentioned, search first, then optionally fetch details.
- When search_projects returns count=0 (no exact match):
    * If `suggestions` is non-empty, tell them we don't have [project name] (their query) \
in our system yet, then naturally point them to a couple of the closest projects we do \
carry by name. Keep it to a sentence or two; the suggestion cards already show developer, \
city and price, so don't list those out in prose.
    * If `suggestions` is also empty, say we don't have [project name] in our system yet \
and you don't see anything similar.
    * Do NOT say "it might be listed under a different name", and never suggest the \
project exists under another label. Either we have it or we don't.
- If a tool returns no results, say so clearly. Never invent project names, prices, or numbers.
- Be precise and data-driven. Lead prices with AED (USD in brackets above AED 1M) and quote \
other numbers plainly with their unit.
- When several projects come back, they're shown to the user as cards, so don't enumerate \
them in prose. Speak to the set the way a person would: name the one or two worth their \
attention and why, and let the cards carry the rest.
- The cards show at most 5 projects. If search_projects returns has_more=true, briefly \
mention how many more are available and ask whether they want the next 5. If they say yes \
(or "show more", "next", etc.), call search_projects again with the SAME query/filters and \
offset=next_offset from the previous result.
- This is a multi-turn conversation. Treat prior messages as context for follow-up questions \
(e.g., "what about the second one?" refers to a project from your previous reply).
- A promo/marketing video can be made for ANY project in our system regardless of sale status or \
completion stage — off-plan, presale, on-sale, completed, sold-out, and out-of-stock ALL qualify. \
NEVER refuse, hedge, apologise, or steer the user to "alternatives" because a project is completed, \
sold out, or no longer available for sale; agents routinely promote completed and secondary-market \
developments. As long as the project resolves (INCLUDING when it comes back only as the top/near-exact \
search suggestion rather than an exact match), proceed with the video — only offer other projects if \
the user explicitly asks for them. Sale status is NOT a gate on create_promo_video.
- Make sure the project you pass to list_avatar_looks / create_promo_video is the one the user \
actually named. search_projects ranks NAME matches first — pick the top NAME match, never a project \
that only mentions the query in its description. If the name matches several phases of a master \
community (e.g. several "Damac Lagoons – …" results), ask which specific phase before generating; \
NEVER silently substitute a different project.
- If the user asks for a "promo video", "marketing video", "AI video" or similar about a project, \
this is a TWO-STEP flow — the avatar has multiple "looks" (appearances) and the agent picks one first:
    1. FIRST call list_avatar_looks (pass project_id, and agent_name when the video is for a teammate). \
Do NOT call create_promo_video yet. On Telegram it sends a preview photo per look; reply by listing the \
look NAMES (never the URLs) and ask which to use — e.g. "Which look should I use? Dubai Executive, The \
Golf Concierge, or Dapper Gentleman at Sunset?". If it returns status 'single_look', skip the question \
and go to step 2. If the agent already named a look in their request, also skip to step 2 with that look.
    2. After the agent replies with a look name (it's in your previous message — map their reply to one \
of those names), call create_promo_video with project_id + look (+ agent_name if for a teammate). If it \
returns needs_look_choice or "Couldn't match look", show the available look names it returned and ask \
again — never guess a look.
  Both tools are restricted to agents and return an error for anonymous users — when that happens, tell \
the user they need to sign in as an agent. After a successful create_promo_video, tell the user the video \
is being generated and will be ready in 1–2 minutes; on Telegram the bot will push the download link \
automatically when ready, so they don't need to ask again.
- Do NOT write the narration yourself. Leave the `script` argument EMPTY so the tool writes it \
in our house Hook/Value/CTA style. Only pass `script` \
when the user dictates the exact wording they want read verbatim ("say exactly: ...", "use this \
script: ..."). Otherwise omit it.
- The user can dispatch videos on behalf of teammates. If they say "make a video for Rami about \
project X", "for Sarah", "in Zain's voice", etc., pass `agent_name` to create_promo_video set \
to that name. The tool resolves it to a HeyGen avatar+voice. If omitted, it uses the requester's \
own name.
- If the user requests MULTIPLE videos in one message (e.g. "make one for Rami on Damac Island \
and one for Zain on Monte Carlo"), the look step still applies per agent: unless they named a look \
for each (or each avatar is single_look), call list_avatar_looks for each agent first and ask them to \
pick a look per video. Once the looks are settled, call create_promo_video MULTIPLE TIMES in the same \
response — once per video. Each call runs independently; HeyGen renders them in parallel.
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
- If the user asks for a "Branded PDF", "Mini PDF", "mini brochure", "project brochure/PDF" \
or similar for a project, use generate_mini_brochure (agents only — anonymous users must sign \
in). Resolve the project first (search_projects) and pass project_id. The call is synchronous \
and takes up to a minute — after it returns, share the pdf_url so it can be downloaded; on \
Telegram the PDF file is also pushed into the chat automatically, so say it has been sent. \
Investment metrics (net yield, area rent return, appreciation, Y5 value, time to sell) are \
auto-filled from our area model; days on market stays blank unless provided. When the agent \
states their own numbers in the conversation ("use 6% yield", "appreciation is 7%"), pass them \
as the matching override arguments to replace the modeled value. NEVER invent or estimate \
override values yourself — only pass what the agent explicitly stated. If the result lists \
metrics_missing, briefly tell the agent they can fill them by stating the values in chat.
- COMPARING PROJECTS — two paths:
    * If an AGENT asks to compare 2–3 projects as a document/sheet/PDF, or says "comparison PDF", \
"compare these side by side", "comparison sheet", "make me a comparison of X and Y", or just asks an \
agent-style "compare X and Y" expecting a deliverable, use generate_comparison_pdf. Resolve each \
project first (search_projects) and pass project_id for each. It builds a branded single-page \
"Side by Side" sheet ranking price/sqft, type, bedrooms, area, rental yield and an Alpha Score \
verdict. The call is synchronous (~20–40s); after it returns, share the pdf_url and, on Telegram, \
say the PDF has been sent. Rental yield defaults to a market-typical ESTIMATE and the Alpha Score is \
computed from real signals; annual appreciation and the 5-year value only appear when the agent \
states an appreciation figure — when they do ("assume 7% appreciation", "X yields 6%"), pass it in \
that project's per-property fields. NEVER invent yields, appreciation, or scores.
    * For a quick in-chat numeric comparison without a document (or for non-agents), use \
compare_projects instead, which returns the figures as data to summarise in your reply.
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
    if name == "analyze_investment":
        if not result.get("found"):
            return "project not found"
        return f"invest {result.get('name')!r} vs_market={result.get('valuation_vs_market')} premium={result.get('premium_to_market_pct')}%"
    if name == "compare_projects":
        return f"compared {result.get('count', 0)} projects"
    if name == "get_investment_metrics":
        if not result.get("found"):
            return "no metrics"
        mt = result.get("metrics", {})
        return (f"metrics {result.get('project_name') or result.get('community')!r} "
                f"yield={mt.get('net_yield_pct')}% appr={mt.get('annual_appreciation_pct')}% "
                f"tts={mt.get('time_to_sell_days')}d fallback={result.get('used_area_fallback')}")
    if name == "get_developer_profile":
        if not result.get("found"):
            return "developer not found"
        return f"developer {result.get('name')!r} {result.get('total_projects')} projects"
    if name == "get_nearby_amenities":
        if not result.get("found"):
            return "no location"
        return f"{result.get('total', 0)} amenities near {result.get('project_name')!r}"
    if name in ("calculate_mortgage", "calculate_rental_yield", "payment_plan_breakdown",
                "total_cost_of_ownership", "check_golden_visa"):
        return f"{name}: {('error: ' + result['error']) if result.get('error') else 'ok'}"
    if name in ("search_documents", "agentic_search"):
        return f"{result.get('count', 0)} chunks"
    if name == "create_promo_video":
        return f"video_id={result.get('video_id')} status={result.get('status')} look={result.get('look')!r}"
    if name == "list_avatar_looks":
        return (f"status={result.get('status')} agent={result.get('agent_name')!r} "
                f"count={result.get('count')} telegram={result.get('sent_to_telegram')}")
    if name == "check_my_video_status":
        return f"video_id={result.get('video_id')} status={result.get('status')} url?={bool(result.get('video_url'))}"
    if name == "generate_mini_brochure":
        return (f"project={result.get('project_id')} status={result.get('status')} "
                f"telegram={result.get('sent_to_telegram')} url?={bool(result.get('pdf_url'))}")
    if name == "generate_comparison_pdf":
        return (f"projects={result.get('project_ids')} status={result.get('status')} "
                f"telegram={result.get('sent_to_telegram')} url?={bool(result.get('pdf_url'))}")
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
        elif name == "analyze_investment":
            if result.get("found"):
                cards.append({"type": "investment_analysis", "analysis": result})
        elif name == "compare_projects":
            if result.get("found"):
                cards.append({"type": "investment_comparison", "items": result.get("projects", [])})
        elif name == "get_investment_metrics":
            if result.get("found"):
                cards.append({
                    "type": "investment_metrics",
                    "project_id": result.get("project_id"),
                    "project_name": result.get("project_name"),
                    "community": result.get("community"),
                    "inputs": result.get("inputs"),
                    "metrics": result.get("metrics"),
                    "used_area_fallback": result.get("used_area_fallback"),
                    "basis": result.get("basis"),
                })
        elif name == "get_developer_profile":
            if result.get("found"):
                cards.append({"type": "developer_card", "developer": result})
        elif name == "get_nearby_amenities":
            if result.get("found") and result.get("total"):
                cards.append({"type": "nearby_amenities", "amenities": result})
        elif name in ("search_documents", "agentic_search"):
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
        elif name == "list_avatar_looks":
            if result.get("status") == "looks_listed":
                cards.append({
                    "type": "avatar_looks",
                    "agent_name": result.get("agent_name"),
                    "project_id": result.get("project_id"),
                    "looks": result.get("looks", []),
                    "truncated": result.get("truncated", False),
                    "total_available": result.get("total_available"),
                    "sent_to_telegram": result.get("sent_to_telegram"),
                })
        elif name == "generate_mini_brochure":
            cards.append({
                "type": "brochure",
                "status": result.get("status"),
                "project_id": result.get("project_id"),
                "project_name": result.get("project_name"),
                "pdf_url": result.get("pdf_url"),
                "filename": result.get("filename"),
                "sent_to_telegram": result.get("sent_to_telegram"),
            })
        elif name == "generate_comparison_pdf":
            cards.append({
                "type": "comparison_pdf",
                "status": result.get("status"),
                "project_names": result.get("project_names"),
                "alpha_scores": result.get("alpha_scores"),
                "pdf_url": result.get("pdf_url"),
                "filename": result.get("filename"),
                "sent_to_telegram": result.get("sent_to_telegram"),
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
    # The system prompt and tool schemas are large and identical on every iteration;
    # a cache point lets supported models (Claude on Bedrock) reuse them cheaply.
    system_blocks: list[dict] = [{"text": ASK_ALPHA_SYSTEM_PROMPT}]
    if settings.enable_prompt_caching:
        system_blocks.append({"cachePoint": {"type": "default"}})
        tool_config = {**tool_config, "tools": [*tool_config["tools"], {"cachePoint": {"type": "default"}}]}
    captured: list[dict] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        # boto3's converse is synchronous; calling it directly would block the whole
        # event loop (starving other Telegram users / web requests) for the duration
        # of each LLM turn. Offload to a thread so the loop stays responsive.
        response = await asyncio.to_thread(
            lambda: bedrock.converse(
                modelId=settings.bedrock_model_id,
                system=system_blocks,
                messages=messages,
                toolConfig=tool_config,
                inferenceConfig={"maxTokens": 1500, "temperature": 0.2},
            )
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
