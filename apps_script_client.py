"""
apps_script_client.py — the single HTTP transport to the Apps Script Web App.

Both the cellar (cellar.CellarBackend) and the conversation memory
(chat_memory.ChatMemory) talk to the SAME Google Apps Script deployment
(SHEETS_MEMORY_URL), gated by the SAME shared secret (SHEETS_SECRET, see
apps_script.js). This is the ONE place that knows how to reach it: read the env,
sign every request with the secret (query param on GET, body field on POST), and
marshal JSON over urllib. One deployment, one auth path, one timeout policy — the
feature modules just call get_json / post_json.

Stdlib-only (urllib) so the serverless function stays dependency-free.
"""

import json
import os
import urllib.parse
import urllib.request


class AppsScriptClient:
    """Secret-signed JSON transport over the shared Apps Script Web App.

    *timeout* (seconds) is per-request; callers pick a value matching how slow
    the operation can be (cellar writes get longer than a memory read).
    """

    def __init__(self, timeout: int = 8):
        self._url = os.environ.get("SHEETS_MEMORY_URL", "").strip()
        # Shared secret gating the Apps Script Web App (see apps_script.js).
        self._secret = os.environ.get("SHEETS_SECRET", "").strip()
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        """True when SHEETS_MEMORY_URL is set; nothing reaches the sheet otherwise."""
        return bool(self._url)

    def get_json(self, params: dict) -> dict:
        """GET with *params* (plus the secret) as the query string; return parsed JSON.

        Raises on network/parse error so each caller decides whether to degrade
        (return None / [] / "") rather than crash.
        """
        query = dict(params)
        if self._secret:
            query["key"] = self._secret
        url = f"{self._url}?{urllib.parse.urlencode(query)}"
        with urllib.request.urlopen(url, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_json(self, payload: dict) -> dict:
        """POST *payload* as JSON, signing it with the secret; return parsed JSON.

        Returns {} when the endpoint answers with an empty body (some actions
        acknowledge without a document). Raises on network/parse error.
        """
        body = payload
        if self._secret:
            body = {**payload, "key": self._secret}
        req = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
