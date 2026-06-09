/**
 * Google Apps Script for the Sommelier Bot.
 *
 * This single Web App backs TWO features, both writing as the deploying user
 * (Roy). We deliberately reuse ONE deployment so there is no second Google auth
 * path: the same "execute as me" identity already owns every target sheet.
 *
 *   1. Chat memory  (original) - reads/writes the bound "Sommelier Memory" sheet.
 *   2. Wine cellar ingestion (/addwine) - opens the cellar spreadsheet by ID and
 *      appends rows, plus a tiny KV store for the /addwine + /editwine state.
 *      /editwine additionally lists rows (list_wines) and overwrites one row's
 *      columns A-N in place (update_wine), located by sheet row index.
 *
 * NOTE: auto-archiving of "Finished" bottles is NOT handled here. It lives in
 * a separate Apps Script bound to the cellar spreadsheet itself (an onEdit
 * trigger); see cellar_apps_script.js in this repo.
 *
 * SETUP / REDEPLOY (must be done in the Apps Script editor, not by the bot):
 *   1. Open the "Sommelier Memory" Google Sheet -> Extensions -> Apps Script.
 *   2. Paste this entire file, replacing everything.
 *   3. Confirm CELLAR_FILE_ID below points at your cellar spreadsheet.
 *   4. Deploy -> Manage deployments -> edit the existing Web App deployment ->
 *      "New version" -> Deploy. (Reusing the deployment keeps the same URL, so
 *      SHEETS_MEMORY_URL does not change.)
 *   5. Execute as: "Me". Who has access: "Anyone".
 *   6. SECURITY: Project Settings -> Script Properties -> add
 *      BOT_SECRET = <the same value as the bot's SHEETS_SECRET env var>. The
 *      deployment is "Anyone" (Google requires this for an unauthenticated
 *      webhook), so this shared secret is the ONLY thing standing between the
 *      open URL and your sheets. Until BOT_SECRET is set the endpoint stays
 *      open for backward compatibility — set it.
 *
 * Memory sheet (bound spreadsheet, first/active sheet) headers in row 1, A-D:
 *   A1: Chat ID | B1: Active History | C1: Long Term Summary | D1: Last Updated
 *
 * Backward compatibility: requests with no "action" field are treated as the
 * original memory protocol, so the running bot keeps working before redeploy.
 */

// reason: the cellar lives in a DIFFERENT spreadsheet than this bound script,
// so we must open it explicitly by ID. The deploying user owns it -> write access.
var CELLAR_FILE_ID = "1xMwKiTr7JZ__vcLBKQrUTR8it__dQVCnHfd9k3_wZxo";

// Header that identifies the main cellar tab at runtime (spec: do not assume gid).
var CELLAR_HEADER_MARKER = "יקב"; // "יקב" (winery), column A header.
var CELLAR_WRITE_COLS = 14;                      // Columns A-N only. Never O/P/Q.
var STATUS_HEADER_MARKER = "סטטוס חדש";          // status column (Open/Closed/Finished), lives outside A-N.

var STATE_SHEET_NAME = "AddWine State";          // KV tab for /addwine conversation.


/**
 * Shared-secret gate. Returns true when BOT_SECRET is unset (legacy: keeps the
 * running bot working before the property is configured) or when the caller's
 * key matches. Set BOT_SECRET in Script Properties to close the open back door.
 */
function _authorized(provided) {
  var expected = PropertiesService.getScriptProperties().getProperty("BOT_SECRET");
  if (!expected) return true;
  return String(provided || "") === expected;
}


function doGet(e) {
  if (!_authorized(e && e.parameter && e.parameter.key)) {
    return _jsonOut({"error": "unauthorized"});
  }
  var action = (e.parameter && e.parameter.action) || "memory";

  if (action === "addwine_state") {
    return _jsonOut(_stateGet(e.parameter.chat_id));
  }
  if (action === "list_wines") {
    return _jsonOut(_listWines());
  }
  // Default: original memory read protocol (unchanged).
  return _jsonOut(_memoryGet(e.parameter.chat_id));
}


function doPost(e) {
  var payload;
  try {
    payload = JSON.parse(e.postData.contents);
  } catch (err) {
    return _jsonOut({"error": "Bad JSON: " + err});
  }

  if (!_authorized(payload.key)) {
    return _jsonOut({"error": "unauthorized"});
  }

  var action = payload.action || "memory";

  try {
    switch (action) {
      case "add_wine":       return _jsonOut(_addWine(payload));
      case "update_wine":    return _jsonOut(_updateWine(payload));
      case "addwine_state":  return _jsonOut(_stateSet(payload));
      default:               return _jsonOut(_memorySet(payload)); // memory (legacy)
    }
  } catch (err) {
    return _jsonOut({"error": err.toString()});
  }
}


