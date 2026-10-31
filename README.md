# gmailtodrivepdf

A command-line tool that converts every **starred** email in your Gmail account (or,
optionally, every email with chosen labels) into a PDF file. It can also upload the
PDFs to a `pdfs` folder in Google Drive.

It uses these public Google APIs:

- **Gmail API** (`gmail.readonly` scope) to find starred (or labelled) messages and read them.
- **Google Drive API** (`drive.file` scope, only requested with `--upload-to-drive`)
  to create the folder and upload files. This scope only lets the app see files
  it created itself.

The PDFs are rendered locally with headless Chromium through
[Playwright](https://playwright.dev/python/).
Each PDF has a header with the subject, sender, recipients, date and attachment names,
followed by the email body (HTML if available, otherwise plain text). Inline (`cid:`)
images are embedded. By default, remote images are blocked because they are often
tracking pixels. JavaScript, local file references, frames, external stylesheets
and external fonts are blocked. `--allow-remote-images` permits HTTPS image requests
only; enabling it may let senders track that an email was processed.

Chromium preserves HTML styling and prints paginated A4 PDFs with backgrounds.
One browser is reused throughout a run, with an isolated context for each email.
Rendering failures are reported and do not replace existing PDFs with partial files.
There is no automatic text-only fallback. Plain-text-only emails are still supported.

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
uv run playwright install chromium
```

The second command downloads Playwright's browser (several hundred megabytes);
Google Chrome does not need to be installed. Repeat it after updating Playwright.
On Linux, `uv run playwright install --with-deps chromium` also installs the
required system libraries and may need administrator privileges.

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

# Export complete conversations oldest first, removing recognisable reply history
uv run gmailtodrivepdf --threads

# Export newest first instead, retaining quoted history
uv run gmailtodrivepdf --threads --thread-order newest-first --keep-quotes

# Upload to a differently named Drive folder, processing only the 10 most recent
uv run gmailtodrivepdf -u --drive-folder "Starred emails" -n 10
```

| Option | Description |
| --- | --- |
| `-o`, `--output-dir` | Directory for the PDFs (default: `./pdfs`). |
| `-u`, `--upload-to-drive` | Also upload the PDFs to Google Drive. |
| `--drive-folder` | Name of the Drive folder in *My Drive* (default: `pdfs`). It is created if it does not exist. |
| `-l`, `--labels` | Comma-separated Gmail labels. Converts emails with **any** of these labels instead of starred emails. Names are case-insensitive; nested labels use `/` (e.g. `Work/Invoices`); system labels such as `INBOX` or `IMPORTANT` also work. Unknown labels are reported with a list of your available labels. |
| `-n`, `--max-results` | Process at most this many emails, or distinct conversations with `--threads`. |
| `--threads` | Export all messages in each matching conversation into one PDF. |
| `--thread-order` | `oldest-first` (default) or `newest-first`; used with `--threads`. |
| `--keep-quotes` | Retain quoted reply history in thread PDFs instead of best-effort removal. Requires `--threads`. |
| `--overwrite` | Regenerate PDFs that already exist locally and replace them in Drive. |
| `--allow-remote-images` | Download remote HTTPS images referenced in emails. |
| `--credentials` | Path to the OAuth client secrets JSON file. |
| `--token` | Path to the cached OAuth token. |

Files are named `YYYY-MM-DD <subject> [<message id>].pdf`, so reruns skip emails that
have already been converted or uploaded.

### Conversation exports

Without `--threads`, message-by-message export is unchanged, including quoted
history. In thread mode, a conversation is selected when at least one message
matches your starred/label filters. All messages returned by Gmail for that thread
are exported, including unstarred replies and your sent messages. Conversations
are deduplicated across labels; `--max-results` limits conversations.

Messages are ordered by Gmail's internal timestamp, oldest first by default.
Each begins on a new page with its own subject, sender, recipients, date and
attachment list. Each message is rendered in isolation before its pages are merged,
preserving its HTML styling and inline images without leaking CSS between messages.
PDF bookmarks identify the messages. Regular attachment contents are not exported.

Quote removal is deliberately conservative: explicitly marked `gmail_quote`
containers and `blockquote type="cite"` blocks are removed from HTML. Plain-text
history is removed only for a trailing quoted run following an `On ... wrote:`
header. Unrecognised history, generic blockquotes and interleaved plain-text replies
are retained. Replies written inside explicitly marked HTML quote containers may
be removed too; use `--keep-quotes` when retaining all content matters.

Conversation PDFs have stable names: `thread [<thread id>].pdf`. Their custom
`/GmailToDriveThreadRevision` metadata stores the revision hash and tracks the
exported messages and rendering options without separate state files.
New or changed messages, changed ordering/quote/image options, or missing revision
metadata cause regeneration; unchanged conversations are skipped. PDF editors may
remove this metadata, which causes regeneration on the next run.
Unreadable PDFs are reported as errors; use
`--overwrite` to replace them.
Drive exports track their revision independently and replace outdated thread PDFs
without needing `--overwrite`, including retrying previously failed uploads.
`--overwrite` forces regeneration and replacement.

## Troubleshooting

- **`Could not start Chromium`:** run `uv run playwright install chromium`.
  If the browser is already installed, check the accompanying browser error for
  missing system libraries or sandbox restrictions.
- **Earlier `LayoutError` / `BlockBox` / `PmlKeepInFrame` errors:** these came from
  xhtml2pdf, which has now been replaced. Retry those emails with Chromium. Use
  `--overwrite` to regenerate previously saved text-only recovery PDFs.
  Browser printing is more compatible with email HTML, but unusually large or
  fixed-size content can still need adjustments.
- **Gmail `403 rateLimitExceeded` or `userRateLimitExceeded`:** Gmail limits API
  usage over time, and reading messages and downloading inline images each consume
  quota. All Gmail read requests now use Google's built-in exponential backoff
  with jitter, with up to seven retries. You may see retry warnings and a pause
  while the quota recovers. `429` and transient server errors are also retried.
  If retries are exhausted, the error is still reported; avoid simultaneous runs
  against the same account and try again later. Permission errors and daily quota
  errors are not retried, since waiting briefly does not resolve them.

After updating, rerun the same command to retry failed emails. Successfully saved
PDFs are skipped unless you use `--overwrite`.

## Development

```shell
uv run pytest
```
