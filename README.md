# gmailtodrivepdf

A command-line tool that converts every **starred** email in your Gmail account (or,
optionally, every email with chosen labels) into a PDF file. It can also upload the
PDFs to a `pdfs` folder in Google Drive.

It uses these public Google APIs:

- **Gmail API** (`gmail.readonly` scope) to find starred (or labelled) messages and read them.
- **Google Drive API** (`drive.file` scope, only requested with `--upload-to-drive`)
  to create the folder and upload files. This scope only lets the app see files
  it created itself.

The PDFs are rendered locally with [xhtml2pdf](https://github.com/xhtml2pdf/xhtml2pdf).
Each PDF has a header with the subject, sender, recipients, date and attachment names,
followed by the email body (HTML if available, otherwise plain text). Inline (`cid:`)
images are embedded. By default, remote images are blocked because they are often
tracking pixels. Local file references are always blocked.

## Google Cloud setup (one-off)

1. Create or select a project in the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Gmail API** and the **Google Drive API** under *APIs & Services → Library*.
3. Configure the **OAuth consent screen** (the *External* user type is fine). Add your
   Google account as a test user.
4. Under *APIs & Services → Credentials*, create an **OAuth client ID** with the
   application type **Desktop app**. Download the JSON file.
5. Save the file as `credentials.json` in the app's configuration directory, or pass
   its location with `--credentials`:
   - Windows: `%APPDATA%\gmailtodrivepdf\credentials.json`
   - macOS: `~/Library/Application Support/gmailtodrivepdf/credentials.json`
   - Linux: `~/.config/gmailtodrivepdf/credentials.json`

The first time you run the tool, it opens a browser window so you can grant access.
The resulting token is cached as `token.json` in the same directory. Keep both files
private and never commit them to version control.

## Installation

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```shell
uv sync
```

## Usage

```shell
# Write PDFs of all starred emails to ./pdfs
uv run gmailtodrivepdf

# Use a different output directory
uv run gmailtodrivepdf --output-dir ~/Documents/starred

# Also upload to a "pdfs" folder in Google Drive
uv run gmailtodrivepdf --upload-to-drive

# Convert emails labelled "Receipts" or "Work/Invoices" instead of starred ones
uv run gmailtodrivepdf --labels "Receipts,Work/Invoices"

# Upload to a differently named Drive folder, processing only the 10 most recent
uv run gmailtodrivepdf -u --drive-folder "Starred emails" -n 10
```

| Option | Description |
| --- | --- |
| `-o`, `--output-dir` | Directory for the PDFs (default: `./pdfs`). |
| `-u`, `--upload-to-drive` | Also upload the PDFs to Google Drive. |
| `--drive-folder` | Name of the Drive folder in *My Drive* (default: `pdfs`). It is created if it does not exist. |
| `-l`, `--labels` | Comma-separated Gmail labels. Converts emails with **any** of these labels instead of starred emails. Names are case-insensitive; nested labels use `/` (e.g. `Work/Invoices`); system labels such as `INBOX` or `IMPORTANT` also work. Unknown labels are reported with a list of your available labels. |
| `-n`, `--max-results` | Process at most this many emails. |
| `--overwrite` | Regenerate PDFs that already exist locally and replace them in Drive. |
| `--allow-remote-images` | Download remote HTTPS images referenced in emails. |
| `--credentials` | Path to the OAuth client secrets JSON file. |
| `--token` | Path to the cached OAuth token. |

Files are named `YYYY-MM-DD <subject> [<message id>].pdf`, so reruns skip emails that
have already been converted or uploaded.

## Development

```shell
uv run pytest
```
