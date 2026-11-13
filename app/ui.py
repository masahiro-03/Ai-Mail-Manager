import csv
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.classification import CATEGORIES, PRIORITIES, Classifier, rules
from app.config import is_public_demo
from app.gmail import MAIL_SCOPE_QUERIES, Mail, connect, gmail_query_for, service_for
from app.storage import Store
from app.sync import LIMITS, AuthError, SyncError, mailbox_account, sync

JST = timezone(timedelta(hours=9))
DEMO = "demo"
COLUMNS = {
    "received_at": "受信日時", "sender": "送信者", "subject": "件名",
    "category": "カテゴリ", "priority": "優先度", "summary": "要約",
    "recommendation": "推奨対応", "gmail_link": "Gmailで開く",
}

# SQLite and classifiers use the stable internal values in CATEGORIES.  The
# public UI intentionally shows shorter business-facing labels instead.
CATEGORY_DISPLAY_NAMES = {
    "システム障害・不具合": "システム障害",
    "請求・支払い": "支払い",
    "見積・契約": "契約",
    "一般問い合わせ": "一般",
    "その他": "その他",
}
CATEGORY_OPTIONS = tuple(CATEGORY_DISPLAY_NAMES[category] for category in CATEGORIES)
CATEGORY_BY_DISPLAY_NAME = {label: category for category, label in CATEGORY_DISPLAY_NAMES.items()}


def category_display_name(category):
    return CATEGORY_DISPLAY_NAMES.get(category, category)


def category_internal_name(label):
    return CATEGORY_BY_DISPLAY_NAME.get(label, label)
def local_time(value):
    return datetime.fromisoformat(value).astimezone(JST).strftime("%Y/%m/%d %H:%M JST") if value else "—"


