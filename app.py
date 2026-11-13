"""Streamlit Community Cloud entry point for the public portfolio demo."""

from app.ui import run_app


# Explicitly call the renderer instead of relying on module-import side effects.
# The public entry point never exposes Gmail, OpenAI, or local-mail storage.
run_app(public_demo=True)
