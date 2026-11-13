import base64
import inspect
import runpy
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import httpx
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from openai import APIConnectionError

from app import classification, gmail
from app.config import is_public_demo
from app.classification import CATEGORIES, Classification, Classifier, rules
from app.gmail import MAIL_SCOPE_QUERIES, gmail_links, gmail_query_for, parse_message
from app.storage import Store
from app.sync import AuthError, SyncError, mailbox_account, sync
from app.ui import (
    CATEGORY_BY_DISPLAY_NAME,
    CATEGORY_OPTIONS,
    category_display_name,
    category_internal_name,
    dashboard,
    filter_rows,
    safe_csv_cell,
    sort_rows_by_received_at,
)


def part(mime, body, charset="utf-8", **extra):
    return {
        "mimeType": mime,
        "headers": [{"name": "Content-Type", "value": f"{mime}; charset={charset}"}],
        "body": {"data": base64.urlsafe_b64encode(body.encode(charset)).decode().rstrip("=")},
        **extra,
    }


def message(*parts, mid="abc123"):
    return {
        "id": mid, "threadId": "def456", "internalDate": "1704067200000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "担当 <sample@example.com>"},
                {"name": "Subject", "value": "=?UTF-8?B?6KuL5rGC?="},
                {"name": "Message-ID", "value": "<test+id@example.com>"},
            ],
            "parts": [{"mimeType": "multipart/alternative", "parts": list(parts)}],
        },
    }


def test_parse_gmail_plain_and_attachment():
    mail = parse_message(message(
        part("text/plain", "添付", filename="note.txt"),
        part("text/html", "<p>HTML版</p>"),
        part("text/plain", "請求書を確認", charset="iso-2022-jp"),
    ))
    assert (mail.message_id, mail.thread_id) == ("abc123", "def456")
    assert mail.sender == "担当 <sample@example.com>"
    assert mail.subject == "請求"
    assert mail.body == "請求書を確認"
    assert mail.received_at == "2024-01-01T00:00:00+00:00"


def test_html_body_and_detached_part():
    detached = {"mimeType": "text/plain", "body": {"attachmentId": "skip"}}
    html = "<p>お世話になります。<br>見積依頼です。</p><script>bad()</script><img src='https://example.com/'>"
    assert parse_message(message(detached, part("text/html", html))).body == "お世話になります。\n見積依頼です。"


def test_gmail_links():
    primary, search = gmail_links("def456", "<test+id@example.com>")
    assert primary == "https://mail.google.com/mail/u/0/#all/def456"
    assert search == "https://mail.google.com/mail/u/0/#search/rfc822msgid%3Atest%2Bid%40example.com"
    assert gmail_links("", "<test+id@example.com>") == (search, search)
    assert gmail_links("", "broken") == ("", "")


@pytest.mark.parametrize(("scope", "query"), [
    ("受信トレイのすべて", "in:inbox"),
    ("直近7日間のメール", "in:inbox newer_than:7d"),
    ("直近30日間のメール", "in:inbox newer_than:30d"),
    ("未読メールのみ", "in:inbox is:unread"),
    ("スター付きメール", "is:starred"),
    ("すべてのメール", "in:anywhere"),
])
def test_mail_scope_query_mapping(scope, query):
    assert gmail_query_for(scope) == query


def test_mail_scope_default_is_inbox():
    assert next(iter(MAIL_SCOPE_QUERIES)) == "受信トレイのすべて"


def test_category_labels_are_consistent_and_selectable():
    assert CATEGORIES[0] == "システム障害・不具合"
    assert "システム障害・不具合" in CATEGORIES
    assert all(not category.startswith("・") for category in CATEGORIES)
    assert "・" in CATEGORIES[0]


def test_category_display_names_map_to_stable_internal_categories():
    assert CATEGORY_OPTIONS == ("システム障害", "支払い", "契約", "一般", "その他")
    assert CATEGORY_BY_DISPLAY_NAME == {
        "システム障害": "システム障害・不具合",
        "支払い": "請求・支払い",
        "契約": "見積・契約",
        "一般": "一般問い合わせ",
        "その他": "その他",
    }
    assert category_display_name("システム障害・不具合") == "システム障害"
    assert category_internal_name("システム障害") == "システム障害・不具合"


def test_single_select_filters_and_no_selection_shows_all_rows():
    rows = [
        {"sender": "a@example.com", "subject": "障害", "category": "システム障害・不具合", "priority": "高"},
        {"sender": "b@example.com", "subject": "請求", "category": "請求・支払い", "priority": "中"},
        {"sender": "c@example.com", "subject": "契約", "category": "見積・契約", "priority": "低"},
    ]
    assert filter_rows(rows) == rows
    assert filter_rows(rows, category_label="システム障害") == [rows[0]]
    assert filter_rows(rows, priority="中") == [rows[1]]
    assert filter_rows(rows, category_label="契約", priority="低") == [rows[2]]


def test_received_at_sort_uses_datetime_not_text_order():
    rows = [
        {"message_id": "late", "received_at": "2026-01-15T03:00:00+00:00"},
        {"message_id": "early", "received_at": "2026-01-15T10:00:00+09:00"},
        {"message_id": "middle", "received_at": "2026-01-15T02:00:00+00:00"},
    ]
    assert [row["message_id"] for row in sort_rows_by_received_at(rows)] == ["late", "middle", "early"]
    assert [row["message_id"] for row in sort_rows_by_received_at(rows, oldest_first=True)] == ["early", "middle", "late"]


@pytest.mark.parametrize(("value", "expected"), [
    ("1", True), ("true", True), ("yes", True), ("", False), ("0", False),
])
def test_public_demo_switch(monkeypatch, value, expected):
    monkeypatch.setenv("AI_MAIL_MANAGER_PUBLIC_DEMO", value)
    assert is_public_demo() is expected


