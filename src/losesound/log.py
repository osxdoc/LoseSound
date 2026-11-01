"""Small structured logging helper.

Logs are written to stdout as ``key=value`` pairs so that they stay readable in
``docker compose logs`` while still being easy to parse. URLs are redacted by
default because radio stream URLs regularly carry access tokens.
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import urlsplit, urlunsplit

LOG_URL_QUERY = os.getenv("LOG_URL_QUERY", "0").strip().lower() in ("1", "true", "yes", "on")


def redact_url(url: str) -> str:
    """Strip query and fragment from a URL unless LOG_URL_QUERY is enabled."""
    if not url or LOG_URL_QUERY:
        return url
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return str(url)
    query = "<redacted>" if parts.query else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def format_log_value(value) -> str:
    text = str(value).replace("\n", "\\n").replace("\r", "\\r")
    if not text or any(char.isspace() for char in text):
        return json.dumps(text)
    return text


def log(event: str, **fields) -> None:
    """Write one structured log line."""
    parts = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), event]
    for key, value in fields.items():
        if value is None:
            continue
        if "url" in key.lower() or key.lower().endswith("uri"):
            value = redact_url(str(value))
        parts.append(f"{key}={format_log_value(value)}")
    print(" ".join(parts), flush=True)
