import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from app.core import get_rows, process_csv


def render_legacy():
    st.title('問い合わせ分類・業務自動化デモ')
    st.caption('架空データ専用の応募用サンプル。ルールベース分類でオフライン実行できます。')
    upload = st.file_uploader('CSVをアップロード（subject,body 列）', type='csv')
    if 'db_path' not in st.session_state:
        st.session_state.db_path = str(Path(tempfile.mkdtemp()) / 'demo.db')
    if upload and st.button('分類して保存'):
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
            f.write(upload.getvalue())
            temp_path = f.name
        try:
            count = process_csv(temp_path, st.session_state.db_path)
            st.success(f'{count}件を新規登録しました。重複行は登録しません。')
        finally:
            Path(temp_path).unlink(missing_ok=True)
    rows = get_rows(st.session_state.db_path)
    if rows:
        df = pd.DataFrame(rows, columns=['件名','本文','分類','優先度','理由'])
        st.dataframe(df, width="stretch")
        st.download_button('結果CSVを保存', df.to_csv(index=False).encode('utf-8-sig'), 'results.csv', 'text/csv')
