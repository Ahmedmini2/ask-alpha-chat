"""Agentic retrieval over the brochure vector store (document_chunks).

Upgrades the single-shot search_documents into a small, bounded agentic flow:
decompose the question into facet sub-queries, retrieve for each, then fuse and
dedupe the results with provenance. This lifts recall on broad questions ("is X a
good investment?") where one embedding misses facets like payment terms or amenities.

Deterministic by design (no LLM call needed to pick facets) so it's fast and
reliable; the orchestrator's model does the final synthesis from the cited chunks.
"""
import asyncio
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.embeddings import embed_text

MAX_SUBQUERIES = 5
TOP_K_PER_SUBQUERY = 5
MAX_RESULTS = 8

# Facet expansions for investment/value questions — each becomes its own retrieval.
_INVESTMENT_FACETS = [
    "payment plan, post-handover terms, down payment and installments",
    "amenities, facilities, finishes and what's included",
    "location, connectivity, nearby landmarks and transport",
    "price, value for money, ROI and rental potential",
]
_INVESTMENT_TRIGGERS = ("invest", "worth", "roi", "good buy", "good value", "should i buy", "return")


def _decompose(query: str) -> list[str]:
    q = query.strip()
    subs = [q]
    low = q.lower()
    if any(t in low for t in _INVESTMENT_TRIGGERS):
        subs.extend(_INVESTMENT_FACETS)
    return subs[:MAX_SUBQUERIES]


async def _retrieve(db: AsyncSession, vec: list[float], project_id: Optional[int], k: int) -> list[dict]:
    sql = """
        SELECT c.id, c.project_id, c.asset_id, c.source_kind, c.chunk_index, c.content,
               1 - (c.embedding <=> CAST(:qv AS vector)) AS similarity,
               p.name AS project_name
        FROM document_chunks c
        LEFT JOIN projects p ON p.id = c.project_id
        WHERE c.embedding IS NOT NULL
    """
    params: dict = {"qv": str(vec), "k": k}
    if project_id is not None:
        sql += " AND c.project_id = :pid"
        params["pid"] = int(project_id)
    sql += " ORDER BY c.embedding <=> CAST(:qv AS vector) LIMIT :k"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def run_agentic_search(db: AsyncSession, query: str, project_id: Optional[int] = None) -> dict:
    sub_queries = _decompose(query)

    # Embed all sub-queries off the event loop, then retrieve concurrently.
    vecs = await asyncio.gather(*[asyncio.to_thread(embed_text, sq) for sq in sub_queries])
    retrievals = await asyncio.gather(*[
        _retrieve(db, vec, project_id, TOP_K_PER_SUBQUERY) for vec in vecs
    ])

    # Fuse: dedupe by chunk id, keep the best similarity and which facet found it.
    best: dict[int, dict] = {}
    for sq, rows in zip(sub_queries, retrievals):
        for r in rows:
            cid = r["id"]
            sim = round(float(r["similarity"]), 3)
            if cid not in best or sim > best[cid]["similarity"]:
                best[cid] = {
                    "chunk_id": cid,
                    "project_id": r["project_id"],
                    "project_name": r["project_name"],
                    "source_kind": r["source_kind"],
                    "chunk_index": r["chunk_index"],
                    "similarity": sim,
                    "matched_facet": sq,
                    "content": r["content"],
                }
    fused = sorted(best.values(), key=lambda x: x["similarity"], reverse=True)[:MAX_RESULTS]

    return {
        "count": len(fused),
        "query": query,
        "sub_queries": sub_queries,
        "chunks": fused,
    }