// ====================================================================
// Memory protocol (original behavior, factored into helpers)
// ====================================================================

function _memoryGet(chatId) {
  if (!chatId) return {"error": "Missing chat_id"};

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var data = sheet.getDataRange().getValues();

  for (var i = 1; i < data.length; i++) {
    if (String(data[i][0]) === String(chatId)) {
      var activeHistory = [];
      try {
        activeHistory = data[i][1] ? JSON.parse(data[i][1]) : [];
      } catch (err) {}
      return {
        "chat_id": chatId,
        "active_history": activeHistory,
        "long_term_summary": data[i][2] || "",
        "updated_at": data[i][3] || 0
      };
    }
  }
  return {"chat_id": chatId, "active_history": [], "long_term_summary": "", "updated_at": 0};
}

function _memorySet(payload) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var chatId = String(payload.chat_id);
  var activeHistory = JSON.stringify(payload.active_history || []);
  var longTermSummary = payload.long_term_summary || "";
  var updatedAt = payload.updated_at || new Date().getTime() / 1000.0;

  var rowIndex = _findRowByKey(sheet, chatId);
  if (rowIndex > -1) {
    sheet.getRange(rowIndex, 2, 1, 3).setValues([[activeHistory, longTermSummary, updatedAt]]);
  } else {
    sheet.appendRow([chatId, activeHistory, longTermSummary, updatedAt]);
  }
  return {"status": "success"};
}


// ====================================================================
// /addwine: cellar append
// ====================================================================

function _addWine(payload) {
  // payload.rows is an array of rows, each an array of exactly 14 cells (A-N).
  // We accept a batch so multi-wine text input writes in a single setValues call.
  var rows = payload.rows;
  if (!rows || !rows.length) return {"error": "No rows to write"};

  for (var r = 0; r < rows.length; r++) {
    if (rows[r].length !== CELLAR_WRITE_COLS) {
      return {"error": "Row " + r + " has " + rows[r].length + " cols, expected " + CELLAR_WRITE_COLS};
    }
  }

  var sheet = _getCellarSheet();
  // reason: getLastRow() can be inflated by a whole-column ARRAYFORMULA in O.
  // Anchor on the last filled WINE row (cols A/B) so we never overwrite data and
  // never land mid-formula-range.
  var firstRow = _lastFilledWineRow(sheet) + 1;

  sheet.getRange(firstRow, 1, rows.length, CELLAR_WRITE_COLS).setValues(rows);

  // Default the status column for the freshly added rows (new bottles are
  // unopened -> "Closed"). Located by header name so we never assume its
  // position and never overwrite the O/P/Q value/sort formulas. Silently
  // skipped if the column is absent.
  var status = payload.status || "Closed";
  var statusCol = _findHeaderColumn(sheet, STATUS_HEADER_MARKER);
  if (statusCol > 0) {
    var statusValues = [];
    for (var s = 0; s < rows.length; s++) statusValues.push([status]);
    sheet.getRange(firstRow, statusCol, rows.length, 1).setValues(statusValues);
  }

  return {
    "status": "success",
    "rows_written": rows.length,
    "first_row": firstRow,
    "tab": sheet.getName()
  };
}

// ====================================================================
// /editwine: list rows + update one row in place
// ====================================================================

function _listWines() {
  // Return every row that holds a wine (cols A or B non-empty), with its
  // 1-indexed sheet row, its A-N values, and its status cell. The row index is
  // the unambiguous handle /editwine uses to write the edit back.
  var sheet = _getCellarSheet();
  var lastRow = _lastFilledWineRow(sheet);
  var wines = [];
  if (lastRow < 2) return {"wines": wines};

  var statusCol = _findHeaderColumn(sheet, STATUS_HEADER_MARKER);
  var data = sheet.getRange(2, 1, lastRow - 1, CELLAR_WRITE_COLS).getValues();
  var statuses = (statusCol > 0)
    ? sheet.getRange(2, statusCol, lastRow - 1, 1).getValues()
    : null;

  for (var i = 0; i < data.length; i++) {
    var values = data[i];
    if (String(values[0]).trim() === "" && String(values[1]).trim() === "") {
      continue; // gap row (formula-only / blank) -> not a wine.
    }
    wines.push({
      "row": i + 2, // data starts at sheet row 2.
      "values": values,
      "status": statuses ? statuses[i][0] : ""
    });
  }
  return {"wines": wines};
}

