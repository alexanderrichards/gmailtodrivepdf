import base64
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gmailtodrivepdf import cli, drive, gmail, pdf

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


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


def gmail_service(messages: dict[str, dict]) -> MagicMock:
    service = MagicMock()
    msgs = service.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = {
        "messages": [{"id": i} for i in messages]
    }
    msgs.get.side_effect = lambda userId, id, format: MagicMock(
        execute=MagicMock(return_value=messages[id])
    )
    msgs.attachments.return_value.get.return_value.execute.return_value = {
        "data": b64(PNG)
    }
    return service


def test_parse_message_extracts_parts():
    service = gmail_service({"abc123": raw_message()})
    message = gmail.get_message(service, "abc123")

    assert message.subject == "Invoice: Q3/2026 <final>"
    assert message.sender == "Alice <alice@example.com>"
    assert message.date == datetime.fromisoformat("2023-11-14T22:13:20+00:00")
    assert message.text_body == "Hello plain"
    assert "<b>world</b> \u00e9" in message.html_body
    assert message.inline_images["logo@x"] == ("image/png", PNG)
    assert message.attachment_names == ["invoice.pdf"]


def test_list_starred_ids_paginates_and_limits():
    service = MagicMock()
    list_call = service.users.return_value.messages.return_value.list
    list_call.return_value.execute.side_effect = [
        {"messages": [{"id": "1"}, {"id": "2"}], "nextPageToken": "t"},
        {"messages": [{"id": "3"}]},
    ]
    assert list(gmail.list_starred_ids(service)) == ["1", "2", "3"]
    assert list_call.call_args_list[0].kwargs["q"] == "is:starred"

    list_call.return_value.execute.side_effect = [
        {"messages": [{"id": "1"}, {"id": "2"}], "nextPageToken": "t"},
    ]
    assert list(gmail.list_starred_ids(service, max_results=1)) == ["1"]


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


def test_link_callback_blocks_local_and_remote_files():
    message = gmail.EmailMessage(id="x", inline_images={"logo@x": ("image/png", PNG)})
    callback = pdf._make_link_callback(message, allow_remote=False)
    assert callback("cid:logo@x", "").startswith("data:image/png;base64,")
    assert callback("file:///etc/passwd", "") == pdf._BLANK_IMAGE
    assert callback("C:\\secret.png", "") == pdf._BLANK_IMAGE
    assert callback("https://tracker.example/p.gif", "") == pdf._BLANK_IMAGE
    allow = pdf._make_link_callback(message, allow_remote=True)
    assert allow("https://example.com/a.png", "") == "https://example.com/a.png"
    assert allow("http://example.com/a.png", "") == pdf._BLANK_IMAGE


@pytest.mark.parametrize("html_body", [True, False])
def test_render_pdf(tmp_path: Path, html_body: bool):
    message = gmail.parse_message(gmail_service({}), raw_message())
    if not html_body:
        message.html_body = None
    out = pdf.render_pdf(message, tmp_path / "sub" / "out.pdf")
    assert out.read_bytes().startswith(b"%PDF")
    assert not list(tmp_path.rglob("*.part"))


def test_render_pdf_with_lang_and_xml_lang(tmp_path: Path):
    message = gmail.EmailMessage(
        id="x",
        html_body='<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang=\'en\'>'
        "<body><p>Receipt</p></body></html>",
    )
    out = pdf.render_pdf(message, tmp_path / "out.pdf")
    assert out.read_bytes().startswith(b"%PDF")


def test_render_pdf_wraps_parser_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise KeyError("lang")

    monkeypatch.setattr(pdf.pisa, "CreatePDF", boom)
    with pytest.raises(pdf.PdfRenderError, match="KeyError"):
        pdf.render_pdf(gmail.EmailMessage(id="x", text_body="hi"), tmp_path / "out.pdf")
    assert not list(tmp_path.iterdir())


def test_cli_continues_after_render_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bad = raw_message("bad")
    bad["payload"]["headers"][0]["value"] = "Bad"
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *a: object())
    monkeypatch.setattr(
        cli, "build", lambda *a, **k: gmail_service({"bad": bad, "good": raw_message("good")})
    )
    real_create = pdf.pisa.CreatePDF

    def flaky(src, *args, **kwargs):
        if "<h1>Bad</h1>" in src:
            raise KeyError("lang")
        return real_create(src, *args, **kwargs)

    monkeypatch.setattr(pdf.pisa, "CreatePDF", flaky)
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


def test_cli_default_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *a: object())
    monkeypatch.setattr(cli, "build", lambda *a, **k: gmail_service({"m1": raw_message("m1")}))
    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "pdfs").glob("*.pdf"))) == 1


def test_cli_missing_credentials(tmp_path: Path):
    result = CliRunner().invoke(
        cli.app,
        ["--credentials", str(tmp_path / "nope.json"), "--token", str(tmp_path / "t.json")],
    )
    assert result.exit_code == 2
