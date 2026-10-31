import base64
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from playwright.sync_api import Error as PlaywrightError
from pypdf import PdfReader
from typer.testing import CliRunner

from gmailtodrivepdf import cli, drive, gmail, pdf

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def pdf_image_sizes(path: Path) -> list[tuple[int, int]]:
    # Chromium also embeds its broken-image icon for resources blocked by CSP.
    objects = PdfReader(path).pages[0]["/Resources"].get("/XObject", {})
    return [
        (obj["/Width"], obj["/Height"])
        for reference in objects.values()
        if (obj := reference.get_object()).get("/Subtype") == "/Image"
    ]


def raw_message(message_id: str = "abc123") -> dict:
    return {
        "id": message_id,
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Invoice: Q3/2026 <final>"},
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "To", "value": "bob@example.com"},
                {"name": "Date", "value": "Tue, 14 Nov 2023 22:13:20 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/related",
                    "parts": [
                        {
                            "mimeType": "multipart/alternative",
                            "parts": [
                                {
                                    "mimeType": "text/plain",
                                    "headers": [
                                        {"name": "Content-Type", "value": "text/plain; charset=utf-8"}
                                    ],
                                    "body": {"data": b64("Hello plain")},
                                },
                                {
                                    "mimeType": "text/html",
                                    "headers": [
                                        {"name": "Content-Type", "value": 'text/html; charset="utf-8"'}
                                    ],
                                    "body": {
                                        "data": b64(
                                            "<html><body><p>Hello <b>world</b> \u00e9</p>"
                                            '<img src="cid:logo@x">'
                                            '<img src="file:///etc/passwd">'
                                            "<script>alert(1)</script></body></html>"
                                        )
                                    },
                                },
                            ],
                        },
                        {
                            "mimeType": "image/png",
                            "headers": [{"name": "Content-ID", "value": "<logo@x>"}],
                            "body": {"attachmentId": "att1"},
                        },
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att2"},
                },
            ],
        },
    }


LABELS = [
    {"id": "INBOX", "name": "INBOX"},
    {"id": "STARRED", "name": "STARRED"},
    {"id": "Label_1", "name": "Receipts"},
    {"id": "Label_2", "name": "Work/Projects"},
]


def gmail_service(messages: dict[str, dict], labelled: dict[str, list[str]] | None = None) -> MagicMock:
    """Mock Gmail service. ``labelled`` maps label ID -> message IDs (default: all STARRED)."""
    labelled = labelled if labelled is not None else {"STARRED": list(messages)}
    service = MagicMock()
    service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
        "labels": LABELS
    }
    msgs = service.users.return_value.messages.return_value
    msgs.list.side_effect = lambda userId, labelIds, **kw: MagicMock(
        execute=MagicMock(
            return_value={"messages": [{"id": i} for i in labelled.get(labelIds[0], [])]}
        )
    )
    msgs.get.side_effect = lambda userId, id, format: MagicMock(
        execute=MagicMock(return_value=messages[id])
    )
    msgs.attachments.return_value.get.return_value.execute.return_value = {
        "data": b64(PNG)
    }
    return service


def test_parse_message_extracts_parts():
    service = gmail_service({"abc123": raw_message()})
    get = service.users().messages().get
    get.side_effect = None
    get.return_value.execute.return_value = raw_message()
    message = gmail.get_message(service, "abc123")

    assert message.subject == "Invoice: Q3/2026 <final>"
    assert message.sender == "Alice <alice@example.com>"
    assert message.date == datetime.fromisoformat("2023-11-14T22:13:20+00:00")
    assert message.text_body == "Hello plain"
    assert "<b>world</b> \u00e9" in message.html_body
    assert message.inline_images["logo@x"] == ("image/png", PNG)
    assert message.attachment_names == ["invoice.pdf"]
    get.return_value.execute.assert_called_once_with(
        num_retries=gmail.GMAIL_REQUEST_RETRIES
    )
    service.users().messages().attachments().get.return_value.execute.assert_called_once_with(
        num_retries=gmail.GMAIL_REQUEST_RETRIES
    )


