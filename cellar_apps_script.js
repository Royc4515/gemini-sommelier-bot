/**
 * Auto-archive trigger — bound to the CELLAR spreadsheet ("Roy's Wine Cellar").
 *
 * This is a SEPARATE Apps Script from apps_script.js. apps_script.js is bound to
 * the "Sommelier Memory" sheet and backs the bot's read/write API; this file is
 * bound to the cellar spreadsheet itself. Paste it into that spreadsheet's own
 * editor: Extensions -> Apps Script -> replace everything -> Save. No deployment
 * and no manual trigger install are needed — a simple onEdit fires automatically
 * because it only touches its own spreadsheet.
 *
 * Behavior: when a bottle's status cell ('סטטוס חדש') is set to "Finished", its
 * whole row is moved to the "Archive" tab and removed from the active cellar.
 *
 * What this fixes vs. the previous version:
 *   - e.value === "Finished" only fired for a SINGLE typed cell, so filling the
 *     status of several rows at once (or pasting) archived nothing. We now read
 *     the status cells from the edited range and handle multi-row edits.
 *   - The status column was hard-coded to P (16); it is now located by its
 *     header, so inserting/moving a column no longer silently breaks archiving.
 *   - The source tab was matched only by the name "Active Cellar"; we now fall
 *     back to the winery-header marker so a renamed tab still works.
 *   - copyTo() dragged the cellar's live formulas (O/P/Q) into Archive, where
 *     their relative references recompute to garbage. We keep the formatting but
 *     write a frozen snapshot of the computed values instead.
 *   - A missing Archive tab popped a getUi().alert(), which throws in a trigger
 *     with no open UI and aborts the whole run. We auto-create the tab instead.
 */

var ACTIVE_CELLAR_NAME = "Active Cellar";   // primary tab name (fallback below).
var ARCHIVE_SHEET_NAME = "Archive";
var CELLAR_HEADER_MARKER = "יקב";           // winery header -> identifies the cellar tab.
var STATUS_HEADER_MARKER = "סטטוס חדש";     // status header -> located by name, not a fixed column.
var FINISHED_STATUS = "Finished";


function onEdit(e) {
  if (!e || !e.range) return;

  var sheet = e.range.getSheet();
  if (!_isActiveCellar(sheet)) return;

  var statusCol = _statusColumn(sheet);
  if (statusCol < 1) return;

  // The edit must overlap the status column.
  var firstCol = e.range.getColumn();
  var lastCol = firstCol + e.range.getNumColumns() - 1;
  if (statusCol < firstCol || statusCol > lastCol) return;

  // Collect matching rows first, then move them bottom-up so deleting one row
  // never shifts the index of another we still need to move. We read the cell
  // value rather than e.value so multi-row edits/pastes are handled too.
  var startRow = e.range.getRow();
  var numRows = e.range.getNumRows();
  var rows = [];
  for (var r = startRow; r < startRow + numRows; r++) {
    if (r === 1) continue; // never the header
    var status = String(sheet.getRange(r, statusCol).getValue()).trim();
    if (status === FINISHED_STATUS) rows.push(r);
  }
  for (var i = rows.length - 1; i >= 0; i--) {
    _archiveRow(sheet, rows[i]);
  }
}


function _archiveRow(sheet, row) {
  var ss = sheet.getParent();
  var width = sheet.getLastColumn();

  var archive = ss.getSheetByName(ARCHIVE_SHEET_NAME);
  if (!archive) {
    archive = ss.insertSheet(ARCHIVE_SHEET_NAME);
    archive.getRange(1, 1, 1, width).setValues(sheet.getRange(1, 1, 1, width).getValues());
  }

  var source = sheet.getRange(row, 1, 1, width);
  var values = source.getValues();              // snapshot BEFORE deleting.
  var destRow = Math.max(2, archive.getLastRow() + 1);
  var dest = archive.getRange(destRow, 1, 1, width);

  // Keep the cell formatting/RTL/alignment, but write the computed values so we
  // never carry live formulas into the Archive tab.
  source.copyTo(dest, { formatOnly: true });
  dest.setValues(values);

  sheet.deleteRow(row);
}


function _isActiveCellar(sheet) {
  // The Archive tab mirrors the cellar headers, so exclude it explicitly to
  // avoid the header fallback below matching it and archiving onto itself.
  if (sheet.getName() === ARCHIVE_SHEET_NAME) return false;
  if (sheet.getName() === ACTIVE_CELLAR_NAME) return true;
  return _findHeaderColumn(sheet, CELLAR_HEADER_MARKER) > 0;
}


function _statusColumn(sheet) {
  return _findHeaderColumn(sheet, STATUS_HEADER_MARKER);
}


function _findHeaderColumn(sheet, headerName) {
  // 1-indexed column whose row-1 header equals headerName, else -1.
  var lastCol = Math.max(1, sheet.getLastColumn());
  var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  for (var c = 0; c < headers.length; c++) {
    if (String(headers[c]).trim() === headerName) return c + 1;
  }
  return -1;
}


/**
 * One-off sweep: archive every bottle already marked "Finished". Run this once
 * from the editor (Run -> archiveFinishedNow) to clear bottles that were marked
 * Finished before this trigger existed.
 */
function archiveFinishedNow() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(ACTIVE_CELLAR_NAME);
  if (!sheet || !_isActiveCellar(sheet)) {
    var sheets = ss.getSheets();
    sheet = null;
    for (var i = 0; i < sheets.length; i++) {
      if (_isActiveCellar(sheets[i])) { sheet = sheets[i]; break; }
    }
  }
  if (!sheet) throw new Error("Active cellar tab not found");

  var statusCol = _statusColumn(sheet);
  if (statusCol < 1) throw new Error("Status column '" + STATUS_HEADER_MARKER + "' not found");

  var moved = 0;
  for (var row = sheet.getLastRow(); row >= 2; row--) {
    if (String(sheet.getRange(row, statusCol).getValue()).trim() === FINISHED_STATUS) {
      _archiveRow(sheet, row);
      moved++;
    }
  }
  return moved + " row(s) archived";
}
