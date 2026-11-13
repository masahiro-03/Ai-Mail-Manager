import csv
from app.core import classify, process_csv, get_rows

def test_error():
    assert classify('ログインできない', 'エラーが出ます')['category'] == '障害・不具合'

def test_billing():
    assert classify('領収書', '支払いを確認')['category'] == '請求・支払い'

def test_quote():
    assert classify('見積依頼', '料金を知りたい')['category'] == '見積・契約'

def test_general():
    assert classify('こんにちは', '営業時間を教えて')['category'] == '一般問い合わせ'

def test_priority():
    assert classify('障害', '')['priority'] == '高'

def test_idempotent(tmp_path):
    src = tmp_path / 'input.csv'
    with src.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['subject','body'])
        writer.writeheader()
        writer.writerow({'subject':'請求書','body':'送付してください'})
    db = tmp_path / 'test.db'
    assert process_csv(src, db) == 1
    assert process_csv(src, db) == 0
    assert len(get_rows(db)) == 1
