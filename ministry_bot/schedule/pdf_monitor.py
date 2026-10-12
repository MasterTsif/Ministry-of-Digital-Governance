"""Find the schedule PDF, fetch its snapshot and detect content changes."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

from .pdf_storage import PdfSnapshot, PdfStateStore

MAX_PDF_BYTES = 20 * 1024 * 1024
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}


class PdfMonitorError(Exception):
    """The schedule source did not provide a valid PDF attachment."""

    pass


class PdfAttachmentParser(HTMLParser):
    """Collect PDF links and identify the page's attachment section."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []
        self.links: list[tuple[str, str, bool]] = []
        self.anchor: tuple[str, bool] | None = None
        self.label: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag not in VOID_TAGS:
            self.stack.append((tag, attributes.get("id")))
        if tag == "a" and attributes.get("href"):
            inside = any(value == "announcementAttachment" for _, value in self.stack)
            self.anchor = (attributes["href"], inside)
            self.label = []

    def handle_data(self, data):
        if self.anchor is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            href, preferred = self.anchor
            if urlsplit(href).path.lower().endswith(".pdf"):
                self.links.append((href, " ".join("".join(self.label).split()), preferred))
            self.anchor = None
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position][0] == tag:
                del self.stack[position:]
                break


def extract_pdf_attachment(content: str, base_url: str) -> tuple[str, str]:
    """Return the attachment's absolute URL and its display title."""
    parser = PdfAttachmentParser()
    parser.feed(content)
    preferred = [link for link in parser.links if link[2]]
    links = preferred or parser.links
    unique = {urljoin(base_url, href): title for href, title, _ in links}
    if len(unique) != 1:
        raise PdfMonitorError("The page must contain exactly one PDF attachment.")
    url, title = next(iter(unique.items()))
    if urlsplit(url).scheme not in {"https", "http"}:
        raise PdfMonitorError("Invalid PDF URL.")
    return quote(url, safe=":/%?=&+~#@!$'()*;,[]"), title or "Class timetable"


class SchedulePdfClient:
    """Read the source page and fingerprint its PDF, using HTTP cache headers."""

    def __init__(self, session: aiohttp.ClientSession, api_url: str):
        self.session = session
        self.api_url = api_url

    async def fetch_pdf_snapshot(self, previous: PdfSnapshot | None) -> PdfSnapshot:
        """Return PDF metadata and its SHA-256 without changing saved state."""
        async with self.session.get(
            self.api_url,
            params={"_fields": "id,modified_gmt,content.rendered,link"},
            # The department includes the attachment only in its Greek source page.
            headers={"Accept-Language": "el-GR,el;q=0.9"},
        ) as response:
            response.raise_for_status()
            page = await response.json()
        try:
            content = page["content"]["rendered"]
        except (KeyError, TypeError) as exc:
            raise PdfMonitorError("The API did not return page content.") from exc
        if not isinstance(content, str):
            raise PdfMonitorError("The API content is not an HTML string.")
        url, title = extract_pdf_attachment(content, page.get("link") or self.api_url)
        headers = {}
        if previous is not None and previous.url == url:
            if previous.etag:
                headers["If-None-Match"] = previous.etag
            if previous.last_modified:
                headers["If-Modified-Since"] = previous.last_modified
        async with self.session.get(url, headers=headers) as response:
            if response.status == 304:
                if previous is None or previous.url != url:
                    raise PdfMonitorError("The server returned 304 without a previous file.")
                digest = previous.sha256
                etag = response.headers.get("ETag") or previous.etag
                last_modified = response.headers.get("Last-Modified") or previous.last_modified
            else:
                response.raise_for_status()
                if response.status != 200:
                    raise PdfMonitorError(f"Unexpected PDF response: {response.status}.")
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise PdfMonitorError("The attachment exceeds the 20 MiB limit.")
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not body.startswith(b"%PDF-") or b"%%EOF" not in body[-4096:]:
                    raise PdfMonitorError("The attachment does not appear to be a complete PDF.")
                digest = hashlib.sha256(body).hexdigest()
                etag = response.headers.get("ETag")
                last_modified = response.headers.get("Last-Modified")
        return PdfSnapshot(
            url=url,
            title=title,
            sha256=digest,
            etag=etag,
            last_modified=last_modified,
            checked_at=datetime.now(UTC).isoformat(),
            page_modified=page.get("modified_gmt"),
        )


@dataclass(frozen=True)
class PdfCheckResult:
    kind: str
    snapshot: PdfSnapshot


class SchedulePdfMonitor:
    """Compare PDF snapshots and save changes with pending notifications."""

    def __init__(self, client: SchedulePdfClient, store: PdfStateStore):
        self.pdf_client = client
        self.state_store = store
        self.lock = asyncio.Lock()

    async def check_for_pdf_changes(self) -> PdfCheckResult:
        """Return baseline, changed or unchanged after one serialized check."""
        async with self.lock:
            previous = self.state_store.get_current_snapshot()
            snapshot = await self.pdf_client.fetch_pdf_snapshot(previous)
            changed = previous is not None and previous.sha256 != snapshot.sha256
            self.state_store.save_snapshot(snapshot, notify=changed)
            kind = "baseline" if previous is None else "changed" if changed else "unchanged"
            return PdfCheckResult(kind, snapshot)
