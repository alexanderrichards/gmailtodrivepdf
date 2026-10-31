"""Uploading files to Google Drive."""

from pathlib import Path
from typing import Any

from googleapiclient.http import MediaFileUpload

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def get_or_create_folder(service: Any, name: str, parent_id: str = "root") -> str:
    """Return the ID of a folder called ``name`` under ``parent_id``, creating it if needed."""
    query = (
        f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME_TYPE}' "
        f"and '{_escape(parent_id)}' in parents and trashed = false"
    )
    response = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
        .execute()
    )
    files = response.get("files", [])
    if files:
        return files[0]["id"]

    folder = (
        service.files()
        .create(
            body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
            fields="id",
        )
        .execute()
    )
    return folder["id"]


def find_file(service: Any, name: str, folder_id: str) -> str | None:
    query = (
        f"name = '{_escape(name)}' and '{_escape(folder_id)}' in parents "
        "and trashed = false"
    )
    response = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id)", pageSize=1)
        .execute()
    )
    files = response.get("files", [])
    return files[0]["id"] if files else None


def list_thread_files(service: Any, folder_id: str) -> list[dict[str, Any]]:
    query = f"'{_escape(folder_id)}' in parents and trashed = false"
    files: list[dict[str, Any]] = []
    token = None
    while True:
        response = service.files().list(
            q=query, spaces="drive", fields="nextPageToken,files(id,name,description,appProperties)",
            pageSize=1000, pageToken=token,
        ).execute()
        files.extend(
            file for file in response.get("files", [])
            if file.get("appProperties", {}).get("threadId")
        )
        token = response.get("nextPageToken")
        if not token:
            return files


def rename_thread_files(service: Any, files: list[dict[str, Any]], names: dict[str, Path]) -> None:
    for file in files:
        id = file["appProperties"]["threadId"]
        if id in names and file["name"] != names[id].name:
            service.files().update(
                fileId=file["id"], body={"name": names[id].name}, fields="id",
            ).execute()
            file["name"] = names[id].name


def upload_pdf(
    service: Any, path: Path, folder_id: str, overwrite: bool = False,
    revision: str | None = None,
    thread_id: str | None = None,
    thread_subject: str | None = None,
) -> tuple[str, bool]:
    """Upload ``path`` to the folder.

    Returns ``(file_id, uploaded)``; ``uploaded`` is False when an existing file was
    left untouched.
    """
    existing = None
    if thread_id is not None:
        matches = [
            file for file in list_thread_files(service, folder_id)
            if file["appProperties"]["threadId"] == thread_id
        ]
        if len(matches) > 1:
            raise ValueError(f"Multiple Drive PDFs claim Gmail thread {thread_id}")
        if matches:
            existing = matches[0]["id"]
            if matches[0]["name"] != path.name:
                service.files().update(
                    fileId=existing, body={"name": path.name}, fields="id",
                ).execute()
    else:
        existing = find_file(service, path.name, folder_id)
    if existing and not overwrite:
        if revision is None:
            return existing, False
        metadata = service.files().get(fileId=existing, fields="appProperties").execute()
        if metadata.get("appProperties", {}).get("threadRevision") == revision:
            return existing, False

    media = MediaFileUpload(str(path), mimetype="application/pdf", resumable=True)
    properties = {}
    if revision is not None:
        properties["threadRevision"] = revision
    if thread_id is not None:
        properties["threadId"] = thread_id
    try:
        if existing:
            options: dict[str, Any] = {}
            if properties:
                options["body"] = {"name": path.name, "appProperties": properties}
                if thread_id is not None:
                    options["body"]["description"] = thread_subject or ""
            result = service.files().update(
                fileId=existing, media_body=media, fields="id", **options,
            ).execute()
        else:
            body: dict[str, Any] = {"name": path.name, "parents": [folder_id]}
            if properties:
                body["appProperties"] = properties
            if thread_id is not None:
                body["description"] = thread_subject or ""
            result = (
                service.files()
                .create(
                    body=body,
                    media_body=media,
                    fields="id",
                )
                .execute()
            )
    finally:
        # Release Windows file locks, including when an upload fails.
        media.stream().close()
    return result["id"], True
