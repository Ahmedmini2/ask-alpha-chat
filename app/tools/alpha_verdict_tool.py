"""get_alpha_verdict — the website-parity Alpha Verdict for a project: BUY/WATCH/SKIP, the
conviction score, the 4 pillars (yield vs community, price/sqft vs community, yield vs Dubai,
risk), and the Numbers at a Glance. Reads the shared project_alpha_verdict store (recomputing if
stale); numbers come from real Property Monitor community stats where available, static model
otherwise. This is the single source both ask-alpha and the website show."""
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import alpha_verdict as av
from app.tools.investment_metrics import _f, _resolve_project
from app.tools.registry import Tool, registry


def _shape(v: dict, project=None) -> dict:
    return {
        "found": True,
        "project_id": project.id if project else None,
        "project_name": project.name if project else None,
        "verdict": v["verdict"],
        "conviction": round(float(v["conviction"])),     # display value (e.g. 75)
        "pillars": v["pillars"],                          # yield / comp / thesis / risk (0..100)
        "numbers": v["numbers"],                          # net yield, area rent, appreciation, Y5, ppsf, vs-area
        "community": v.get("community_label"),
        "used_fallback": v.get("used_fallback"),
        "stats_source": v.get("stats_source"),
        "basis": v.get("basis"),
    }


async def get_alpha_verdict_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    project = None
    if args.get("project_id") is not None or args.get("project_name"):
        project = await _resolve_project(db, args)
        if project is None and not (args.get("price") and args.get("community")):
            return {"found": False, "message": "We don't have that project in our system yet."}

    if project is not None:
        v = await av.get_or_compute_verdict(db, project.id)
        if not v:
            return {"found": False, "project_id": project.id, "project_name": project.name,
                    "message": "That project has no priced units yet, so I can't score a verdict."}
        return _shape(v, project)

    # Hypothetical: bare price + community.
    price = _f(args.get("price"))
    community = args.get("community")
    if price and community:
        v = av.compute_alpha_verdict(price=price, community=community,
                                     beds=_f(args.get("beds")), sqft=_f(args.get("sqft")))
        if v:
            return _shape(v)
    return {"found": False, "message": "Give me a project (name/id) or a price + community."}


registry.register(Tool(
    name="get_alpha_verdict",
    description=(
        "Get Allegiance's ALPHA VERDICT for a project — the SAME verdict the website shows: "
        "BUY / WATCH / SKIP, the conviction score (0-100), the 4 pillar scores (Yield vs Community, "
        "Price/sqft vs Community, Yield vs Dubai, Risk & Safety) and the Numbers at a Glance (net "
        "yield, area average rent return, annual appreciation, 5-year projected value, price/sqft, "
        "premium-vs-area). Call this whenever the user asks 'is X a good buy / worth it / a good "
        "investment', 'what's the verdict / conviction / score', or 'the numbers on X'. Identify the "
        "project by project_id (preferred) or project_name; or pass price + community for a "
        "hypothetical. Lead with the verdict + conviction, then the standout number. To rank/compare "
        "many projects by verdict, use search_projects/search_units with sort='conviction' instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Numeric project ID (preferred); from search_projects."},
            "project_name": {"type": "string", "description": "Project name if the ID isn't known (fuzzy-matched)."},
            "price": {"type": "number", "description": "AED price for a hypothetical (no project)."},
            "beds": {"type": "number", "description": "Bedrooms (optional, refines the verdict)."},
            "sqft": {"type": "number", "description": "Unit size in sqft (optional)."},
            "community": {"type": "string", "description": "Community/district for a hypothetical, e.g. 'Dubai Marina'."},
        },
        "required": [],
    },
    handler=get_alpha_verdict_handler,
))
