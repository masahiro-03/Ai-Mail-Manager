"""Runtime switches that keep the public demo free of external data access."""

import os


def is_public_demo():
    return os.getenv("AI_MAIL_MANAGER_PUBLIC_DEMO", "").strip().lower() in {
        "1", "true", "yes",
    }
