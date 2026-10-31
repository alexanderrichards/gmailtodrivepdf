import copy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pypdf import PdfReader
from typer.testing import CliRunner

from gmailtodrivepdf import cli, drive, gmail, pdf
from gmailtodrivepdf.quotes import without_quotes
from test_gmailtodrivepdf import PNG, b64, raw_message


def thread_service() -> MagicMock:
    service = MagicMock()
    service.users().messages().list.return_value.execute.return_value = {
        "messages": [{"id": "a", "threadId": "t"}, {"id": "b", "threadId": "t"}],
    }
    old = raw_message("old")
    old["internalDate"] = "1700000000000"
    old["payload"] = {
        "mimeType": "text/html",
        "headers": [{"name": "From", "value": "Alice"}, {"name": "Subject", "value": "Conversation"}],
        "body": {"data": b64('<p>First original</p><img src="cid:logo">')},
    }
    new = copy.deepcopy(old)
    new["id"] = "new"
    new["internalDate"] = "1700000100000"
    new["payload"]["headers"][0]["value"] = "Bob"
    new["payload"]["body"]["data"] = b64(
        '<style>p {color:blue}</style><p>Second original</p>'
        '<div class="gmail_quote"><p>First original</p></div>'
    )
    service.users().threads().get.return_value.execute.return_value = {
        "id": "t", "messages": [new, old],
    }
    return service


def test_thread_selection_deduplicates_across_pages_and_labels_and_limits_threads():
    service = thread_service()
    listing = service.users().messages().list
    listing.return_value.execute.side_effect = [
        {"messages": [{"id": "1", "threadId": "a"}, {"id": "2", "threadId": "a"}], "nextPageToken": "next"},
        {"messages": [{"id": "3", "threadId": "b"}]},
    ]
    assert gmail.list_message_ids(service, ["STARRED"], 2, threads=True) == ["a", "b"]
    assert listing.call_args.kwargs["pageToken"] == "next"
    listing.return_value.execute.side_effect = [
        {"messages": [{"id": "1", "threadId": "a"}]},
        {"messages": [{"id": "2", "threadId": "a"}, {"id": "3", "threadId": "b"}]},
    ]
    assert gmail.list_message_ids(service, ["L1", "L2"], threads=True) == ["a", "b"]


def test_get_thread_orders_by_internal_date_and_detects_changes():
    service = thread_service()
    thread = gmail.get_thread(service, "t")
    assert [message.id for message in thread.messages] == ["old", "new"]
    service.users().threads().get.assert_called_with(userId="me", id="t", format="full")
    service.users().threads().get.return_value.execute.assert_called_with(
        num_retries=gmail.GMAIL_REQUEST_RETRIES,
    )
    assert gmail.get_thread(service, "t").revision == thread.revision
    service.users().threads().get.return_value.execute.return_value["messages"][0]["payload"]["body"]["data"] = b64("Changed")
    assert gmail.get_thread(service, "t").revision != thread.revision


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<p>Reply</p><div class="gmail_quote">Old <b>reply</b></div>', "<p>Reply</p>"),
        ('<p>Reply</p><blockquote type="cite">Old</blockquote>', "<p>Reply</p>"),
        ('<blockquote>Meaningful quotation</blockquote><p>Response</p>', '<blockquote>Meaningful quotation</blockquote><p>Response</p>'),
        ('<div class="gmail_quote"><p>Old<br/>reply</p></div><p>New after quote</p>', '<p>New after quote</p>'),
        ('<p>A &amp; B &#163;</p>', '<p>A &amp; B &#163;</p>'),
    ],
)
def test_quote_removal_is_conservative(body, expected):
    original = gmail.EmailMessage(id="x", html_body=body)
    assert without_quotes(original).html_body == expected
    assert original.html_body == body


def test_plain_text_removal_preserves_interleaved_replies():
    original = gmail.EmailMessage(id="x", text_body="Reply\n\nOn Tuesday Alice wrote:\n> Old\n> More")
    assert without_quotes(original).text_body == "Reply"
    original.text_body = "Reply\nOn Tuesday Alice wrote:\n> Old\nMy inline answer\n> More"
    assert without_quotes(original).text_body == original.text_body
    original.text_body = "> A meaningful quotation"
    assert without_quotes(original).text_body == original.text_body


@pytest.mark.parametrize("newest_first", [False, True])
@pytest.mark.parametrize("keep_quotes", [False, True])
def test_thread_pdf_order_quote_handling_and_isolated_images(tmp_path: Path, newest_first, keep_quotes):
    thread = gmail.get_thread(thread_service(), "t")
    thread.messages[0].inline_images["logo"] = ("image/png", PNG)
    thread.messages[1].inline_images["logo"] = ("image/png", PNG)
    thread.messages[1].html_body += '<img src="cid:logo">'
    path = tmp_path / "thread.pdf"
    with pdf.PdfRenderer() as renderer:
        renderer.render_thread(thread, path, newest_first, keep_quotes)
    reader = PdfReader(path)
    assert len(reader.pages) == 2
    texts = [page.extract_text() for page in reader.pages]
    assert ("Second original" in texts[0]) == newest_first
    assert ("First original" in texts[1]) == (newest_first or keep_quotes)
    combined = "\n".join(texts)
    assert combined.count("First original") == (2 if keep_quotes else 1)
    assert "Alice" in combined and "Bob" in combined
    assert all(page.images for page in reader.pages)
    assert len(reader.outline) == 2
    assert not list(tmp_path.glob("*.part"))