def received_at_datetime(value):
    """Convert stored ISO 8601 text into a consistently comparable instant."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sort_rows_by_received_at(rows, oldest_first=False):
    return sorted(rows, key=lambda row: received_at_datetime(row["received_at"]), reverse=not oldest_first)


def filter_rows(rows, category_label=None, priority=None, query=""):
    category = category_internal_name(category_label) if category_label else None
    normalized_query = query.strip().casefold()
    return [
        row for row in rows
        if (not category or row["category"] == category)
        and (not priority or row["priority"] == priority)
        and (not normalized_query or any(normalized_query in row[field].casefold()
                                         for field in ("sender", "subject")))
    ]


def demo_store():
    if "demo_dir" not in st.session_state:
        st.session_state.demo_dir = TemporaryDirectory(prefix="route-team-demo-")
    store = Store(Path(st.session_state.demo_dir.name) / "demo.sqlite3")
    if store.last_sync(DEMO) is None:
        with (ROOT / "data" / "sample_inquiries.csv").open(encoding="utf-8-sig", newline="") as source:
            for number, row in enumerate(csv.DictReader(source), start=1):
                received = datetime(2026, 1, 15, 3, tzinfo=timezone.utc) - timedelta(hours=number)
                mail = Mail(str(number), "", f"sample{number}@example.com", row["subject"],
                            received.isoformat(), row["body"], "", "")
                store.add(DEMO, mail, rules(mail.subject, mail.body))
        store.finish(DEMO)
    return store


def gmail_controls():
    state = st.session_state
    if (notice := state.pop("gmail_notice", None)):
        st.error(notice)
    if "gmail_credentials" not in state:
        st.write("Googleアカウントに読み取り専用で接続します。同期ボタンで指定したメールを取得します。")
        if st.button("Gmailに接続", type="primary"):
            if not (ROOT / "credentials.json").is_file():
                st.error("READMEの手順に従い、Google Cloudの credentials.json を配置してください。")
                return None, None
            try:
                with st.spinner("ブラウザーのGoogle認証画面で操作してください。"):
                    credentials = connect()
            except Exception:
                st.error("Google認証を完了できませんでした。設定、認証の許可、接続を確認してください。")
                return None, None
            state.gmail_credentials = credentials
            st.rerun()
        return None, None

    if st.button("Gmailの接続を解除"):
        for key in ("gmail_credentials", "gmail_account", "ai_enabled", "ai_consent"):
            state.pop(key, None)
        st.rerun()

    account = state.get("gmail_account")
    if account:
        st.caption(f"接続中のメールボックス　{account}")
    path = ROOT / "private" / "mail.sqlite3"
    path.parent.mkdir(exist_ok=True)
    store = Store(path)
    scope = st.selectbox("取得するメール", list(MAIL_SCOPE_QUERIES), index=0)
    query = gmail_query_for(scope)
    if scope == "すべてのメール":
        st.caption("迷惑メール・ゴミ箱を含む、すべてのメールを対象にします。")
    elif scope in ("直近7日間のメール", "直近30日間のメール", "未読メールのみ"):
        st.caption("受信トレイ内のメールを対象にします。")
    limit = st.selectbox("取得件数", LIMITS)
    has_key = bool(os.getenv("OPENAI_API_KEY", "").strip())
    enabled = st.checkbox("OpenAIによる分類を使う", disabled=not has_key, key="ai_enabled")
    consent = False
    if enabled and has_key:
        st.write("同期時に新着メールの件名と本文の一部をOpenAIに送ります。"
                 "メールアドレスと電話番号をマスキングします。氏名や機密情報が残る場合があります。")
        consent = st.checkbox("上記のデータをOpenAIに送信することに同意する", key="ai_consent")
    else:
        st.caption("ルールベースで分類します。")
    if st.button("メールを同期", type="primary", disabled=enabled and has_key and not consent):
        try:
            with st.spinner("Gmailを同期しています。"):
                service = service_for(state.gmail_credentials)
                account = mailbox_account(service)
                state.gmail_account = account
                report = sync(service, store, account, query, limit, Classifier(enabled, consent))
            st.success(f"新規 {report.added}件、登録済み {report.existing}件")
            if report.unavailable:
                st.warning(f"{report.unavailable}件は取得または解析できませんでした。")
            if report.fallback:
                st.warning(f"{report.fallback}件はOpenAIで分類できず、ルールで分類しました。")
        except AuthError as exc:
            for key in ("gmail_credentials", "gmail_account", "ai_enabled", "ai_consent"):
                state.pop(key, None)
            state.gmail_notice = str(exc)
            st.rerun()
        except SyncError as exc:
            st.error(f"{exc} 同期中に保存済みのメールは再試行時にスキップされます。")
    return store, account


def safe_csv_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@"))
                                   or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def dashboard(store, account, demo=False):
    rows = store.rows(account)
    st.header("ダッシュボード")
    total, high, last = st.columns(3)
    total.metric("取得メール総数", len(rows))
    high.metric("高優先度", sum(row["priority"] == "高" for row in rows))
    last.metric("最終同期日時", "架空データ" if demo else local_time(store.last_sync(account)))
    counts = pd.DataFrame({"カテゴリ": CATEGORY_OPTIONS, "件数": [
        sum(row["category"] == category for row in rows) for category in CATEGORIES
    ]})
    st.bar_chart(counts, x="カテゴリ", y="件数", horizontal=True, color="#367b70")
    st.caption("　／　".join(f"{name} {count}件" for name, count in counts.itertuples(index=False)))

    st.header("メール一覧")
    left, middle, right = st.columns(3)
    category_label = left.selectbox("カテゴリ", CATEGORY_OPTIONS, index=None,
                                    placeholder="オプションを選択", key=f"category-{demo}")
    priority = middle.selectbox("優先度", PRIORITIES, index=None,
                                placeholder="オプションを選択", key=f"priority-{demo}")
    received_order = right.selectbox("受信日時順", ("新しい順", "古い順"), index=None,
                                     placeholder="オプションを選択", key=f"order-{demo}")
    query = st.text_input("送信者・件名で検索", key=f"search-{demo}").strip().casefold()
    filtered = filter_rows(rows, category_label, priority, query)
    filtered = sort_rows_by_received_at(filtered, oldest_first=received_order == "古い順")
    if not filtered:
        st.info("表示できるメールがありません。")
        return
    table = pd.DataFrame([{
        **row,
        "received_at": local_time(row["received_at"]),
        "category": category_display_name(row["category"]),
    } for row in filtered])
    table = table[list(COLUMNS)].rename(columns=COLUMNS)
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        "Gmailで開く": st.column_config.LinkColumn(display_text="Gmailで開く"),
    })
    export = table.apply(lambda column: column.map(safe_csv_cell)).to_csv(index=False).encode("utf-8-sig")
    st.download_button("一覧をCSVでダウンロード", export, "inquiries.csv", "text/csv")

    by_id = {row["message_id"]: row for row in filtered}
    selected = st.selectbox("詳細・分類修正", list(by_id), index=None, placeholder="メールを選択",
                            key=f"details-{demo}",
                            format_func=lambda mid: f"{local_time(by_id[mid]['received_at'])} | {by_id[mid]['subject']}")
    if selected is None:
        return
    row = by_id[selected]
    st.text(f"分類理由　{row['reason']}")
    model = f" / {row['model']}" if row["model"] else ""
    st.caption(f"分類元　{row['method']}{model} / バージョン {row['prompt_version']}"
               + (" / カテゴリ・優先度は手動修正済み" if row["edited"] else ""))
    if row["search_link"]:
        st.link_button("Message-IDでGmailを検索", row["search_link"])
    key = hashlib.sha256(f"{account}\0{selected}".encode()).hexdigest()
    with st.form(f"edit-{key}"):
        category = st.selectbox("カテゴリを修正", CATEGORY_OPTIONS,
                                index=CATEGORY_OPTIONS.index(category_display_name(row["category"])))
        priority = st.selectbox("優先度を修正", PRIORITIES, index=PRIORITIES.index(row["priority"]))
        if st.form_submit_button("修正を保存"):
            store.update(account, selected, category_internal_name(category), priority)
            st.rerun()


def run_app(public_demo=None):
    """Render either the safe public demo or the full local-only application."""
    if public_demo is None:
        public_demo = is_public_demo()

    st.set_page_config(page_title="問い合わせ管理", page_icon="📨", layout="wide")
    st.title("AI Mail Manager")
    try:
        if public_demo:
            st.caption("採用選考向け公開デモ｜架空データのみを使用し、Gmail・OpenAIなど外部サービスには接続しません。")
            dashboard(demo_store(), DEMO, demo=True)
            return

        mode = st.radio("データソース", ["Gmail連携", "架空データのデモ", "従来のCSVデモ"], horizontal=True)
        if mode == "Gmail連携":
            store, account = gmail_controls()
            if account:
                dashboard(store, account)
        elif mode == "架空データのデモ":
            st.caption("架空の問い合わせをルールベースで分類しています。")
            dashboard(demo_store(), DEMO, demo=True)
        else:
            from app.legacy import render_legacy
            render_legacy()
    except sqlite3.Error:
        st.error("データベースを読み書きできませんでした。保存先と空き容量を確認してください。")


if __name__ == "__main__":
    run_app()