def test_list_message_ids_paginates_and_limits():
    service = MagicMock()
    list_call = service.users.return_value.messages.return_value.list
    list_call.return_value.execute.side_effect = [
        {"messages": [{"id": "1"}, {"id": "2"}], "nextPageToken": "t"},
        {"messages": [{"id": "3"}]},
    ]
    assert gmail.list_message_ids(service, ["STARRED"]) == ["1", "2", "3"]
    first = list_call.call_args_list[0].kwargs
    assert first["labelIds"] == ["STARRED"]
    assert first["includeSpamTrash"] is False
    assert list_call.call_args_list[1].kwargs["pageToken"] == "t"
    list_call.return_value.execute.assert_called_with(num_retries=gmail.GMAIL_REQUEST_RETRIES)

    list_call.return_value.execute.side_effect = [
        {"messages": [{"id": "1"}, {"id": "2"}], "nextPageToken": "t"},
    ]
    assert gmail.list_message_ids(service, ["STARRED"], max_results=1) == ["1"]


def test_list_message_ids_unions_labels_without_duplicates():
    service = gmail_service({}, {"Label_1": ["a", "b"], "Label_2": ["b", "c"], "TRASH": ["d"]})
    assert gmail.list_message_ids(service, ["Label_1", "Label_2"]) == ["a", "b", "c"]
    assert gmail.list_message_ids(service, ["Label_1", "Label_2"], max_results=2) == ["a", "b"]
    gmail.list_message_ids(service, ["TRASH"])
    list_call = service.users.return_value.messages.return_value.list
    assert list_call.call_args.kwargs["includeSpamTrash"] is True


def test_parse_label_list():
    assert gmail.parse_label_list(" Receipts, work/projects ,,receipts, ") == [
        "Receipts",
        "work/projects",
    ]
    assert gmail.parse_label_list(" , ") == []


def test_resolve_label_ids():
    service = gmail_service({})
    assert gmail.resolve_label_ids(service, ["receipts", "WORK/PROJECTS", "inbox", "Label_1"]) == [
        "Label_1",
        "Label_2",
        "INBOX",
    ]
    with pytest.raises(gmail.UnknownLabelError) as info:
        gmail.resolve_label_ids(service, ["Receipts", "Nope"])
    assert info.value.unknown == ["Nope"]
    assert "Work/Projects" in info.value.available
    service.users().labels().list().execute.assert_called_with(
        num_retries=gmail.GMAIL_REQUEST_RETRIES
    )


def api_request(responses: list[tuple[int, dict]]) -> HttpRequest:
    http = MagicMock()
    http.request.side_effect = [
        (httplib2.Response({"status": str(status)}), json.dumps(body).encode())
        for status, body in responses
    ]
    request = HttpRequest(
        http,
        lambda response, content: json.loads(content),
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/example",
    )
    request._sleep = MagicMock()
    request._rand = lambda: 0.5
    return request


def api_error(status: int, reason: str) -> tuple[int, dict]:
    return status, {"error": {"code": status, "errors": [{"reason": reason}]}}


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, "rateLimitExceeded"), (403, "userRateLimitExceeded"), (429, "tooManyRequests"), (503, "backendError")],
)
def test_gmail_attachment_download_retries_transient_errors(status: int, reason: str):
    request = api_request([
        api_error(status, reason),
        (200, {"data": b64(PNG)}),
    ])
    service = MagicMock()
    service.users().messages().attachments().get.return_value = request

    assert gmail._part_bytes(service, "example", {"attachmentId": "image"}) == PNG
    assert request.http.request.call_count == 2
    request._sleep.assert_called_once_with(1.0)


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, "insufficientPermissions"), (403, "dailyLimitExceeded"), (401, "authError"), (404, "notFound")],
)
def test_gmail_does_not_retry_permanent_errors(status: int, reason: str):
    request = api_request([api_error(status, reason)])
    service = MagicMock()
    service.users().messages().get.return_value = request

    with pytest.raises(HttpError):
        gmail.get_message(service, "example")
    assert request.http.request.call_count == 1
    request._sleep.assert_not_called()


