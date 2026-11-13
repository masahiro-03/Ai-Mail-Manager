import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from urllib.parse import quote

import httplib2
from bs4 import BeautifulSoup
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAIL_SCOPE_QUERIES = {
    "受信トレイのすべて": "in:inbox",
    "直近7日間のメール": "in:inbox newer_than:7d",
    "直近30日間のメール": "in:inbox newer_than:30d",
    "未読メールのみ": "in:inbox is:unread",
    "スター付きメール": "is:starred",
    "すべてのメール": "in:anywhere",
}
ROOT = Path(__file__).resolve().parents[1]
# The library logs the callback request line, which can contain the OAuth code.
logging.getLogger("google_auth_oauthlib.flow").setLevel(logging.WARNING)


def gmail_query_for(scope):
    """Return the Gmail API search query for a user-facing mail scope."""
    return MAIL_SCOPE_QUERIES[scope]


def connect():
    flow = InstalledAppFlow.from_client_secrets_file(
        str(ROOT / "credentials.json"), SCOPES, autogenerate_code_verifier=True
    )
    return flow.run_local_server(
        host="127.0.0.1", port=0, timeout_seconds=120,
        authorization_prompt_message=None,
        success_message="認証処理が完了しました。アプリ画面に戻ってください。",
    )


def service_for(credentials):
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=20))
    return build("gmail", "v1", http=http, cache_discovery=False)


def gmail_links(thread_id, message_id_header):
    base = "https://mail.google.com/mail/u/0/"
    direct = base + "#all/" + quote(thread_id, safe="") if thread_id else ""
    rfc_id = message_id_header.strip().strip("<>")
    search = ""
    if re.fullmatch(r"[^\s<>@]+@[^\s<>@]+", rfc_id):
        search = base + "#search/" + quote(f"rfc822msgid:{rfc_id}", safe="")
    return direct or search, search


def headers(part):
    return {h["name"].lower(): h["value"] for h in part.get("headers", [])}


def decode_header_text(value):
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        return value


def html_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("script, style, head, template, noscript, iframe, [hidden]"):
        element.decompose()
    for element in soup.select("br, p, div, li, tr"):
        element.append("\n")
    return "\n".join(line.strip() for line in soup.get_text().splitlines() if line.strip())


def body_options(part):
    meta = headers(part)
    disposition = meta.get("content-disposition", "").split(";", 1)[0].strip().lower()
    if part.get("filename") or disposition == "attachment":
        return
    mime = part.get("mimeType", "").lower()
    if mime.startswith("multipart/"):
        for child in part.get("parts", []):
            yield from body_options(child)
    if mime not in ("text/plain", "text/html"):
        return
    body = part.get("body", {})
    data = body.get("data")
    if body.get("attachmentId") or not data:
        return
    content_type = Message()
    content_type["Content-Type"] = meta.get("content-type", mime)
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        value = raw.decode(content_type.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        value = raw.decode("utf-8", errors="replace")
    yield mime, (html_text(value) if mime == "text/html" else value).strip()


@dataclass(frozen=True)
class Mail:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    received_at: str
    body: str
    gmail_link: str
    search_link: str


def parse_message(raw):
    payload = raw["payload"]
    meta = headers(payload)
    options = list(body_options(payload))
    body = next((text for mime, text in options if mime == "text/plain" and text), "")
    if not body:
        body = next((text for mime, text in options if mime == "text/html" and text), "")
    thread_id = raw.get("threadId", "")
    direct, search = gmail_links(thread_id, meta.get("message-id", ""))
    received = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc)
    return Mail(raw["id"], thread_id, decode_header_text(meta.get("from", "")),
                decode_header_text(meta.get("subject", "")), received.isoformat(),
                body, direct, search)
