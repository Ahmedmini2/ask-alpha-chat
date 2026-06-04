"""Agentic RAG tool — multi-query, facet-decomposed retrieval over brochures."""
from sqlalchemy.ext.asyncio import AsyncSession
from app.rag.agentic import run_agentic_search
from app.tools.registry import Tool, registry


async def agentic_search_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    project_id = args.get("project_id")
    return await run_agentic_search(db, query, int(project_id) if project_id is not None else None)


registry.register(Tool(
    name="agentic_search",
    description=(
        "Deep document search over project marketing materials (brochures, payment plans, "
        "amenities, location narratives). Unlike search_documents, this decomposes a broad "
        "question into facets (payment terms, amenities, location, price/value) and retrieves for "
        "each, then fuses the best-matching passages with citations. Prefer this for open-ended or "
        "multi-part questions like 'is this project a good investment' or 'tell me everything about "
        "X's payment plan and amenities'. Pass project_id when the question is about one project."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The user's question in natural language."},
            "project_id": {"type": "integer", "description": "Restrict to one project's documents (recommended when known)."},
        },
        "required": ["query"],
    },
    handler=agentic_search_handler,
))
