import copy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pypdf import PdfWriter
from typer.testing import CliRunner

from gmailtodrivepdf import cli, drive, gmail, pdf
from gmailtodrivepdf.thread_files import ThreadFiles, subject_stem
from test_threads import thread_service


def saved_thread(path: Path, id: str, subject: str, revision: str = "revision") -> None:
    with PdfWriter() as writer:
        writer.add_blank_page(595, 842)
        writer.add_metadata({
            pdf.THREAD_ID_KEY: id,
            pdf.THREAD_SUBJECT_KEY: subject,
            pdf.THREAD_REVISION_KEY: revision,
        })
        writer.write(str(path))


@pytest.mark.parametrize(
    ("subject", "expected"),
    [("Invoice: Q3/2026", "Invoice_ Q3_2026"), ("  ...  ", "no subject"),
     ("", "no subject"), ("CON", "_CON"), ("nul.txt", "_nul.txt"),
     ("Receipts   and\tInvoices", "Receipts and Invoices"), ("a" * 200, "a" * 120)],
)
def test_subject_stem(subject, expected):
    assert subject_stem(subject) == expected


def test_second_collision_renames_first_and_metadata_locates_both_after_restart(tmp_path: Path):
    registry = ThreadFiles(tmp_path)
    path = registry.assign("first", "Invoice")["first"]
    assert path.name == "Invoice.pdf"
    saved_thread(path, "first", "Invoice")
    registry.saved("first", path)
    names = registry.assign("second", "invoice")
    assert names["first"].name == "Invoice [first].pdf"
    assert names["second"].name == "invoice [second].pdf"
    assert not path.exists()
    assert pdf.read_thread_metadata(names["first"])[pdf.THREAD_ID_KEY] == "first"
    saved_thread(names["second"], "second", "invoice")
    registry.saved("second", names["second"])
    restarted = ThreadFiles(tmp_path)
    assert restarted.assign("first", "Invoice") == names
    assert len(list(tmp_path.glob("*.pdf"))) == 2


def test_sanitised_and_truncated_subjects_collide(tmp_path: Path):
    registry = ThreadFiles(tmp_path)
    assert registry.assign("first", "A/B")["first"].name == "A_B.pdf"
    names = registry.assign("second", "A:B")
    assert {path.name for path in names.values()} == {"A_B [first].pdf", "A_B [second].pdf"}
    registry = ThreadFiles(tmp_path)
    registry.assign("first", "a" * 121)
    names = registry.assign("second", "a" * 120 + "b")
    assert all(f"[{id}]" in path.name for id, path in names.items())


def test_user_files_and_secondary_collisions_are_not_overwritten(tmp_path: Path):
    user_file = tmp_path / "Invoice.pdf"
    with PdfWriter() as writer:
        writer.add_blank_page(595, 842)
        writer.write(str(user_file))
    before = user_file.read_bytes()
    registry = ThreadFiles(tmp_path)
    assert registry.assign("a", "Invoice")["a"].name == "Invoice [a].pdf"
    names = registry.assign("b", "Invoice [a]")
    assert names["b"].name == "Invoice [a] [b].pdf"
    assert user_file.read_bytes() == before


def test_changed_subject_and_manually_renamed_export(tmp_path: Path):
    path = tmp_path / "custom.pdf"
    saved_thread(path, "a", "Original")
    registry = ThreadFiles(tmp_path)
    names = registry.assign("a", "New subject")
    assert names["a"].name == "New subject.pdf"
    assert not path.exists()
    assert pdf.read_thread_metadata(names["a"])[pdf.THREAD_ID_KEY] == "a"


def test_duplicate_thread_metadata_is_an_error(tmp_path: Path):
    saved_thread(tmp_path / "a.pdf", "same", "Invoice")
    saved_thread(tmp_path / "b.pdf", "same", "Invoice")
    with pytest.raises(ValueError, match="Multiple local PDFs"):
        ThreadFiles(tmp_path)


def test_failed_collision_rename_restores_both_original_files(tmp_path: Path, monkeypatch):
    first = tmp_path / "one.pdf"
    second = tmp_path / "two.pdf"
    saved_thread(first, "a", "Invoice")
    saved_thread(second, "b", "Invoice")
    registry = ThreadFiles(tmp_path)
    original_rename = Path.rename
    failed = False

    def fail_one_rename(self, target):
        nonlocal failed
        if not failed and self.suffix == ".part" and Path(target).name == "Invoice [b].pdf":
            failed = True
            raise PermissionError("Rename denied")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_one_rename)
    with pytest.raises(PermissionError, match="Rename denied"):
        registry.assign("a", "Invoice")
    assert pdf.read_thread_metadata(first)[pdf.THREAD_ID_KEY] == "a"
    assert pdf.read_thread_metadata(second)[pdf.THREAD_ID_KEY] == "b"
    assert not list(tmp_path.glob("*.part"))
    assert registry.entries["a"].path == first
    assert registry.entries["b"].path == second


def test_remote_only_subject_collision(tmp_path: Path):
    names = ThreadFiles(tmp_path, {"remote": "Invoice"}).assign("local", "Invoice")
    assert names["remote"].name == "Invoice [remote].pdf"
    assert names["local"].name == "Invoice [local].pdf"