def test_gmail_retries_are_bounded_and_use_exponential_backoff():
    request = api_request(
        [api_error(403, "rateLimitExceeded")] * (gmail.GMAIL_REQUEST_RETRIES + 1)
    )
    service = MagicMock()
    service.users().messages().attachments().get.return_value = request

    with pytest.raises(HttpError):
        gmail._part_bytes(service, "example", {"attachmentId": "image"})
    assert request.http.request.call_count == gmail.GMAIL_REQUEST_RETRIES + 1
    assert [call.args[0] for call in request._sleep.call_args_list] == [
        1, 2, 4, 8, 16, 32, 64,
    ]


def test_safe_filename():
    message = gmail.EmailMessage(
        id="abc", subject='a/b\\c:d*e?"f<g>|h', date=datetime(2026, 1, 2)
    )
    assert pdf.safe_filename(message) == "2026-01-02 a_b_c_d_e_f_g_h [abc].pdf"
    assert pdf.safe_filename(gmail.EmailMessage(id="x")) == "undated no subject [x].pdf"


def test_build_html_strips_scripts_and_escapes_headers():
    message = gmail.parse_message(gmail_service({}), raw_message())
    html = pdf.build_html(message)
    assert "<script" not in html
    assert "Invoice: Q3/2026 &lt;final&gt;" in html


def test_build_html_embeds_inline_images_and_blocks_active_content():
    message = gmail.EmailMessage(id="x", inline_images={"logo@x": ("image/png", PNG)})
    message.html_body = (
        '<html><head><meta http-equiv="refresh" content="0;url=https://example.com">'
        '<base href="file:///"><link rel="stylesheet" href="https://example.com/style.css">'
        '<style>.logo {background-image:url(cid:logo@x)}</style></head>'
        '<body><script>alert(1)</script><iframe srcdoc="secret"></iframe>'
        '<object data="file:///secret"></object><img src="cid:logo@x" onerror="alert(1)">'
        '<a href="file:///secret">Unsafe link</a><p>Receipt &amp; tax</p></body></html>'
    )
    document = pdf.build_html(message)
    assert f"data:image/png;base64,{base64.b64encode(PNG).decode()}" in document
    for forbidden in ("<script", "<iframe", "<object", "<base", "<link", 'http-equiv="refresh"', "onerror", "file:///"):
        assert forbidden not in document
    assert "Receipt &amp; tax" in document
    assert "img-src data:;" in document
    assert "img-src data: https:;" in pdf.build_html(message, True)


@pytest.mark.parametrize("allow_remote", [False, True])
@pytest.mark.parametrize(
    ("url", "resource_type", "navigation", "allowed"),
    [
        ("https://example.com/image.png", "image", False, True),
        ("http://example.com/image.png", "image", False, False),
        ("file:///secret.png", "image", False, False),
        ("https://example.com/style.css", "stylesheet", False, False),
        ("https://example.com/code.js", "script", False, False),
        ("https://example.com/font.woff", "font", False, False),
        ("https://example.com/page", "image", True, False),
    ],
)
def test_browser_resource_routing(url, resource_type, navigation, allowed, allow_remote):
    route = MagicMock()
    route.request.url = url
    route.request.resource_type = resource_type
    route.request.is_navigation_request.return_value = navigation
    pdf._route_resource(route, allow_remote)
    if allowed and allow_remote:
        route.continue_.assert_called_once()
        route.abort.assert_not_called()
    else:
        route.abort.assert_called_once()
        route.continue_.assert_not_called()


@pytest.mark.parametrize("html_body", [True, False])
def test_render_pdf(tmp_path: Path, html_body: bool):
    message = gmail.parse_message(gmail_service({}), raw_message())
    if not html_body:
        message.html_body = None
    out = pdf.render_pdf(message, tmp_path / "sub" / "out.pdf")
    assert out.read_bytes().startswith(b"%PDF")
    reader = PdfReader(out)
    text = reader.pages[0].extract_text()
    assert "Invoice: Q3/2026 <final>" in text
    assert "Alice <alice@example.com>" in text
    assert "invoice.pdf" in text
    assert ("Hello world" if html_body else "Hello plain") in text
    assert bool(reader.pages[0].images) == html_body
    assert abs(float(reader.pages[0].mediabox.width) - 595.28) < 1
    assert not list(tmp_path.rglob("*.part"))


