from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.embeddings import embed_text
from app.tools.registry import Tool, registry


async def search_documents_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = args.get("query")
    if not query:
        return {"error": "query is required"}
    project_id = args.get("project_id")
    source_kind = args.get("source_kind")
    limit = min(int(args.get("limit", 5)), 15)

    vec = embed_text(query)
    sql = """
        SELECT c.id, c.project_id, c.asset_id, c.source_kind, c.chunk_index,
               c.content,
               1 - (c.embedding <=> CAST(:qv AS vector)) AS similarity,
               p.name AS project_name
        FROM rag_chunks c
        LEFT JOIN projects p ON p.id = c.project_id
        WHERE c.embedding IS NOT NULL
    """
    params: dict = {"qv": str(vec)}
    if project_id is not None:
        sql += " AND c.project_id = :pid"
        params["pid"] = int(project_id)
    if source_kind:
        sql += " AND c.source_kind = :sk"
        params["sk"] = source_kind
    sql += " ORDER BY c.embedding <=> CAST(:qv AS vector) LIMIT :lim"
    params["lim"] = limit

    rows = (await db.execute(text(sql), params)).mappings().all()
    return {
        "count": len(rows),
        "chunks": [
            {
                "project_id": r["project_id"],
                "project_name": r["project_name"],
                "asset_id": r["asset_id"],
                "source_kind": r["source_kind"],
                "chunk_index": r["chunk_index"],
                "similarity": round(float(r["similarity"]), 3),
                "content": r["content"],
            }
            for r in rows
        ],
    }


registry.register(Tool(
    name="search_documents",
    description=(
        "Semantic search over project marketing documents (brochures, payment plans, etc.) "
        "that have been ingested into the vector store. Use this when the user asks about "
        "anything that lives in the prose of marketing materials — payment plans, amenity "
        "details, finishings, location narratives, ROI/investment language — and not in "
        "structured columns. Combine with search_projects/get_project_details when the "
        "question names a specific project."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query, e.g. 'payment plan after handover' or 'private gym amenities'",
            },
            "project_id": {
                "type": "integer",
                "description": "Optional: restrict search to one project's documents",
            },
            "source_kind": {
                "type": "string",
                "description": "Optional filter: 'brochure', 'payment_plan', 'floor_plan', etc.",
            },
            "limit": {
                "type": "integer",
                "description": "Max chunks to return (default 5, max 15)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
    handler=search_documents_handler,
))
