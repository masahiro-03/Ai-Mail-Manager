from dataclasses import dataclass
from threading import Lock

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError

from app.gmail import parse_message

SYNC_LOCK = Lock()
LIMITS = (20, 50, 100)


class SyncError(Exception):
    pass


class AuthError(SyncError):
    pass


@dataclass
class SyncReport:
    added: int = 0
    existing: int = 0
    unavailable: int = 0
    fallback: int = 0


def execute(request):
    try:
        return request.execute(num_retries=1)
    except RefreshError as exc:
        raise AuthError("Gmail認証が失効しました。接続し直してください。") from exc
    except HttpError as exc:
        status = exc.resp.status
        if status == 401:
            raise AuthError("Gmail認証が失効しました。接続し直してください。") from exc
        if status == 404:
            raise
        raise SyncError(f"Gmail APIエラー（HTTP {status}）。権限、API設定、利用上限をご確認ください。") from exc
    except (TransportError, httplib2.HttpLib2Error, OSError) as exc:
        raise SyncError("Gmailに接続できませんでした。ネットワークを確認して再試行してください。") from exc


def mailbox_account(service):
    try:
        return execute(service.users().getProfile(userId="me"))["emailAddress"].casefold()
    except HttpError as exc:
        raise SyncError("Gmailのメールボックスを確認できませんでした。") from exc


def sync(service, store, account, query, limit, classifier):
    if limit not in LIMITS:
        raise ValueError("未対応の取得件数")
    report = SyncReport()
    with SYNC_LOCK:
        messages = service.users().messages()
        remaining = limit
        token = None
        seen = set()
        while remaining:
            try:
                page = execute(messages.list(userId="me", q=query, maxResults=remaining,
                                             pageToken=token))
            except HttpError as exc:
                raise SyncError("Gmailのメール一覧を取得できませんでした。") from exc
            for item in page.get("messages", [])[:remaining]:
                message_id = item["id"]
                if message_id in seen:
                    continue
                seen.add(message_id)
                if store.contains(account, message_id):
                    report.existing += 1
                    continue
                try:
                    raw = execute(messages.get(userId="me", id=message_id, format="full"))
                    mail = parse_message(raw)
                except (HttpError, KeyError, ValueError, TypeError, OverflowError):
                    report.unavailable += 1
                    continue
                result = classifier(mail.subject, mail.body)
                if store.add(account, mail, result):
                    report.added += 1
                    report.fallback += result.method == "rule_fallback"
            remaining = limit - len(seen)
            next_token = page.get("nextPageToken")
            if not next_token or token == next_token:
                break
            token = next_token
        store.finish(account)
    return report