def test_render_pdf_with_lang_and_xml_lang(tmp_path: Path):
    message = gmail.EmailMessage(
        id="x",
        html_body='<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang=\'en\'>'
        "<body><p>Receipt</p></body></html>",
    )
    out = pdf.render_pdf(message, tmp_path / "out.pdf")
    assert out.read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize("nested", [False, True])
def test_chromium_preserves_styled_html_across_pages(tmp_path: Path, nested: bool):
    lines = [f"Receipt item {i:04d}" for i in range(250)]
    inner = "".join(f"<tr><td>{line}</td></tr>" for line in lines)
    source = (
        '<html lang="en" xml:lang="en"><head><style>'
        ".receipt {border: 2px solid #cc0000; background: #eeeeff; padding: 10px}"
        "</style></head><body><script>not email text</script>"
        '<div class="receipt">'
        + ('<div class="receipt">' if nested else "")
        + f"<table>{inner}</table>"
        + ('</div>' if nested else "")
        + "</div></body></html>"
    )
    message = gmail.EmailMessage(
        id="layout",
        subject="Test receipt",
        html_body=source,
        text_body="This plain-text alternative must not replace the HTML.",
    )
    destination = tmp_path / "receipt.pdf"
    pdf.render_pdf(message, destination)

    reader = PdfReader(destination)
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) > 1
    assert "Test receipt" in text
    assert all(line in text for line in lines)
    assert "not email text" not in text
    assert "plain-text alternative" not in text
    graphics = reader.pages[0].get_contents().get_data()
    assert b".8 0 0 RG" in graphics
    assert b".9333 .9333 1 rg" in graphics
    assert message.html_body == source
    assert not list(tmp_path.glob("*.part"))


def test_browser_failure_preserves_existing_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    browser = MagicMock()
    context = browser.new_context.return_value
    page = context.new_page.return_value
    def fail_print(**kwargs):
        Path(kwargs["path"]).write_bytes(b"partial PDF")
        raise PlaywrightError("Print failed")

    page.pdf.side_effect = fail_print
    monkeypatch.setattr(pdf.PdfRenderer, "_get_browser", lambda self: browser)
    destination = tmp_path / "receipt.pdf"
    destination.write_bytes(b"existing PDF")
    message = gmail.EmailMessage(id="layout", html_body="<p>Receipt</p>")
    with pytest.raises(pdf.PdfRenderError, match="Print failed"):
        pdf.render_pdf(message, destination)
    assert destination.read_bytes() == b"existing PDF"
    context.close.assert_called_once()
    assert not list(tmp_path.glob("*.part"))


def test_missing_chromium_has_install_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = MagicMock()
    playwright = manager.start.return_value
    playwright.chromium.launch.side_effect = PlaywrightError("Executable doesn't exist")
    monkeypatch.setattr(pdf, "sync_playwright", lambda: manager)
    with pytest.raises(pdf.PdfRenderError, match="uv run playwright install chromium"):
        pdf.render_pdf(gmail.EmailMessage(id="plain", text_body="Receipt"), tmp_path / "receipt.pdf")
    playwright.stop.assert_called_once()
    assert not list(tmp_path.iterdir())


