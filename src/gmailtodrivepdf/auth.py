"""OAuth 2.0 authentication against Google APIs."""

from pathlib import Path

import typer
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

APP_NAME = "gmailtodrivepdf"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
# drive.file only grants access to files/folders created by this app.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def default_config_dir() -> Path:
    return Path(typer.get_app_dir(APP_NAME))


def default_credentials_path() -> Path:
    return default_config_dir() / "credentials.json"


def default_token_path() -> Path:
    return default_config_dir() / "token.json"


def get_credentials(
    scopes: list[str], credentials_path: Path, token_path: Path
) -> Credentials:
    """Load cached credentials, refreshing or re-running the OAuth flow as needed."""
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path))
        if not creds.has_scopes(scopes):
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None

    if not creds or not creds.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"OAuth client secrets file not found at '{credentials_path}'. "
                "Create a Desktop OAuth client in Google Cloud Console and "
                "download its JSON (see README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return creds
