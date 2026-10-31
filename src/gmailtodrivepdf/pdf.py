"""Rendering emails to PDF files."""

import base64
import html
import logging
import re
from pathlib import Path

from xhtml2pdf import pisa

from .gmail import EmailMessage

# xhtml2pdf logs a warning for every unsupported CSS property or missing glyph,
# which is very noisy for typical marketing/receipt emails.
logging.getLogger("xhtml2pdf").setLevel(logging.ERROR)

# 1x1 transparent GIF used in place of blocked resources.
_BLANK_IMAGE = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_BODY_OPEN = re.compile(r"<body[^>]*>", re.IGNORECASE)
_SCRIPT = re.compile(r"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
# xhtml2pdf crashes (KeyError) when an element has both lang and xml:lang.
_XML_LANG = re.compile(r"""\sxml:lang\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_MAX_STEM_LENGTH = 120

_HEADER_CSS = """
@page { size: a4; margin: 1.5cm; }
.gtdp-header { border-bottom: 1px solid #888; margin-bottom: 12px; padding-bottom: 6px;
               font-family: Helvetica; font-size: 10pt; }
.gtdp-header h1 { font-size: 14pt; margin: 0 0 6px 0; }
.gtdp-header td { vertical-align: top; padding: 1px 4px; }
.gtdp-label { font-weight: bold; width: 70px; }
.gtdp-plain { font-family: Courier; font-size: 9pt; white-space: pre-wrap; }
"""


class PdfRenderError(RuntimeError):
    pass


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


def build_html(message: EmailMessage) -> str:
    header = f"<style>{_HEADER_CSS}</style>{_header_html(message)}"
    if message.html_body:
        body = _XML_LANG.sub("", _SCRIPT.sub("", message.html_body))
        match = _BODY_OPEN.search(body)
        if match:
            return body[: match.end()] + header + body[match.end() :]
        return f"<html><head><meta charset='utf-8'></head><body>{header}{body}</body></html>"

    text = html.escape(message.text_body or "").replace("\n", "<br/>")
    return (
        "<html><head><meta charset='utf-8'></head><body>"
        f'{header}<div class="gtdp-plain">{text}</div></body></html>'
    )


def _make_link_callback(message: EmailMessage, allow_remote: bool):
    def link_callback(uri: str, rel: str) -> str:
        lowered = uri.strip().lower()
        if lowered.startswith("data:"):
            return uri
        if lowered.startswith("cid:"):
            image = message.inline_images.get(uri.strip()[4:].strip("<>"))
            if image:
                mime_type, data = image
                return f"data:{mime_type};base64,{base64.b64encode(data).decode()}"
            return _BLANK_IMAGE
        if allow_remote and lowered.startswith("https://"):
            return uri
        # Block everything else, notably file:// and relative paths, which could
        # otherwise embed local files into the PDF.
        return _BLANK_IMAGE

    return link_callback


def render_pdf(
    message: EmailMessage, destination: Path, allow_remote_images: bool = False
) -> Path:
    """Render a message to a PDF at ``destination``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".pdf.part")
    try:
        with tmp.open("wb") as handle:
            result = pisa.CreatePDF(
                build_html(message),
                dest=handle,
                encoding="utf-8",
                link_callback=_make_link_callback(message, allow_remote_images),
            )
    except Exception as exc:
        # xhtml2pdf can raise arbitrary exceptions on unusual HTML.
        tmp.unlink(missing_ok=True)
        raise PdfRenderError(
            f"Failed to render message {message.id} to PDF: {type(exc).__name__}: {exc}"
        ) from exc
    if result.err:
        tmp.unlink(missing_ok=True)
        raise PdfRenderError(f"Failed to render message {message.id} to PDF")
    tmp.replace(destination)
    return destination
