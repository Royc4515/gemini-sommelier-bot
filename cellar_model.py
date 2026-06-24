"""
cellar_model.py — the A-N column model for the cellar sheet.

Pure, dependency-free projection between a wine-record dict and the
spreadsheet's fixed column order, plus the small identity helpers the write
flows share. Kept apart from the network client (cellar.CellarBackend) and the
fill parser (cellar_fill) so the column contract has one obvious home.
"""


# Sheet columns A-N, in exact order. The bot NEVER writes O/P/Q.
ROW_ORDER = (
    "winery", "wine_name", "type", "vintage", "grape_blend", "region",
    "quantity", "price", "store", "purchase_date", "purpose",
    "drinking_window", "opening_recommendation", "tasting_notes",
)


def build_row(record: dict) -> list:
    """Project a record dict onto the A-N column order."""
    return [record.get(key, "") for key in ROW_ORDER]


def display_name(record: dict) -> str:
    name = record.get("wine_name") or "(ללא שם)"
    winery = record.get("winery")
    return f"{winery} - {name}" if winery else name


def expect_from_state(state: dict) -> dict:
    """The shifted-row identity guard for a write flow's confirm step.

    Every stateful write flow (/editwine, /status, /delete, and the
    orchestrator) stashes the chosen bottle's original winery / wine name under
    ``orig_winery`` / ``orig_wine_name`` when it enters its confirm step. This
    projects that state back onto the ``expect`` dict that
    ``CellarBackend.update_wine`` / ``set_status`` / ``delete_wine`` verify, so
    a row that shifted since it was listed is refused instead of clobbered.
    """
    return {"winery": state.get("orig_winery", ""),
            "wine_name": state.get("orig_wine_name", "")}