@pytest.mark.parametrize(("subject", "body", "category", "priority"), [
    ("システム障害", "ログインできません", "システム障害・不具合", "高"),
    ("領収書", "支払い済みです", "請求・支払い", "中"),
    ("見積依頼", "急ぎません", "見積・契約", "低"),
    ("営業時間", "受付は何時までですか", "一般問い合わせ", "中"),
    ("自動通知", "返信不要です", "その他", "低"),
    ("請求書", "至急発行してください", "請求・支払い", "高"),
])
def test_japanese_rules(subject, body, category, priority):
    result = rules(subject, body).classification
    assert (result.category, result.priority) == (category, priority)


def mock_service():
    service = MagicMock()
    return service, service.users.return_value.messages.return_value


def test_sync_pagination_dedup_preserves_edit_and_accounts(tmp_path):
    service, api = mock_service()
    api.list.return_value.execute.side_effect = [
        {"messages": [{"id": "abc123"}], "nextPageToken": "page2"},
        {"messages": [{"id": "abc123"}]},
        {"messages": [{"id": "abc123"}]},
        {"messages": [{"id": "abc123"}]},
    ]
    api.get.return_value.execute.return_value = message(part("text/plain", "元の本文"))
    store = Store(tmp_path / "db")
    classifier = MagicMock(side_effect=rules)
    assert sync(service, store, "a", "in:inbox", 20, classifier).added == 1
    assert api.list.call_args.kwargs["pageToken"] == "page2"
    assert api.list.call_args.kwargs["q"] == "in:inbox"
    store.update("a", "abc123", "その他", "低")
    assert sync(service, store, "a", "", 20, classifier).existing == 1
    classifier.assert_called_once()
    api.get.assert_called_once()
    row, = store.rows("a")
    assert (row["category"], row["priority"], row["edited"]) == ("その他", "低", 1)
    assert "body" not in row
    assert store.last_sync("a")
    assert sync(service, store, "b", "", 20, classifier).added == 1


@pytest.mark.parametrize("error", [
    RefreshError("private token detail"),
    HttpError(httplib2.Response({"status": "401"}), b"{}"),
])
def test_authentication_error(error):
    service, _ = mock_service()
    service.users.return_value.getProfile.return_value.execute.side_effect = error
    with pytest.raises(AuthError, match="認証が失効"):
        mailbox_account(service)


def test_oauth_only_readonly(monkeypatch):
    flow = MagicMock()
    monkeypatch.setattr(gmail, "InstalledAppFlow", flow)
    gmail.connect()
    assert flow.from_client_secrets_file.call_args.args[1] == [
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    assert flow.from_client_secrets_file.return_value.run_local_server.call_args.kwargs["host"] == "127.0.0.1"


def test_partial_sync_can_resume(tmp_path):
    service, api = mock_service()
    api.list.return_value.execute.return_value = {"messages": [{"id": "abc123"}, {"id": "2"}]}
    api.get.return_value.execute.side_effect = [
        message(part("text/plain", "問い合わせ")),
        HttpError(httplib2.Response({"status": "503"}), b"{}"),
        message(part("text/plain", "見積"), mid="2"),
    ]
    store = Store(tmp_path / "db")
    with pytest.raises(SyncError, match="503"):
        sync(service, store, "a", "", 20, rules)
    assert len(store.rows("a")) == 1
    assert store.last_sync("a") is None
    assert sync(service, store, "a", "", 20, rules).added == 1
    assert len(store.rows("a")) == 2


def test_openai_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = MagicMock()
    monkeypatch.setattr(classification, "OpenAI", client)
    client.return_value.responses.parse.side_effect = APIConnectionError(
        request=httpx.Request("POST", "https://example.com"),
    )
    assert Classifier(True, True)("請求書", "").method == "rule_fallback"


@pytest.mark.parametrize(("enabled", "consent", "key"), [(False, True, "test"), (True, False, "test"), (True, True, "")])
def test_openai_requires_consent_and_key(monkeypatch, enabled, consent, key):
    monkeypatch.setenv("OPENAI_API_KEY", key)
    client = MagicMock()
    monkeypatch.setattr(classification, "OpenAI", client)
    assert Classifier(enabled, consent)("件名", "本文").method == "rule"
    client.assert_not_called()


def test_openai_structured_output_masking(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = MagicMock()
    monkeypatch.setattr(classification, "OpenAI", client)
    parse = client.return_value.responses.parse
    parse.return_value.output_parsed = rules("見積", "見積をお願いします").classification
    parse.return_value.model = "model-returned-by-api"
    result = Classifier(True, True)("user@example.com", "連絡先 090-1234-5678")
    args = parse.call_args.kwargs
    assert args["text_format"] is Classification
    assert args["store"] is False
    assert "user@example.com" not in args["input"]
    assert "090-1234-5678" not in args["input"]
    assert result.model == "model-returned-by-api"
    assert result.prompt_version == classification.PROMPT_VERSION


def test_dashboard_uses_single_select_filters():
    source = inspect.getsource(dashboard)
    assert "multiselect" not in source
    assert source.count("selectbox") >= 4
    assert source.count('placeholder="オプションを選択"') == 3


def test_csv_export_preserves_display_values_and_blocks_formula_injection():
    assert safe_csv_cell("システム障害") == "システム障害"
    assert safe_csv_cell("=danger") == "'=danger"


def test_public_entrypoint_invokes_fabricated_data_only():
    script = Path(__file__).resolve().parents[1] / "app.py"
    with patch("app.ui.run_app") as run_app:
        runpy.run_path(str(script))
    run_app.assert_called_once_with(public_demo=True)
