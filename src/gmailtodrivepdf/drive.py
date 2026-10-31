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


def upload_pdf(
    service: Any, path: Path, folder_id: str, overwrite: bool = False
) -> tuple[str, bool]:
    """Upload ``path`` to the folder.

    Returns ``(file_id, uploaded)``; ``uploaded`` is False when an existing file was
    left untouched.
    """
    existing = find_file(service, path.name, folder_id)
    if existing and not overwrite:
        return existing, False

    media = MediaFileUpload(str(path), mimetype="application/pdf", resumable=True)
    if existing:
        result = (
            service.files().update(fileId=existing, media_body=media, fields="id").execute()
        )
    else:
        result = (
            service.files()
            .create(
                body={"name": path.name, "parents": [folder_id]},
                media_body=media,
                fields="id",
            )
            .execute()
        )
    return result["id"], True