def test_thread_render_failure_does_not_replace_existing_pdf(tmp_path: Path, monkeypatch):
    path = tmp_path / "thread.pdf"
    path.write_bytes(b"previous complete PDF")
    real_render = pdf.PdfRenderer.render_pdf

    def fail_second(self, message, *args, **kwargs):
        if message.id == "new":
            raise pdf.PdfRenderError("Failed second message")
        return real_render(self, message, *args, **kwargs)

    monkeypatch.setattr(pdf.PdfRenderer, "render_pdf", fail_second)
    with pdf.PdfRenderer() as renderer, pytest.raises(pdf.PdfRenderError, match="second message"):
        renderer.render_thread(gmail.get_thread(thread_service(), "t"), path)
    assert path.read_bytes() == b"previous complete PDF"
    assert not list(tmp_path.glob("*.part"))


def test_cli_thread_reruns_options_and_new_replies(tmp_path: Path, monkeypatch):
    service = thread_service()
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *args: object())
    monkeypatch.setattr(cli, "build", lambda *args, **kwargs: service)
    args = ["--threads", "-o", str(tmp_path)]
    runner = CliRunner()
    first = runner.invoke(cli.app, args)
    assert first.exit_code == 0, first.output
    assert "Created 1" in first.output
    path = tmp_path / "thread [t].pdf"
    assert path.exists()
    rerun = runner.invoke(cli.app, args)
    assert rerun.exit_code == 0, rerun.output
    assert "skipped 1" in rerun.output
    original = service.users().threads().get.return_value.execute.return_value
    reply = copy.deepcopy(original["messages"][0])
    reply["id"] = "third"
    reply["internalDate"] = "1700000200000"
    reply["payload"]["body"]["data"] = b64("<p>Third original</p>")
    original["messages"].append(reply)
    changed = runner.invoke(cli.app, args)
    assert changed.exit_code == 0, changed.output
    assert "Created 1" in changed.output
    assert len(list(tmp_path.glob("*.pdf"))) == 1
    assert len(PdfReader(path).pages) == 3
    reordered = runner.invoke(cli.app, args + ["--thread-order", "newest-first"])
    assert reordered.exit_code == 0, reordered.output
    assert "Created 1" in reordered.output
    assert "Third original" in PdfReader(path).pages[0].extract_text()
    assert runner.invoke(cli.app, args + ["--keep-quotes"]).exit_code == 0


def test_drive_thread_updates_and_failed_upload_retry(tmp_path: Path):
    path = tmp_path / "thread.pdf"
    path.write_bytes(b"%PDF")
    service = MagicMock()
    files = service.files()
    files.list.return_value.execute.return_value = {"files": [{"id": "drive-thread"}]}
    files.get.return_value.execute.return_value = {"appProperties": {"threadRevision": "old"}}
    files.update.return_value.execute.return_value = {"id": "drive-thread"}
    assert drive.upload_pdf(service, path, "folder", revision="new") == ("drive-thread", True)
    assert files.update.call_args.kwargs["body"]["appProperties"]["threadRevision"] == "new"
    files.get.return_value.execute.return_value = {"appProperties": {"threadRevision": "new"}}
    assert drive.upload_pdf(service, path, "folder", revision="new") == ("drive-thread", False)
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = {"id": "created"}
    assert drive.upload_pdf(service, path, "folder", revision="new") == ("created", True)
    assert files.create.call_args.kwargs["body"]["appProperties"]["threadRevision"] == "new"


def test_cli_thread_retries_drive_upload_even_when_local_pdf_unchanged(tmp_path: Path, monkeypatch):
    service = thread_service()
    drive_service = MagicMock()
    files = drive_service.files()
    files.list.return_value.execute.return_value = {"files": [{"id": "remote"}]}
    files.get.return_value.execute.return_value = {"appProperties": {}}
    files.update.return_value.execute.side_effect = [OSError("Network interrupted"), {"id": "remote"}]
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *args: object())
    monkeypatch.setattr(cli, "build", lambda name, *args, **kwargs: service if name == "gmail" else drive_service)
    monkeypatch.setattr(drive, "get_or_create_folder", lambda *args: "folder")
    args = ["--threads", "--upload-to-drive", "-o", str(tmp_path)]
    runner = CliRunner()
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "Network interrupted" in result.output
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "skipped 1" in result.output
    assert "uploaded 1" in result.output
    revision = files.update.call_args.kwargs["body"]["appProperties"]["threadRevision"]
    files.get.return_value.execute.return_value = {"appProperties": {"threadRevision": revision}}
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "uploaded 0" in result.output
    assert files.update.call_count == 2
    reply = copy.deepcopy(service.users().threads().get.return_value.execute.return_value["messages"][0])
    reply["id"] = "later"
    reply["internalDate"] = "1700000300000"
    service.users().threads().get.return_value.execute.return_value["messages"].append(reply)
    files.update.return_value.execute.side_effect = None
    files.update.return_value.execute.return_value = {"id": "remote"}
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "Created 1" in result.output and "uploaded 1" in result.output
    assert files.update.call_args.kwargs["fileId"] == "remote"


def test_missing_thread_state_regenerates_and_overwrite_forces_update(tmp_path: Path, monkeypatch):
    service = thread_service()
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *args: object())
    monkeypatch.setattr(cli, "build", lambda *args, **kwargs: service)
    runner = CliRunner()
    args = ["--threads", "-o", str(tmp_path)]
    assert runner.invoke(cli.app, args).exit_code == 0
    (tmp_path / "thread [t].thread-state").unlink()
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0 and "Created 1" in result.output
    result = runner.invoke(cli.app, args + ["--overwrite"])
    assert result.exit_code == 0 and "Created 1" in result.output


@pytest.mark.parametrize("option", [["--keep-quotes"], ["--thread-order", "newest-first"]])
def test_thread_only_options_require_threads(option):
    result = CliRunner().invoke(cli.app, option)
    assert result.exit_code == 2
    assert "require --threads" in result.output
