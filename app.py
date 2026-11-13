"""Streamlit Community Cloud entry point for the public portfolio demo.

The full Gmail integration remains available locally through ``app/ui.py``.
This entry point deliberately enables only fabricated demo data before loading
the shared UI, so a public deployment cannot start OAuth or call OpenAI.
"""

import os

os.environ["AI_MAIL_MANAGER_PUBLIC_DEMO"] = "1"

import app.ui  # noqa: F401  - Streamlit renders the module at import time.
