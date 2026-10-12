"""Exercise schedule PDF changes using a local server and temporary database."""

from contextlib import asynccontextmanager
from pathlib import Path
import tempfile

import aiohttp
from aiohttp import web

from ministry_bot.schedule.pdf_storage import PdfStateStore
from ministry_bot.schedule.pdf_monitor import SchedulePdfClient, SchedulePdfMonitor, PdfMonitorError


def build_test_pdf(version: int) -> bytes:
    """Create a small, valid PDF fixture entirely in memory."""
    stream = f"BT /F1 12 Tf 20 80 Td (TEST timetable version {version}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 260 160] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(body)
    body.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


@asynccontextmanager
async def simulate_pdf_update():
    """Exercise API, conditional GET, hashing and outbox in an isolated fixture."""
    version = 1

    async def page(request):
        return web.json_response({
            "id": 101,
            "content": {"rendered": (
                '<div id="announcementAttachment">'
                f'<a href="/schedule.pdf">Test timetable v{version}</a></div>'
            )},
        })

    async def pdf(request):
        etag = f'"demo-{version}"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.Response(
            body=build_test_pdf(version), content_type="application/pdf",
            headers={"ETag": etag},
        )

    app = web.Application()
    app.router.add_get("/api", page)
    app.router.add_get("/schedule.pdf", pdf)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10), trust_env=False
        ) as session:
            with tempfile.TemporaryDirectory(prefix="ministry-bot-test-") as directory:
                store = PdfStateStore(Path(directory) / "test.sqlite3")
                watcher = SchedulePdfMonitor(
                    SchedulePdfClient(session, f"http://127.0.0.1:{port}/api"), store
                )
                baseline = await watcher.check_for_pdf_changes()
                version = 2
                changed = await watcher.check_for_pdf_changes()
                unchanged = await watcher.check_for_pdf_changes()
                if (
                    (baseline.kind, changed.kind, unchanged.kind)
                    != ("baseline", "changed", "unchanged")
                    or len(store.get_pending_notifications()) != 1
                ):
                    raise PdfMonitorError("The simulation did not detect exactly one change.")
                yield store
    finally:
        await runner.cleanup()
