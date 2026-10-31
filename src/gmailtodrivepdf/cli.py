"""Command-line interface."""

from pathlib import Path
from typing import Annotated

import typer
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import auth, drive, gmail, pdf

app = typer.Typer(
    help="Convert starred Gmail messages to PDF files, optionally uploading them "
    "to Google Drive.",
    no_args_is_help=False,
)


@app.command()
def main(
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory to write PDF files to.",
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("pdfs"),
    upload_to_drive: Annotated[
        bool,
        typer.Option(
            "--upload-to-drive",
            "-u",
            help="Also upload the PDFs to a folder in Google Drive.",
        ),
    ] = False,
    drive_folder: Annotated[
        str,
        typer.Option(help="Name of the Google Drive folder (in My Drive) to upload to."),
    ] = "pdfs",
    max_results: Annotated[
        int | None,
        typer.Option(
            "--max-results", "-n", min=1, help="Maximum number of starred emails to process."
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(help="Re-create PDFs that already exist locally or in Drive."),
    ] = False,
    allow_remote_images: Annotated[
        bool,
        typer.Option(
            help="Download remote (HTTPS) images referenced by emails. Disabled by "
            "default as these are often tracking pixels.",
        ),
    ] = False,
    credentials: Annotated[
        Path,
        typer.Option(
            help="Path to the Google OAuth client secrets JSON file.",
            dir_okay=False,
        ),
    ] = auth.default_credentials_path(),
    token: Annotated[
        Path,
        typer.Option(help="Path at which to cache the OAuth token.", dir_okay=False),
    ] = auth.default_token_path(),
) -> None:
    """Save every starred email in your Gmail account as a PDF."""
    scopes = auth.GMAIL_SCOPES + (auth.DRIVE_SCOPES if upload_to_drive else [])
    try:
        creds = auth.get_credentials(scopes, credentials, token)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    drive_service = None
    folder_id = None
    if upload_to_drive:
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        folder_id = drive.get_or_create_folder(drive_service, drive_folder)

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        message_ids = list(gmail.list_starred_ids(gmail_service, max_results))
    except HttpError as exc:
        typer.secho(f"Failed to list starred emails: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not message_ids:
        typer.echo("No starred emails found.")
        return

    typer.echo(f"Found {len(message_ids)} starred email(s).")
    created = skipped = uploaded = failed = 0
    with typer.progressbar(message_ids, label="Processing") as progress:
        for message_id in progress:
            try:
                message = gmail.get_message(gmail_service, message_id)
                path = output_dir / pdf.safe_filename(message)
                if path.exists() and not overwrite:
                    skipped += 1
                else:
                    pdf.render_pdf(message, path, allow_remote_images)
                    created += 1
                if drive_service is not None and folder_id is not None:
                    _, did_upload = drive.upload_pdf(
                        drive_service, path, folder_id, overwrite
                    )
                    uploaded += did_upload
            except (HttpError, pdf.PdfRenderError, OSError) as exc:
                failed += 1
                typer.secho(f"\nError processing {message_id}: {exc}", fg=typer.colors.RED, err=True)

    summary = f"Created {created}, skipped {skipped} existing PDF(s) in {output_dir}"
    if upload_to_drive:
        summary += f"; uploaded {uploaded} to Drive folder '{drive_folder}'"
    if failed:
        summary += f"; {failed} failed"
    typer.secho(summary + ".", fg=typer.colors.YELLOW if failed else typer.colors.GREEN)
    if failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
