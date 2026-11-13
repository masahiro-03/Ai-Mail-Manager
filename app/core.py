import csv
import hashlib
import sqlite3
from pathlib import Path

CATEGORIES = {
    'システム障害・不具合': ('障害', 'エラー', 'ログインでき', '動かない', '不具合'),
    '請求・支払い': ('請求', '支払', '領収書', '振込'),
    '見積・契約': ('見積', '契約', '料金', '価格'),
}

def classify(subject: str, body: str) -> dict:
    text = f'{subject} {body}'
    scores = {name: sum(word in text for word in words) for name, words in CATEGORIES.items()}
    category = max(scores, key=scores.get)
    if scores[category] == 0:
        category = '一般問い合わせ'
    priority = '高' if category == 'システム障害・不具合' else '通常'
    return {'category': category, 'priority': priority, 'reason': 'キーワードに基づく分類' if category != '一般問い合わせ' else '該当キーワードなし'}

def init_db(path):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE IF NOT EXISTS inquiries (id TEXT PRIMARY KEY, subject TEXT, body TEXT, category TEXT, priority TEXT, reason TEXT)')
    con.commit()
    return con

def process_csv(csv_path, db_path):
    con = init_db(db_path)
    added = 0
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            subject, body = row['subject'], row['body']
            uid = hashlib.sha256(f'{subject}\0{body}'.encode()).hexdigest()
            result = classify(subject, body)
            cur = con.execute('INSERT OR IGNORE INTO inquiries VALUES (?, ?, ?, ?, ?, ?)', (uid, subject, body, result['category'], result['priority'], result['reason']))
            added += cur.rowcount
    con.commit()
    con.close()
    return added

def get_rows(db_path):
    con = init_db(db_path)
    rows = con.execute('SELECT subject, body, category, priority, reason FROM inquiries ORDER BY rowid DESC').fetchall()
    con.close()
    return rows
