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
 *     their relative references recompute to garbage (e.g. a mangled "3Finished"
 *     cell). We copy the full look (formatting + dropdown chips) but then
 *     overwrite with a frozen snapshot of the computed values, keeping each row
 *     aligned column-for-column with the header.
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
  var archive = _ensureArchive(ss, sheet, width);

  // Grow the row count if the Archive grid is too short (copyTo/setValues
  // cannot write past the sheet's physical edge — appendRow could, but it
  // does not carry formatting, which is what caused misaligned rows before).
  var destRow = Math.max(2, archive.getLastRow() + 1);
  if (archive.getMaxRows() < destRow) {
    archive.insertRowsAfter(archive.getMaxRows(), destRow - archive.getMaxRows());
  }

  var source = sheet.getRange(row, 1, 1, width);
  var values = source.getValues();              // snapshot BEFORE deleting.
  var dest = archive.getRange(destRow, 1, 1, width);

  // Full copyTo carries formatting AND the data-validation dropdown chips, so
  // the Archive matches the cellar's look. setValues then overwrites with the
  // computed values, freezing them and stripping the cellar's live formulas
  // (which would otherwise recompute against Archive cells -> garbage like
  // "3Finished"). Column-for-column copy keeps every row aligned to the header.
  source.copyTo(dest);
  dest.setValues(values);

  sheet.deleteRow(row);
}


function _ensureArchive(ss, cellar, width) {
  var archive = ss.getSheetByName(ARCHIVE_SHEET_NAME);
  if (!archive) archive = ss.insertSheet(ARCHIVE_SHEET_NAME);

  // A trimmed Archive tab (empty columns deleted) is narrower than the cellar,
  // which makes copyTo/setValues throw "destination exceeds sheet size".
  if (archive.getMaxColumns() < width) {
    archive.insertColumnsAfter(archive.getMaxColumns(), width - archive.getMaxColumns());
  }
  // Seed the header (matching the cellar's look) on first use.
  if (archive.getLastRow() === 0) {
    var header = cellar.getRange(1, 1, 1, width);
    header.copyTo(archive.getRange(1, 1, 1, width));
    archive.getRange(1, 1, 1, width).setValues(header.getValues());
  }
  return archive;
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
