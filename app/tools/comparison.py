"""generate_comparison_pdf — the branded "Side by Side" Property Comparison PDF.

Triggered by "compare these as a PDF", "comparison sheet", "side by side", etc.
Takes 2–3 projects, computes each one's metrics (price/sqft, type, beds, size)
and the Alpha Score verdict from the same investment signals as analyze_investment,
ranks them (BEST / MOST / LARGEST / HIGHEST badges), renders the single-page
A4 sheet to PDF, uploads it to S3, and — on Telegram — sends the file into the chat.

Financial figures the agent states in chat (yield, appreciation) override the
computed defaults; ones we can neither compute nor were given simply don't appear.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.brochures import render as brochure_render
from app.brochures import storage as brochure_storage
from app.brochures.data import resolve_project
from app.comparisons.data import OVERRIDE_KEYS, build_comparison_context
from app.core.profiles import get_profile, is_agent
from app.tools.brochures import ASSETS_BUCKET, _send_telegram_document
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.comparisons")


async def generate_comparison_pdf_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    user_id = ctx.get("user_id")
    if user_id is None:
        return {"error": "Sign in required. Comparison PDFs are only for our agents."}
    profile = await get_profile(db, user_id)
    if not is_agent(profile):
        return {"error": "This feature is only available to agents."}

    # Accept either a rich `properties` array (with per-project overrides) or a
    # bare `project_ids` list.
    props = args.get("properties")
    if not props:
        props = [{"project_id": i} for i in (args.get("project_ids") or [])]
    if not isinstance(props, list) or len(props) < 2:
        return {"error": "Pick 2 or 3 projects to compare."}
    props = props[:3]

    projects, overrides, missing = [], [], []
    for item in props:
        if not isinstance(item, dict):
            continue
        p = await resolve_project(db, item.get("project_id"), item.get("project_name"))
        if p is None:
            missing.append(item.get("project_name") or item.get("project_id"))
            continue
        projects.append(p)
        overrides.append({k: item[k] for k in OVERRIDE_KEYS if item.get(k) is not None})

    if missing:
        return {"error": f"Couldn't find these project(s): {missing}. Search projects first "
                         "to confirm the name or ID, then try again."}

    # De-dupe (comparing a project against itself isn't useful).
    seen, uniq_p, uniq_o = set(), [], []
    for p, o in zip(projects, overrides):
        if p.id in seen:
            continue
        seen.add(p.id)
        uniq_p.append(p)
        uniq_o.append(o)
    if len(uniq_p) < 2:
        return {"error": "Need at least 2 different projects to compare."}
    projects, overrides = uniq_p, uniq_o

    try:
        context, image_files = await build_comparison_context(db, projects, overrides)
        pdf_bytes = await brochure_render.render_comparison_pdf(context, image_files)
    except Exception as e:
        log.exception("comparison generation failed for %s", [p.id for p in projects])
        return {"error": f"Comparison generation failed: {e}"}

    base = "comparison-" + "-vs-".join(brochure_storage.slugify(p.name) for p in projects)
    s3_key, pdf_url = None, None
    try:
        s3_key, pdf_url = await brochure_storage.upload_pdf(pdf_bytes, base, ASSETS_BUCKET)
    except Exception as e:
        log.error("comparison S3 upload failed (continuing with Telegram only): %s", e)

    filename = f"{brochure_storage.slugify(base)}.pdf"
    delivered = False
    tg_chat_id = ctx.get("telegram_chat_id")
    if tg_chat_id:
        delivered = await _send_telegram_document(
            int(tg_chat_id), pdf_bytes, filename,
            caption="📊 Property comparison — " + " vs ".join(p.name for p in projects),
        )

    if not delivered and not pdf_url:
        if tg_chat_id:
            return {"error": "Comparison was rendered but couldn't be delivered: the S3 download "
                             "link needs an admin to grant s3:PutObject on the assets bucket, and "
                             "Telegram delivery failed this time. Please try again."}
        return {"error": "Comparison was rendered but there's no download link yet: it needs an "
                         "admin to grant s3:PutObject on the assets bucket. Ask an admin to "
                         "enable it — retrying won't help until then."}

    alpha = [c["disp"] for c in context["alpha"]["cells"]]
    log.info("comparison ready ids=%s size=%dKB url=%s telegram=%s",
             [p.id for p in projects], len(pdf_bytes) // 1024, s3_key, delivered)
    result = {
        "status": "completed",
        "project_ids": [p.id for p in projects],
        "project_names": [p.name for p in projects],
        "count": len(projects),
        "alpha_scores": alpha,
        "pdf_url": pdf_url,
        "filename": filename,
        "sent_to_telegram": delivered,
    }
    if pdf_url:
        result["url_expires"] = "7 days"
    else:
        result["note"] = ("No download link — S3 upload unavailable. The PDF was "
                          "delivered via Telegram only.")
    return result


_PROP_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "project_id": {"type": "integer", "description": "Project ID (preferred — from search_projects)."},
        "project_name": {"type": "string", "description": "Project name if the ID is unknown."},
        "net_yield_pct": {"type": "number", "description": "Rental yield % for THIS project, ONLY if the agent stated it (else a market-typical estimate is shown)."},
        "annual_appreciation_pct": {"type": "number", "description": "Annual appreciation % for THIS project, ONLY if the agent stated it (else the row is omitted)."},
        "y5_projected_value_aed": {"type": "number", "description": "Year-5 projected value in AED, ONLY if the agent stated it (else computed from appreciation when given)."},
        "price_per_sqft_aed": {"type": "number", "description": "Override price/sqft in AED, ONLY if the agent stated it."},
        "alpha_score": {"type": "number", "description": "Override Alpha Score 0–100, ONLY if the agent stated it (else computed)."},
    },
}

registry.register(Tool(
    name="generate_comparison_pdf",
    description=(
        "Generate the Allegiance-branded 'Side by Side' Property Comparison PDF for 2–3 projects: a "
        "single-page sheet ranking them on price/sqft, property type, bedrooms, built-up area, rental "
        "yield, and an Alpha Score verdict (0–100), with BEST / MOST / LARGEST / HIGHEST badges on the "
        "winning cells. Use when an agent asks to compare projects as a document — 'comparison PDF', "
        "'compare these side by side', 'comparison sheet', 'make a comparison of X and Y', etc. Agents "
        "only. Takes ~20–40 seconds. Returns a download URL; on Telegram the PDF is also sent into the "
        "chat. Identify each project by project_id (preferred) or project_name. Rental yield defaults to "
        "a market-typical ESTIMATE and Alpha Score is computed from real signals; annual appreciation and "
        "5-year value only appear if the agent states the appreciation. Pass agent-stated figures in the "
        "matching per-project fields — NEVER invent them yourself."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "properties": {
                "type": "array",
                "items": _PROP_ITEM_SCHEMA,
                "minItems": 2,
                "maxItems": 3,
                "description": "The 2–3 projects to compare, each with its ID/name and any agent-stated overrides.",
            },
            "project_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Simpler alternative: 2–3 project IDs (no overrides).",
            },
        },
    },
    handler=generate_comparison_pdf_handler,
))