function _updateWine(payload) {
  // payload: { row: <1-indexed>, values: [14 cells A-N], expect: {winery, wine_name} }
  // Overwrites ONLY columns A-N of that row. Never touches O/P/Q or the status.
  var row = payload.row;
  var values = payload.values;
  if (!row || row < 2) return {"error": "Bad row: " + row};
  if (!values || values.length !== CELLAR_WRITE_COLS) {
    return {"error": "Expected " + CELLAR_WRITE_COLS + " cells, got " +
                     (values ? values.length : 0)};
  }

  var sheet = _getCellarSheet();
  if (row > sheet.getLastRow()) return {"error": "Row " + row + " out of range"};

  // Identity guard: refuse to write if the row no longer holds the wine we
  // listed (e.g. rows were inserted/deleted in the meantime).
  var expect = payload.expect || {};
  if (expect.winery !== undefined || expect.wine_name !== undefined) {
    var current = sheet.getRange(row, 1, 1, 2).getValues()[0];
    if (String(current[0]).trim() !== String(expect.winery || "").trim() ||
        String(current[1]).trim() !== String(expect.wine_name || "").trim()) {
      return {"error": "row_mismatch", "row": row};
    }
  }

  sheet.getRange(row, 1, 1, CELLAR_WRITE_COLS).setValues([values]);
  return {"status": "success", "row": row, "tab": sheet.getName()};
}

function _findHeaderColumn(sheet, headerName) {
  // Returns the 1-indexed column whose row-1 header equals headerName, else -1.
  var lastCol = Math.max(1, sheet.getLastColumn());
  var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  for (var c = 0; c < headers.length; c++) {
    if (String(headers[c]).trim() === headerName) return c + 1;
  }
  return -1;
}

function _getCellarSheet() {
  var ss = SpreadsheetApp.openById(CELLAR_FILE_ID);
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    // Match the header row by the winery marker so we target the real cellar tab
    // regardless of its name/gid (spec: confirm tab at runtime).
    var headers = sheets[i].getRange(1, 1, 1, Math.max(1, sheets[i].getLastColumn())).getValues()[0];
    for (var c = 0; c < headers.length; c++) {
      if (String(headers[c]).trim() === CELLAR_HEADER_MARKER) {
        return sheets[i];
      }
    }
  }
  throw new Error("Cellar tab not found (no header '" + CELLAR_HEADER_MARKER + "')");
}

function _lastFilledWineRow(sheet) {
  // Scan columns A and B (winery, wine name) bottom-up for the last row that
  // actually holds a wine, ignoring formula-only columns further right.
  var lastRow = sheet.getLastRow();
  if (lastRow < 1) return 1;
  var values = sheet.getRange(1, 1, lastRow, 2).getValues();
  for (var i = values.length - 1; i >= 0; i--) {
    if (String(values[i][0]).trim() !== "" || String(values[i][1]).trim() !== "") {
      return i + 1; // 1-indexed row of the last real wine (or the header row).
    }
  }
  return 1;
}


// ====================================================================
// /addwine: conversation state KV (serverless bot has no in-memory store)
// ====================================================================

function _stateSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(STATE_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(STATE_SHEET_NAME);
    sheet.appendRow(["Chat ID", "State JSON", "Updated At"]);
  }
  return sheet;
}

function _stateGet(chatId) {
  if (!chatId) return {"error": "Missing chat_id"};
  var sheet = _stateSheet();
  var rowIndex = _findRowByKey(sheet, String(chatId));
  if (rowIndex < 0) return {"chat_id": chatId, "state": null, "updated_at": 0};

  var row = sheet.getRange(rowIndex, 1, 1, 3).getValues()[0];
  var state = null;
  try {
    state = row[1] ? JSON.parse(row[1]) : null;
  } catch (err) {}
  return {"chat_id": chatId, "state": state, "updated_at": row[2] || 0};
}

function _stateSet(payload) {
  var sheet = _stateSheet();
  var chatId = String(payload.chat_id);
  var rowIndex = _findRowByKey(sheet, chatId);

  // A null/absent state means "clear" -> delete the row so the bot exits the flow.
  if (payload.state === null || payload.state === undefined) {
    if (rowIndex > -1) sheet.deleteRow(rowIndex);
    return {"status": "cleared"};
  }

  var stateJson = JSON.stringify(payload.state);
  var updatedAt = payload.updated_at || new Date().getTime() / 1000.0;
  if (rowIndex > -1) {
    sheet.getRange(rowIndex, 2, 1, 2).setValues([[stateJson, updatedAt]]);
  } else {
    sheet.appendRow([chatId, stateJson, updatedAt]);
  }
  return {"status": "success"};
}


// ====================================================================
// Shared helpers
// ====================================================================

function _findRowByKey(sheet, key) {
  // Returns the 1-indexed sheet row whose column A equals key, else -1.
  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    if (String(data[i][0]) === key) return i + 1;
  }
  return -1;
}

function _jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
