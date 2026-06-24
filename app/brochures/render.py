"""Render the brochure template to PDF with headless Chromium (Playwright).

The template + brand assets live in app/templates/brochure/. Project photos are
written into a temp dir next to the rendered HTML so Chromium loads everything
from disk; only the Leaflet map tiles need the network (guarded by a timeout —
a grey map never blocks the PDF).
"""
import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger("askalpha.brochures")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "brochure"
STATIC_DIR = TEMPLATE_DIR / "static"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)

MAP_READY_TIMEOUT_MS = 12_000
RENDER_TIMEOUT_MS = 45_000

# Only one Chromium at a time per process; renders are quick (~5-10s) and this
# keeps memory bounded on the small Railway instance.
_render_lock = asyncio.Lock()


def _find_vanitas() -> str | None:
    """The Vanitas font file for templates that load it via the single-weight `vanitas_font`
    slot (comparison/market_report). The brochure (offplan) and flyer hardcode their own
    Regular+Bold Vanitas faces, so they don't use this. Match files like
    'fonnts.com-Vanitas-Bold.otf', preferring a Regular weight for the single-weight slot."""
    fonts = STATIC_DIR / "fonts"
    hits = sorted(fonts.glob("*Vanitas*"))
    if not hits:
        return None
    for h in hits:
        if "regular" in h.name.lower():
            return h.name
    return hits[0].name


def render_html(template_name: str, context: dict) -> str:
    template = _env.get_template(template_name)
    return template.render(
        **context,
        static_dir=STATIC_DIR.as_uri(),
        vanitas_font=_find_vanitas(),
    )


async def html_to_pdf(
    html: str,
    image_files: dict[str, bytes],
    landscape: bool = True,
    viewport: Optional[dict] = None,
) -> bytes:
    """Write html + images to a temp dir, print to PDF, return the bytes."""
    from playwright.async_api import async_playwright

    if viewport is None:
        # A4 at 96dpi: 1123x794 landscape, 794x1123 portrait.
        viewport = {"width": 1123, "height": 794} if landscape else {"width": 794, "height": 1123}

    async with _render_lock:
        with tempfile.TemporaryDirectory(prefix="brochure-") as tmp:
            tmp_path = Path(tmp)
            for name, data in image_files.items():
                (tmp_path / name).write_bytes(data)
            index = tmp_path / "index.html"
            index.write_text(html, encoding="utf-8")

            # Local-dev fallback: WSL/desktop installs without root extract Chromium's
            # missing shared libs here. Production (Docker --with-deps) never has this dir.
            env = None
            local_libs = Path.home() / ".cache" / "chromium-local-libs"
            if local_libs.is_dir():
                prev = os.environ.get("LD_LIBRARY_PATH", "")
                env = {**os.environ, "LD_LIBRARY_PATH": f"{local_libs}:{prev}" if prev else str(local_libs)}

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(env=env, args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--force-color-profile=srgb",
                ])
                try:
                    page = await browser.new_page(viewport=viewport)
                    await page.goto(index.as_uri(), wait_until="networkidle",
                                    timeout=RENDER_TIMEOUT_MS)
                    try:
                        await page.wait_for_function("window.__MAP_READY__ === true",
                                                     timeout=MAP_READY_TIMEOUT_MS)
                    except Exception:
                        log.warning("map tiles not ready before timeout — rendering anyway")
                    await page.wait_for_timeout(400)  # settle fonts/last tile paints
                    pdf = await page.pdf(
                        format="A4",
                        landscape=landscape,
                        print_background=True,
                        prefer_css_page_size=True,
                        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    )
                finally:
                    await browser.close()
    return pdf


async def render_brochure_pdf(context: dict, image_files: dict[str, bytes]) -> bytes:
    html = render_html("offplan.html.j2", context)
    return await html_to_pdf(html, image_files, landscape=True)


async def render_comparison_pdf(context: dict, image_files: dict[str, bytes]) -> bytes:
    html = render_html("comparison.html.j2", context)
    return await html_to_pdf(html, image_files, landscape=False)


async def render_market_report_pdf(context: dict, image_files: dict[str, bytes]) -> bytes:
    html = render_html("market_report.html.j2", context)
    return await html_to_pdf(html, image_files, landscape=False)


async def html_to_png(
    html: str,
    image_files: dict[str, bytes],
    selector: str,
    scale: int = 2,
) -> bytes:
    """Write html + images to a temp dir, screenshot one element, return PNG bytes.

    Screenshots the matched element (not the viewport) so the output height tracks
    the content; ``scale`` is the device pixel ratio for a crisp, retina-grade image.
    """
    from playwright.async_api import async_playwright

    async with _render_lock:
        with tempfile.TemporaryDirectory(prefix="flyer-") as tmp:
            tmp_path = Path(tmp)
            for name, data in image_files.items():
                (tmp_path / name).write_bytes(data)
            index = tmp_path / "index.html"
            index.write_text(html, encoding="utf-8")

            env = None
            local_libs = Path.home() / ".cache" / "chromium-local-libs"
            if local_libs.is_dir():
                prev = os.environ.get("LD_LIBRARY_PATH", "")
                env = {**os.environ, "LD_LIBRARY_PATH": f"{local_libs}:{prev}" if prev else str(local_libs)}

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(env=env, args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--force-color-profile=srgb",
                ])
                try:
                    page = await browser.new_page(device_scale_factor=scale)
                    await page.goto(index.as_uri(), wait_until="networkidle",
                                    timeout=RENDER_TIMEOUT_MS)
                    await page.wait_for_timeout(400)  # settle fonts/last paints
                    png = await page.locator(selector).screenshot(type="png")
                finally:
                    await browser.close()
    return png


async def render_flyer_png(context: dict, image_files: dict[str, bytes]) -> bytes:
    html = render_html("flyer.html.j2", context)
    return await html_to_png(html, image_files, selector="#flyer")
