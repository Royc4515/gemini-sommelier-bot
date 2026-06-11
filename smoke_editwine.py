#!/usr/bin/env python3
"""
smoke_editwine.py — live smoke test for the /editwine Apps Script contract.

Validates, against your REAL deployed Apps Script + sheet, the two endpoints
/editwine relies on, without going through Telegram and WITHOUT changing any
data:

  * list_wines  -> fetches the cellar, checks every row has {row:int,
                   values:[14 cells], status} (the shape editwine assumes).
  * update_wine -> on the FIRST wine, writes its values back UNCHANGED (a true
                   no-op), proving the write path + identity guard work, then
                   verifies the guard REJECTS a wrong-identity write.

Run it where SHEETS_MEMORY_URL / SHEETS_SECRET are set (your local shell or a
machine with your .env), e.g.:

    SHEETS_MEMORY_URL='https://script.google.com/.../exec' \
    SHEETS_SECRET='your-secret' \
    python smoke_editwine.py            # read-only checks

    ... python smoke_editwine.py --write-test   # also do the no-op write round-trip

Only --write-test touches the sheet, and only with a no-op + a rejected write.
"""

import os
import sys

# Repo root on path so `import cellar` works no matter where we're invoked from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cellar import CellarBackend, ROW_ORDER, build_row  # noqa: E402


def _fail(msg: str) -> None:
    print(f"❌ {msg}")
    sys.exit(1)


def main() -> None:
    write_test = "--write-test" in sys.argv[1:]

    backend = CellarBackend()
    if not backend.configured:
        _fail("SHEETS_MEMORY_URL is not set — run this where your env vars live.")

    print("→ Calling list_wines ...")
    wines = backend.list_wines()
    if not wines:
        _fail("list_wines returned nothing. Either the cellar is empty, the URL/"
              "secret is wrong, or the Apps Script wasn't redeployed with the new code.")

    print(f"✓ list_wines returned {len(wines)} rows.\n")

    # --- Validate the shape editwine assumes -------------------------------
    problems = 0
    for i, w in enumerate(wines):
        row = w.get("row")
        values = w.get("values")
        if not isinstance(row, int) or row < 2:
            print(f"  ⚠ item {i}: bad 'row' = {row!r}")
            problems += 1
        if not isinstance(values, list) or len(values) != len(ROW_ORDER):
            print(f"  ⚠ item {i} (row {row}): 'values' is "
                  f"{len(values) if isinstance(values, list) else type(values).__name__}, "
                  f"expected a list of {len(ROW_ORDER)}")
            problems += 1
    if problems:
        _fail(f"{problems} row(s) had an unexpected shape — editwine would misbehave.")
    print(f"✓ Every row has the expected shape: row:int + {len(ROW_ORDER)} cells + status.\n")

    # Show the first wine so you can eyeball the column mapping.
    first = wines[0]
    print("First wine (column -> value):")
    for key, val in zip(ROW_ORDER, first["values"]):
        print(f"  {key:>22}: {val!r}")
    print(f"  {'(status)':>22}: {first.get('status')!r}\n")

    if not write_test:
        print("Read-only checks passed. Re-run with --write-test to verify the "
              "write path (safe: no-op write + a rejected wrong-identity write).")
        return

    # --- No-op write round-trip on the first wine --------------------------
    row = first["row"]
    values = list(first["values"])
    winery, wine_name = values[0], values[1]
    expect = {"winery": winery, "wine_name": wine_name}

    print(f"→ update_wine (NO-OP, identical values) on row {row} "
          f"({winery!r} / {wine_name!r}) ...")
    try:
        backend.update_wine(row, values, expect)
    except Exception as exc:
        _fail(f"No-op update_wine FAILED: {exc}\n"
              "   The write path is broken — fix before relying on /editwine.")
    print("✓ No-op write succeeded (row unchanged).\n")

    # --- The identity guard must REJECT a wrong-identity write -------------
    print("→ update_wine with a deliberately WRONG identity (guard should reject) ...")
    try:
        backend.update_wine(row, values, {"winery": "__SMOKE_NOPE__",
                                          "wine_name": "__SMOKE_NOPE__"})
    except Exception as exc:
        if "row_mismatch" in str(exc):
            print("✓ Guard correctly rejected the mismatched write (row_mismatch).\n")
        else:
            _fail(f"Guard rejected, but with an unexpected error: {exc}")
    else:
        _fail("Guard did NOT reject a wrong-identity write — a shifted row could be "
              "clobbered. The Apps Script identity check isn't working.")

    print("🎉 All checks passed. The /editwine Apps Script contract is solid.")


if __name__ == "__main__":
    main()
