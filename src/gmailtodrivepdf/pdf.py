"""Rendering emails locally with Chromium."""

import base64
import html
import re
from html.parser import HTMLParser
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Error as PlaywrightError, Playwright, Route, sync_playwright

from .gmail import EmailMessage

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_CID_URL = re.compile(r"""cid:([^\s"'<>()]+)""", re.IGNORECASE)
_MAX_STEM_LENGTH = 120
_TIMEOUT_MS = 30_000
_HEADER_CSS = """
@page { size: A4; margin: 15mm; }
.gtdp-header { border-bottom: 1px solid #888; margin-bottom: 12px; padding-bottom: 6px;
               font-family: Arial, sans-serif; font-size: 10pt; color: #000; }
.gtdp-header h1 { font-size: 14pt; margin: 0 0 6px 0; }
.gtdp-header table { width: 100%; border-collapse: collapse; }
.gtdp-header td { vertical-align: top; padding: 1px 4px; overflow-wrap: anywhere; }
.gtdp-label { font-weight: bold; width: 70px; }
.gtdp-plain { font-family: "Courier New", monospace; font-size: 9pt;
              white-space: pre-wrap; overflow-wrap: anywhere; }
img { max-width: 100%; }
"""


class PdfRenderError(RuntimeError):
    pass


class _EmailHTMLParser(HTMLParser):
    # Remove active content and document metadata before Chromium parses it.
    _DROP_CONTENT = {"script", "iframe", "object", "template", "title", "noscript"}
    _DROP_TAGS = {"base", "meta", "link", "embed"}

    def __init__(self, message: EmailMessage) -> None:
        super().__init__(convert_charrefs=False)
        self.message = message
        self.fragments: list[str] = []
        self.ignored: list[str] = []
        self.in_style = False

    def _inline_images(self, value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            image = self.message.inline_images.get(match[1].strip("<>"))
            if image is None:
                return "data:,"
            mime_type, data = image
            return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"

        return _CID_URL.sub(replace, value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._DROP_CONTENT:
            self.ignored.append(tag)
        if self.ignored or tag in self._DROP_TAGS or tag in {"html", "head"}:
            return
        if tag == "body":
            tag = "div"
        clean_attrs = []
        for name, value in attrs:
            if name.startswith("on") or name in {"srcdoc", "action", "formaction", "nonce"}:
                continue
            if name in {"href", "xlink:href"} and value:
                if urlsplit(value.strip()).scheme.lower() not in {"https", "http", "mailto", "tel"}:
                    continue
            if value is None:
                clean_attrs.append(name)
            else:
                clean_attrs.append(f'{name}="{html.escape(self._inline_images(value), quote=True)}"')
        attributes = " " + " ".join(clean_attrs) if clean_attrs else ""
        self.fragments.append(f"<{tag}{attributes}>")
        if tag == "style":
            self.in_style = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in self._DROP_TAGS or tag in {"html", "head"}:
            return
        if tag == "body":
            tag = "div"
        self.fragments.append(f"</{tag}>")
        if tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.fragments.append(self._inline_images(data) if self.in_style else data)

    def handle_entityref(self, name: str) -> None:
        if not self.ignored:
            self.fragments.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.ignored:
            self.fragments.append(f"&#{name};")


def safe_filename(message: EmailMessage) -> str:
    """Build a filesystem-safe, unique PDF file name for a message."""
    date_part = message.date.strftime("%Y-%m-%d") if message.date else "undated"
    subject = _INVALID_FILENAME_CHARS.sub("_", message.subject or "no subject")
    subject = re.sub(r"\s+", " ", subject).strip(" ._") or "no subject"
    stem = f"{date_part} {subject}"[:_MAX_STEM_LENGTH].rstrip(" ._")
    return f"{stem} [{message.id}].pdf"


def _header_html(message: EmailMessage) -> str:
    rows = [
        ("From", message.sender),
        ("To", message.to),
        ("Cc", message.cc),
        ("Date", message.date_raw or (message.date.isoformat() if message.date else "")),
        ("Attachments", ", ".join(message.attachment_names)),
    ]
    row_html = "".join(
        f'<tr><td class="gtdp-label">{label}:</td><td>{html.escape(value)}</td></tr>'
        for label, value in rows
        if value
    )
    return (
        '<div class="gtdp-header">'
        f"<h1>{html.escape(message.subject or '(no subject)')}</h1>"
        f"<table>{row_html}</table></div>"
    )


def build_html(message: EmailMessage, allow_remote_images: bool = False) -> str:
    if message.html_body:
        parser = _EmailHTMLParser(message)
        parser.feed(message.html_body)
        parser.close()
        body = "".join(parser.fragments)
    else:
        body = f'<div class="gtdp-plain">{html.escape(message.text_body or "")}</div>'

    image_sources = "data: https:" if allow_remote_images else "data:"
    policy = (
        "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
        f"img-src {image_sources}; font-src data:; "
        "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(policy, quote=True)}">'
        f"<style>{_HEADER_CSS}</style></head><body>"
        f"{_header_html(message)}{body}</body></html>"
    )


def _route_resource(route: Route, allow_remote_images: bool) -> None:
    request = route.request
    if (
        allow_remote_images
        and urlsplit(request.url).scheme.lower() == "https"
        and request.resource_type == "image"
        and not request.is_navigation_request()
    ):
        route.continue_()
    else:
        route.abort()


class PdfRenderer:
    """Reuse one headless browser, with a fresh isolated context for each email."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def __enter__(self) -> "PdfRenderer":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._browser = None
            self._playwright = None

    def _get_browser(self) -> Browser:
        if self._browser is None:
            try:
                if self._playwright is None:
                    self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(
                    headless=True, chromium_sandbox=True,
                )
            except PlaywrightError as exc:
                raise PdfRenderError(
                    "Could not start Chromium. Install the browser with "
                    "'uv run playwright install chromium'. "
                    f"Browser error: {exc}"
                ) from exc
        return self._browser

    def render_pdf(
        self, message: EmailMessage, destination: Path, allow_remote_images: bool = False,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(".pdf.part")
        try:
            browser = self._get_browser()
            context = browser.new_context(
                java_script_enabled=False,
                service_workers="block",
                accept_downloads=False,
            )
            try:
                context.set_default_timeout(_TIMEOUT_MS)
                context.route("**/*", lambda route: _route_resource(route, allow_remote_images))
                page = context.new_page()
                page.emulate_media(media="print")
                page.set_content(
                    build_html(message, allow_remote_images),
                    wait_until="load",
                    timeout=_TIMEOUT_MS,
                )
                # Only our trusted code runs via Playwright; email JavaScript stays disabled.
                page.evaluate("() => document.fonts.ready")
                page.pdf(
                    path=str(tmp),
                    format="A4",
                    margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
                    print_background=True,
                    prefer_css_page_size=False,
                )
            finally:
                context.close()
            tmp.replace(destination)
        except (PlaywrightError, PdfRenderError, OSError, ValueError) as exc:
            tmp.unlink(missing_ok=True)
            raise PdfRenderError(
                f"Failed to render message {message.id} to PDF: {exc}"
            ) from exc
        return destination


def render_pdf(
    message: EmailMessage, destination: Path, allow_remote_images: bool = False,
) -> Path:
    """Render a single email; batch callers should reuse a ``PdfRenderer``."""
    with PdfRenderer() as renderer:
        return renderer.render_pdf(message, destination, allow_remote_images)