def test_real_browser_blocks_active_local_and_remote_content_and_isolates_emails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    secret = tmp_path / "secret.svg"
    secret.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<text x="10" y="30">LOCAL SECRET</text></svg>',
        encoding="utf-8",
    )
    message = gmail.EmailMessage(
        id="untrusted",
        inline_images={"inline": ("image/png", PNG)},
        html_body=(
            '<meta http-equiv="refresh" content="0;url=https://example.invalid/redirect">'
            '<script>document.body.innerHTML="SCRIPT EXECUTED"</script>'
            '<img src="cid:inline" onload="document.body.innerHTML=\'HANDLER EXECUTED\'">'
            f'<img src="{secret.as_uri()}">'
            '<img src="https://example.invalid/tracker.png">'
            '<iframe src="https://example.invalid/frame"></iframe>'
            '<link rel="stylesheet" href="https://example.invalid/style.css">'
            '<style>@import url("https://example.invalid/import.css");'
            f'.local {{background-image: url("{secret.as_uri()}");}}'
            '</style><div class="local">Safe receipt</div>'
        ),
    )
    contexts = []
    requests = []
    failures = []
    responses = []
    with pdf.PdfRenderer() as renderer:
        browser = renderer._get_browser()
        real_new_context = browser.new_context

        def record_context(**kwargs):
            assert kwargs["java_script_enabled"] is False
            assert kwargs["service_workers"] == "block"
            context = real_new_context(**kwargs)
            context.on("request", lambda request: requests.append(request.url))
            context.on("requestfailed", lambda request: failures.append(request.url))
            context.on("response", lambda response: responses.append(response.url))
            contexts.append(context)
            return context

        monkeypatch.setattr(browser, "new_context", record_context)
        first = renderer.render_pdf(message, tmp_path / "untrusted.pdf")
        second = renderer.render_pdf(
            gmail.EmailMessage(id="second", text_body="Independent email"),
            tmp_path / "second.pdf",
        )
        assert renderer._get_browser() is browser
        assert len(contexts) == 2
        assert contexts[0] is not contexts[1]
        assert not browser.contexts

    assert renderer._browser is None
    assert renderer._playwright is None
    text = PdfReader(first).pages[0].extract_text()
    assert "Safe receipt" in text
    assert "EXECUTED" not in text
    assert "LOCAL SECRET" not in text
    assert pdf_image_sizes(first).count((1, 1)) == 1
    assert (200, 100) not in pdf_image_sizes(first)
    assert "Safe receipt" not in PdfReader(second).pages[0].extract_text()
    blocked = {url for url in requests if url.startswith(("https:", "http:", "file:"))}
    assert blocked <= set(failures)
    assert not [url for url in responses if url.startswith(("https:", "http:", "file:"))]


@pytest.mark.parametrize("allow_remote_images", [False, True])
def test_real_browser_only_requests_https_images_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_remote_images: bool,
):
    requested = []

    def intercept(route, allow_remote):
        requested.append((route.request.url, route.request.resource_type))
        if allow_remote and route.request.url == "https://example.invalid/allowed.png":
            route.fulfill(content_type="image/png", body=PNG)
        else:
            route.abort()

    monkeypatch.setattr(pdf, "_route_resource", intercept)
    message = gmail.EmailMessage(
        id="remote",
        html_body=(
            '<p>Remote image test</p><img src="https://example.invalid/allowed.png">'
            '<img src="http://example.invalid/blocked.png">'
            '<style>@import url("https://example.invalid/stylesheet.css");'
            '@font-face {font-family: remote; src: url("https://example.invalid/font.woff")}'
            'p {font-family: remote}</style>'
            '<script src="https://example.invalid/code.js"></script>'
        ),
    )
    path = pdf.render_pdf(message, tmp_path / "remote.pdf", allow_remote_images)
    assert requested == (
        [("https://example.invalid/allowed.png", "image")] if allow_remote_images else []
    )
    assert ((1, 1) in pdf_image_sizes(path)) == allow_remote_images


def test_cli_continues_after_render_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bad = raw_message("bad")
    bad["payload"]["headers"][0]["value"] = "Bad"
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *a: object())
    monkeypatch.setattr(
        cli, "build", lambda *a, **k: gmail_service({"bad": bad, "good": raw_message("good")})
    )
    real_render = pdf.PdfRenderer.render_pdf

    def flaky(self, message, *args, **kwargs):
        if message.subject == "Bad":
            raise pdf.PdfRenderError("Print failed")
        return real_render(self, message, *args, **kwargs)

    monkeypatch.setattr(pdf.PdfRenderer, "render_pdf", flaky)
    out = tmp_path / "out"
    result = CliRunner().invoke(cli.app, ["-o", str(out)])
    assert result.exit_code == 1
    assert "1 failed" in result.output
    assert [p.name for p in out.glob("*.pdf")] == [
        "2023-11-14 Invoice_ Q3_2026 _final [good].pdf"
    ]


def test_drive_folder_reused_or_created():
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": [{"id": "F1"}]}
    assert drive.get_or_create_folder(service, "pdfs") == "F1"
    files.create.assert_not_called()

    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = {"id": "F2"}
    assert drive.get_or_create_folder(service, "it's") == "F2"
    assert "name = 'it\\'s'" in files.list.call_args.kwargs["q"]


