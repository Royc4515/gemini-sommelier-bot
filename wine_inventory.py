"""
wine_inventory.py — Data Layer

Fetches and parses the wine inventory CSV from a remote Google Sheets URL.
Uses only stdlib modules (urllib, csv) to keep the deployment lightweight.
"""

import csv
import io
import os
import urllib.request


class WineInventory:
    """Handles fetching, parsing, and formatting the wine inventory."""

    FETCH_TIMEOUT_SECONDS = 4

    def __init__(self):
        self.csv_url: str = os.environ["WINE_CSV_URL"]

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------
    def fetch_inventory(self) -> str:
        """Download the CSV content from *WINE_CSV_URL*.

        Returns the raw text decoded with ``utf-8-sig`` (strips BOM if present).
        Raises ``urllib.error.URLError`` on network failures.
        """
        request = urllib.request.Request(self.csv_url)
        with urllib.request.urlopen(request, timeout=self.FETCH_TIMEOUT_SECONDS) as response:
            raw_bytes: bytes = response.read()
        return raw_bytes.decode("utf-8-sig")

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------
    def parse_inventory(self, raw_csv: str) -> list[dict[str, str]]:
        """Parse *raw_csv* into a list of row-dicts.

        Rows are **skipped** when:
        * 'יקב' (winery) is empty.
        * 'שם היין' (wine name) is empty.
        * 'סטטוס חדש' equals ``"Finished"``.
        """
        reader = csv.DictReader(io.StringIO(raw_csv))
        rows: list[dict[str, str]] = []
        for row in reader:
            winery = (row.get("יקב") or "").strip()
            wine_name = (row.get("שם היין") or "").strip()
            status = (row.get("סטטוס חדש") or "").strip()

            if not winery or not wine_name:
                continue
            if status == "Finished":
                continue

            rows.append(row)
        return rows

    # ------------------------------------------------------------------
    # Format
    # ------------------------------------------------------------------
    def get_formatted_inventory(self) -> str:
        """Return a human-/LLM-readable inventory block.

        Each entry includes: Winery, Wine Name, Vintage, Grapes,
        Status (Open/Closed), Purpose, and Opening Recommendation (המלצת פתיחה).
        """
        raw_csv = self.fetch_inventory()
        rows = self.parse_inventory(raw_csv)

        if not rows:
            return "המלאי ריק כרגע."

        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            winery = row.get("יקב", "")
            wine_name = row.get("שם היין", "")
            vintage = row.get("בציר", "N/A")
            grapes = row.get("זנים", "N/A")
            status = row.get("סטטוס חדש", "N/A")
            purpose = row.get("מטרה", "N/A")
            opening_rec = row.get("המלצת פתיחה", "N/A")

            lines.append(
                f"{i}. {winery} — {wine_name}\n"
                f"   Vintage: {vintage} | Grapes: {grapes}\n"
                f"   Status: {status} | Purpose: {purpose}\n"
                f"   Recommendation: {opening_rec}"
            )

        return "\n\n".join(lines)
