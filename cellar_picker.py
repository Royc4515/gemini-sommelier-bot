"""
cellar_picker.py — the shared cellar "bottle picker" (one home for every flow
that lets the user choose a bottle from the cellar).

/status, /delete and /editwine each open with the same picker: a numbered list
of bottles, an optional tap-to-select keyboard when the view is small, and a
reply that is either a number (choose) or a word (filter the list). That picker
used to be copied almost verbatim into all three flow modules.

It lives here once now. Each flow supplies only what differs:

  * the entry shape — the status/delete flows carry a compact entry
    (``lightweight_entries``); /editwine carries a full A-N record. The render
    helpers take small *accessor* callables (``name_of`` / ``vintage_of`` /
    ``status_of``) so either shape works.
  * the list header text and the callback-data prefix (``status:pick:`` etc.).

Pure functions only — no I/O, no per-flow state — so they stay trivially
unit-testable and side-effect free.
"""

from cellar import display_name

# How many bottles to list, and the view size at/under which we still render
# tap-to-select buttons (a bigger cellar would make an unwieldy keyboard, so the
# number/filter path takes over). Shared so the three flows never drift apart.
MAX_LIST = 60
PICK_MAX = 12

# Status value -> Hebrew label (the sheet stores the English value).
STATUS_LABELS = {
    "Open": "פתוח 🍷",
    "Closed": "סגור",
    "Finished": "הסתיים",
}


def status_label(status) -> str:
    """Hebrew label for a status value; the raw value or '-' when unknown/blank."""
    return STATUS_LABELS.get(status, status or "-")


def lightweight_entries(wines: list[dict]) -> list[dict]:
    """Project ``CellarBackend.list_wines()`` onto the compact picker entry used
    by the /status and /delete flows: identity + status + vintage, no full A-N
    record. Rows without a sheet-row handle are skipped.
    """
    entries: list[dict] = []
    for w in wines:
        row = w.get("row")
        if not row:
            continue
        values = w.get("values") or []
        entries.append({
            "row": row,
            "status": w.get("status") or "",
            "winery": values[0] if len(values) > 0 else "",
            "wine_name": values[1] if len(values) > 1 else "",
            "vintage": values[3] if len(values) > 3 else "",
        })
    return entries


def entry_name(entry: dict) -> str:
    """Display name for a lightweight entry ({winery, wine_name})."""
    return display_name({"winery": entry.get("winery"),
                         "wine_name": entry.get("wine_name")})


# ----------------------------------------------------------------------
# Default accessors for the lightweight entry shape (status / delete).
# /editwine passes its own rec-based accessors.
# ----------------------------------------------------------------------

def _lw_name(entry: dict) -> str:
    return entry_name(entry)


def _lw_vintage(entry: dict) -> str:
    return entry.get("vintage") or "-"


def _lw_status(entry: dict) -> str:
    return status_label(entry.get("status"))


# ----------------------------------------------------------------------
# Rendering + selection (shared by all three pickers)
# ----------------------------------------------------------------------

def render_list(
    entries: list[dict],
    shown: list[int],
    header: str,
    *,
    name_of=_lw_name,
    vintage_of=_lw_vintage,
    status_of=_lw_status,
) -> str:
    """Render the numbered, filterable bottle list under *header*.

    *shown* are the indices of *entries* currently displayed (the full list, or
    a filtered subset). The list is capped at ``MAX_LIST`` with a "narrow it
    down" hint when longer.
    """
    lines = [header]
    for display_num, idx in enumerate(shown[:MAX_LIST], start=1):
        e = entries[idx]
        lines.append(f"{display_num}. {name_of(e)} ({vintage_of(e)}) [{status_of(e)}]")
    if len(shown) > MAX_LIST:
        lines.append(f"\n...ועוד {len(shown) - MAX_LIST}. סנן בעזרת מילה כדי לצמצם.")
    lines.append("\n/cancel לביטול.")
    return "\n".join(lines)


def list_keyboard(
    entries: list[dict],
    shown: list[int],
    pick_prefix: str,
    *,
    name_of=_lw_name,
    vintage_of=_lw_vintage,
) -> dict | None:
    """Inline keyboard of tap-to-select bottle buttons, or ``None`` when the
    view is empty or too large (then numbers/filter remain the only path).

    *pick_prefix* is the flow's callback namespace, e.g. ``"status:pick:"``; the
    bottle's sheet row is appended to it.
    """
    if not shown or len(shown) > PICK_MAX:
        return None
    rows = []
    for idx in shown:
        e = entries[idx]
        label = f"{name_of(e)} ({vintage_of(e)})"
        rows.append([{"text": label[:60], "callback_data": f"{pick_prefix}{e['row']}"}])
    return {"inline_keyboard": rows}


def filter_indices(entries: list[dict], text: str, *, name_of=_lw_name) -> list[int]:
    """Indices of entries whose display name contains *text* (case-folded)."""
    needle = text.casefold()
    return [i for i, e in enumerate(entries) if needle in name_of(e).casefold()]


def resolve_selection(
    entries: list[dict],
    shown: list[int] | None,
    text: str,
    *,
    name_of=_lw_name,
) -> tuple[str, object]:
    """Resolve a reply at the AWAIT_SELECT stage into one explicit outcome.

    Returns one of:
      * ``("pick", entry)``     — a valid 1-based number, the chosen entry.
      * ``("invalid", None)``   — a number outside the current view.
      * ``("filter", indices)`` — a name filter that matched (the new *shown*).
      * ``("empty", None)``     — a name filter that matched nothing.

    *shown* is the currently displayed index list (``None``/empty -> all).
    """
    current = shown if shown else list(range(len(entries)))
    if text.isdigit():
        n = int(text)
        if not (1 <= n <= len(current)):
            return ("invalid", None)
        return ("pick", entries[current[n - 1]])
    indices = filter_indices(entries, text, name_of=name_of)
    if not indices:
        return ("empty", None)
    return ("filter", indices)


def disable_buttons(telegram, chat_id, message_id) -> None:
    """Remove the inline keyboard from a message (best-effort, no-op if no id).

    The visual half of the idempotency guard; ``TelegramClient`` already
    swallows API errors here, so a failed cleanup never blocks the real work.
    """
    if message_id is None:
        return
    telegram.edit_message_reply_markup(
        chat_id, message_id, reply_markup={"inline_keyboard": []}
    )