def test_drive_upload_skips_existing(tmp_path: Path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF")
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": [{"id": "E"}]}
    assert drive.upload_pdf(service, path, "F") == ("E", False)
    files.update.return_value.execute.return_value = {"id": "E"}
    assert drive.upload_pdf(service, path, "F", overwrite=True) == ("E", True)

    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = {"id": "N"}
    assert drive.upload_pdf(service, path, "F") == ("N", True)


def test_cli_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gsvc = gmail_service({"m1": raw_message("m1"), "m2": raw_message("m2")})
    dsvc = MagicMock()
    dfiles = dsvc.files.return_value
    dfiles.list.return_value.execute.return_value = {"files": []}
    dfiles.create.return_value.execute.return_value = {"id": "ID"}
    requested_scopes = []

    def fake_creds(scopes, credentials_path, token_path):
        requested_scopes.extend(scopes)
        return object()

    monkeypatch.setattr(cli.auth, "get_credentials", fake_creds)
    monkeypatch.setattr(
        cli, "build", lambda name, *a, **k: gsvc if name == "gmail" else dsvc
    )
    out = tmp_path / "out"
    result = CliRunner().invoke(cli.app, ["-o", str(out), "--upload-to-drive"])

    assert result.exit_code == 0, result.output
    assert len(list(out.glob("*.pdf"))) == 2
    assert "uploaded 2" in result.output
    assert any("drive.file" in s for s in requested_scopes)

    result = CliRunner().invoke(cli.app, ["-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "skipped 2" in result.output


def test_cli_uploads_chromium_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = gmail_service({"receipt": raw_message("receipt")})
    drive_service = MagicMock()
    drive_service.files().list.return_value.execute.return_value = {"files": []}
    drive_service.files().create.return_value.execute.return_value = {"id": "uploaded"}
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *a: object())
    monkeypatch.setattr(
        cli, "build", lambda name, *a, **k: service if name == "gmail" else drive_service,
    )
    result = CliRunner().invoke(cli.app, ["-o", str(tmp_path), "--upload-to-drive"])

    assert result.exit_code == 0, result.output
    assert "Created 1" in result.output
    assert "uploaded 1" in result.output
    path, = tmp_path.glob("*.pdf")
    assert "Hello world" in PdfReader(path).pages[0].extract_text()
    upload = drive_service.files().create.call_args.kwargs
    assert upload["media_body"].mimetype() == "application/pdf"
    assert upload["body"]["name"] == path.name


def test_cli_default_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *a: object())
    monkeypatch.setattr(cli, "build", lambda *a, **k: gmail_service({"m1": raw_message("m1")}))
    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "pdfs").glob("*.pdf"))) == 1


def test_cli_labels_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    messages = {i: raw_message(i) for i in ("s1", "r1", "w1", "o1")}
    labelled = {"STARRED": ["s1"], "Label_1": ["r1"], "Label_2": ["w1", "r1"], "INBOX": ["o1"]}
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *a: object())
    monkeypatch.setattr(cli, "build", lambda *a, **k: gmail_service(messages, labelled))

    out = tmp_path / "out"
    result = CliRunner().invoke(cli.app, ["-o", str(out), "--labels", "receipts, Work/Projects"])
    assert result.exit_code == 0, result.output
    assert "Found 2 email(s) labelled receipts, Work/Projects" in result.output
    assert sorted(p.name.split("[")[1] for p in out.glob("*.pdf")) == ["r1].pdf", "w1].pdf"]


def test_cli_unknown_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *a: object())
    monkeypatch.setattr(cli, "build", lambda *a, **k: gmail_service({}))
    out = tmp_path / "out"
    result = CliRunner().invoke(cli.app, ["-o", str(out), "-l", "Receipts,Missing"])
    assert result.exit_code == 2
    assert "Unknown Gmail label(s): Missing" in result.output
    assert not out.exists()


def test_cli_empty_labels():
    result = CliRunner().invoke(cli.app, ["--labels", " , "])
    assert result.exit_code == 2
    assert "at least one label" in result.output


def test_cli_missing_credentials(tmp_path: Path):
    result = CliRunner().invoke(
        cli.app,
        ["--credentials", str(tmp_path / "nope.json"), "--token", str(tmp_path / "t.json")],
    )
    assert result.exit_code == 2
