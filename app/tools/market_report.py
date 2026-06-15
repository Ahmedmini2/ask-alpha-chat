"""generate_market_report — the branded Dubai Market Report PDF.

Triggered by "Dubai market report", "market report PDF", "market overview PDF", etc. Builds a
Dubai-wide snapshot from REAL data (Property Monitor price index + per-community stats + our
Alpha Verdict store), renders the Allegiance-branded 2-page A4 report with headless Chromium,
uploads it to S3 for a shareable link, and — on Telegram — sends the file straight into the chat.
Unlike the brochure/comparison sheets this is generic market intel (no per-project input), so it's
available to any signed-in user.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.brochures import render as brochure_render
from app.brochures import storage as brochure_storage
from app.reports import market_data
from app.tools.brochures import ASSETS_BUCKET, _send_telegram_document
from app.tools.registry import Tool, registry

log = logging.getLogger("askalpha.reports")


async def generate_market_report_handler(db: AsyncSession, args: dict, ctx: dict) -> dict:
    try:
        context, image_files = await market_data.build_market_context(db)
        pdf_bytes = await brochure_render.render_market_report_pdf(context, image_files)
    except Exception as e:
        log.exception("market report generation failed")
        return {"error": f"Market report generation failed: {e}"}

    # Upload for a shareable link; Telegram delivery below works even if this fails
    # (e.g. missing s3:PutObject), so don't abort on it.
    s3_key, pdf_url = None, None
    try:
        s3_key, pdf_url = await brochure_storage.upload_pdf(pdf_bytes, "dubai-market-report", ASSETS_BUCKET)
    except Exception as e:
        log.error("market report S3 upload failed (continuing with Telegram only): %s", e)

    filename = "dubai-market-report.pdf"
    delivered = False
    tg_chat_id = ctx.get("telegram_chat_id")
    if tg_chat_id:
        delivered = await _send_telegram_document(
            int(tg_chat_id), pdf_bytes, filename,
            caption=f"📊 Dubai Market Report — {context.get('as_of', '')}".strip(" —"),
        )

    if not delivered and not pdf_url:
        if tg_chat_id:
            return {"error": "The market report was rendered but couldn't be delivered: the S3 "
                             "download link needs an admin to grant s3:PutObject on the assets "
                             "bucket, and Telegram delivery failed this time. Please try again."}
        return {"error": "The market report was rendered but there's no download link yet: it "
                         "needs an admin to grant s3:PutObject on the assets bucket. Ask an admin "
                         "to enable it — retrying won't help until then."}

    log.info("market report ready size=%dKB url=%s telegram=%s",
             len(pdf_bytes) // 1024, s3_key, delivered)
    result = {
        "status": "completed",
        "title": "Dubai Market Report",
        "as_of": context.get("as_of"),
        "pdf_url": pdf_url,
        "filename": filename,
        "pages": 2,
        "communities": len(context.get("communities") or []),
        "picks": len(context.get("picks") or []),
        "sent_to_telegram": delivered,
    }
    if pdf_url:
        result["url_expires"] = "7 days"
    else:
        result["note"] = ("No download link — S3 upload unavailable. The PDF was delivered via "
                          "Telegram only.")
    return result


registry.register(Tool(
    name="generate_market_report",
    description=(
        "Generate the Allegiance-branded Dubai Market Report PDF — a 2-page A4 market snapshot: "
        "Dubai price index + KPIs (avg price/sqft, YoY appreciation), top communities ranked by "
        "Alpha conviction, the highest-conviction BUY projects, the most premium communities, and "
        "the overall verdict mix. All figures are real (Property Monitor + our Alpha Verdict store) "
        "— it is NOT project-specific. Use whenever the user asks for a 'Dubai market report', "
        "'market report PDF', 'market overview PDF', 'state of the market', or similar. Takes "
        "~20-40 seconds. Returns a download URL; on Telegram the PDF is also sent into the chat. "
        "After it returns, tell the user the report is ready but do NOT paste the link yourself."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    handler=generate_market_report_handler,
))