def test_drive_lookup_uses_id_and_renames_without_reuploading(tmp_path: Path):
    path = tmp_path / "Invoice [thread].pdf"
    saved_thread(path, "thread", "Invoice")
    service = MagicMock()
    files = service.files()
    files.list.return_value.execute.return_value = {"files": [{
        "id": "drive-id", "name": "Invoice.pdf", "description": "Invoice",
        "appProperties": {"threadId": "thread"},
    }]}
    files.get.return_value.execute.return_value = {"appProperties": {"threadRevision": "same"}}
    assert drive.upload_pdf(
        service, path, "folder", revision="same", thread_id="thread", thread_subject="Invoice",
    ) == ("drive-id", False)
    files.update.assert_called_once_with(
        fileId="drive-id", body={"name": path.name}, fields="id",
    )


def test_drive_thread_listing_paginates_and_filters_unmanaged():
    service = MagicMock()
    files = service.files()
    files.list.return_value.execute.side_effect = [
        {"files": [{"id": "ordinary"}], "nextPageToken": "next"},
        {"files": [{"id": "thread", "appProperties": {"threadId": "a"}}]},
    ]
    assert drive.list_thread_files(service, "folder") == [
        {"id": "thread", "appProperties": {"threadId": "a"}},
    ]
    assert files.list.call_args.kwargs["pageToken"] == "next"


def test_cli_same_subject_collision_and_subject_change(tmp_path: Path, monkeypatch):
    service = thread_service()
    first = service.users().threads().get.return_value.execute.return_value
    second = copy.deepcopy(first)
    second["id"] = "other"
    for message in second["messages"]:
        message["id"] += "-other"
    service.users().threads().get.side_effect = lambda userId, id, format: MagicMock(
        execute=MagicMock(return_value=first if id == "t" else second),
    )
    service.users().messages().list.return_value.execute.return_value = {
        "messages": [{"id": "1", "threadId": "t"}, {"id": "2", "threadId": "other"}],
    }
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *args: object())
    monkeypatch.setattr(cli, "build", lambda *args, **kwargs: service)
    runner = CliRunner()
    args = ["--threads", "-o", str(tmp_path)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert {path.name for path in tmp_path.glob("*.pdf")} == {
        "Conversation [t].pdf", "Conversation [other].pdf",
    }
    for path in tmp_path.glob("*.pdf"):
        metadata = pdf.read_thread_metadata(path)
        assert metadata[pdf.THREAD_ID_KEY] in {"t", "other"}
    rerun = runner.invoke(cli.app, args)
    assert rerun.exit_code == 0 and "skipped 2" in rerun.output
    # Oldest message title, not the newest reply's subject, controls the name.
    first["messages"][0]["payload"]["headers"][1]["value"] = "Newest subject"
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "Conversation [t].pdf").exists()
    first["messages"][1]["payload"]["headers"][1]["value"] = "New conversation title"
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert {path.name for path in tmp_path.glob("*.pdf")} == {
        "New conversation title.pdf", "Conversation.pdf",
    }


def test_cli_drive_collision_renames_first_export_and_reuses_ids(tmp_path: Path, monkeypatch):
    service = thread_service()
    first = service.users().threads().get.return_value.execute.return_value
    second = copy.deepcopy(first)
    second["id"] = "other"
    for message in second["messages"]:
        message["id"] += "-other"
    service.users().threads().get.side_effect = lambda userId, id, format: MagicMock(
        execute=MagicMock(return_value=first if id == "t" else second),
    )
    listing = service.users().messages().list.return_value.execute
    listing.return_value = {"messages": [{"id": "1", "threadId": "t"}]}
    remote = {}
    drive_service = MagicMock()
    files = drive_service.files()
    files.list.side_effect = lambda **kwargs: MagicMock(
        execute=MagicMock(side_effect=lambda: {"files": copy.deepcopy(list(remote.values()))}),
    )
    files.get.side_effect = lambda fileId, **kwargs: MagicMock(
        execute=MagicMock(side_effect=lambda: copy.deepcopy(remote[fileId])),
    )

    def create(body, **kwargs):
        def execute():
            id = f"remote-{len(remote)}"
            remote[id] = {"id": id, **copy.deepcopy(body)}
            return {"id": id}
        return MagicMock(execute=MagicMock(side_effect=execute))

    def update(fileId, body, **kwargs):
        def execute():
            remote[fileId].update(copy.deepcopy(body))
            return {"id": fileId}
        return MagicMock(execute=MagicMock(side_effect=execute))

    files.create.side_effect = create
    files.update.side_effect = update
    monkeypatch.setattr(cli.auth, "get_credentials", lambda *args: object())
    monkeypatch.setattr(cli, "build", lambda name, *args, **kwargs: service if name == "gmail" else drive_service)
    monkeypatch.setattr(drive, "get_or_create_folder", lambda *args: "folder")
    runner = CliRunner()
    args = ["--threads", "--upload-to-drive", "-o", str(tmp_path)]
    assert runner.invoke(cli.app, args).exit_code == 0
    assert remote["remote-0"]["name"] == "Conversation.pdf"
    listing.return_value["messages"].append({"id": "2", "threadId": "other"})
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert {file["name"] for file in remote.values()} == {
        "Conversation [t].pdf", "Conversation [other].pdf",
    }
    assert remote["remote-0"]["appProperties"]["threadId"] == "t"
    create_count = files.create.call_count
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "skipped 2" in result.output and "uploaded 0" in result.output
    assert files.create.call_count == create_count
    first["messages"][1]["payload"]["headers"][1]["value"] = "Changed title"
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert remote["remote-0"]["name"] == "Changed title.pdf"
    assert remote["remote-1"]["name"] == "Conversation.pdf"
    assert files.create.call_count == create_count
