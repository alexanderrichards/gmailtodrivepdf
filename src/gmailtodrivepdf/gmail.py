"""Fetching and parsing Gmail messages."""

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

STARRED_LABEL_ID = "STARRED"
GMAIL_REQUEST_RETRIES = 7
# Messages with these labels are excluded from listings unless explicitly requested.
_SPAM_TRASH_LABEL_IDS = {"SPAM", "TRASH"}


class UnknownLabelError(ValueError):
    def __init__(self, unknown: list[str], available: list[str]) -> None:
        self.unknown = unknown
        self.available = available
        super().__init__(
            f"Unknown Gmail label(s): {', '.join(unknown)}. "
            f"Available labels: {', '.join(available)}"
        )


@dataclass
class EmailMessage:
    id: str
    subject: str = ""
    sender: str = ""
    to: str = ""
    cc: str = ""
    date: datetime | None = None
    date_raw: str = ""
    html_body: str | None = None
    text_body: str | None = None
    # Content-ID (without angle brackets) -> (mime type, raw bytes)
    inline_images: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    attachment_names: list[str] = field(default_factory=list)


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _charset(part: dict[str, Any]) -> str:
    for header in part.get("headers", []):
        if header["name"].lower() == "content-type":
            for param in header["value"].split(";")[1:]:
                key, _, value = param.strip().partition("=")
                if key.lower() == "charset" and value:
                    return value.strip('"\'')
    return "utf-8"


def _header(part: dict[str, Any], name: str) -> str:
    for header in part.get("headers", []):
        if header["name"].lower() == name.lower():
            return header["value"]
    return ""


def _part_bytes(service: Any, message_id: str, body: dict[str, Any]) -> bytes:
    if "data" in body:
        return _decode(body["data"])
    if "attachmentId" in body:
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=body["attachmentId"])
            .execute(num_retries=GMAIL_REQUEST_RETRIES)
        )
        return _decode(attachment["data"])
    return b""


def _walk(service: Any, message: EmailMessage, part: dict[str, Any]) -> None:
    mime_type = part.get("mimeType", "")
    body = part.get("body", {})
    filename = part.get("filename", "")
    content_id = _header(part, "Content-ID").strip("<>")

    if mime_type.startswith("multipart/"):
        for child in part.get("parts", []):
            _walk(service, message, child)
        return

    if mime_type.startswith("image/") and content_id:
        message.inline_images[content_id] = (
            mime_type,
            _part_bytes(service, message.id, body),
        )
        return

    if filename:
        message.attachment_names.append(filename)
        return

    if mime_type in ("text/html", "text/plain"):
        text = _part_bytes(service, message.id, body).decode(
            _charset(part), errors="replace"
        )
        if mime_type == "text/html" and message.html_body is None:
            message.html_body = text
        elif mime_type == "text/plain" and message.text_body is None:
            message.text_body = text


def parse_message(service: Any, raw: dict[str, Any]) -> EmailMessage:
    payload = raw.get("payload", {})
    message = EmailMessage(
        id=raw["id"],
        subject=_header(payload, "Subject"),
        sender=_header(payload, "From"),
        to=_header(payload, "To"),
        cc=_header(payload, "Cc"),
        date_raw=_header(payload, "Date"),
    )
    if message.date_raw:
        try:
            message.date = parsedate_to_datetime(message.date_raw)
        except (TypeError, ValueError):
            pass
    if message.date is None and "internalDate" in raw:
        message.date = datetime.fromtimestamp(int(raw["internalDate"]) / 1000)
    _walk(service, message, payload)
    return message


def parse_label_list(value: str) -> list[str]:
    """Split a comma-separated list of label names, dropping blanks and duplicates."""
    names: list[str] = []
    for raw in value.split(","):
        name = raw.strip()
        if name and name.casefold() not in (n.casefold() for n in names):
            names.append(name)
    return names


def resolve_label_ids(service: Any, names: list[str]) -> list[str]:
    """Map label names (or IDs) to Gmail label IDs, matching case-insensitively.

    System labels such as INBOX or STARRED have names equal to their IDs, so they
    can be given either way.
    """
    labels = (
        service.users()
        .labels()
        .list(userId="me")
        .execute(num_retries=GMAIL_REQUEST_RETRIES)
        .get("labels", [])
    )
    lookup: dict[str, str] = {}
    for label in labels:
        lookup.setdefault(label["name"].casefold(), label["id"])
        lookup.setdefault(label["id"].casefold(), label["id"])

    unknown = [name for name in names if name.casefold() not in lookup]
    if unknown:
        raise UnknownLabelError(
            unknown, sorted((label["name"] for label in labels), key=str.casefold)
        )
    return list(dict.fromkeys(lookup[name.casefold()] for name in names))


def _list_label(service: Any, label_id: str, limit: int | None) -> Iterator[str]:
    page_token: str | None = None
    yielded = 0
    while True:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                labelIds=[label_id],
                includeSpamTrash=label_id in _SPAM_TRASH_LABEL_IDS,
                pageToken=page_token,
                maxResults=500,
            )
            .execute(num_retries=GMAIL_REQUEST_RETRIES)
        )
        for item in response.get("messages", []):
            yield item["id"]
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def list_message_ids(
    service: Any, label_ids: list[str], max_results: int | None = None
) -> list[str]:
    """Return IDs of messages carrying any of ``label_ids``, without duplicates."""
    # The API's labelIds filter requires *all* labels, so query each label separately.
    seen: dict[str, None] = {}
    for label_id in label_ids:
        for message_id in _list_label(service, label_id, max_results):
            seen.setdefault(message_id, None)
            if max_results is not None and len(seen) >= max_results:
                return list(seen)
    return list(seen)


def get_message(service: Any, message_id: str) -> EmailMessage:
    raw = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute(num_retries=GMAIL_REQUEST_RETRIES)
    )
    return parse_message(service, raw)
