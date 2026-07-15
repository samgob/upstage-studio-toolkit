#!/usr/bin/env python3
"""
Upstage Batch Processor — Web GUI (Enhanced)
==============================================
A cross-platform browser-based GUI for upstage_batch.py.

Features:
  - Server-side folder browser (navigate filesystem from the UI)
  - Drag-and-drop path input
  - Live progress bar + color-coded log streaming
  - Inline results table with sort/filter when batch completes
  - Click-to-expand JSON viewer for each document's extraction output
  - Resume, dry-run, and all batch options

Requirements: Python 3.8+, upstage_batch.py in the parent directory.
Works on macOS, Windows, and Linux. Zero third-party dependencies.

Usage:
    python3 upstage_batch_gui.py              # opens browser automatically
    python3 upstage_batch_gui.py --port 9090  # custom port

Author: Upstage AI — Solutions Engineering
"""

__version__ = "2.0.0"

import http.server
import json
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# =============================================================================
# CONFIG
# =============================================================================

HOST = "127.0.0.1"
DEFAULT_PORT = 8484

# The actual bound port, set in main() once the socket is open. Used to validate
# the Host header on every request (DNS-rebinding defense — see _host_ok).
SERVER_PORT = DEFAULT_PORT

# Find batch script — check same dir, then parent dir
SCRIPT_DIR = Path(__file__).parent
BATCH_SCRIPT = SCRIPT_DIR / "upstage_batch.py"
if not BATCH_SCRIPT.exists():
    BATCH_SCRIPT = SCRIPT_DIR.parent / "upstage_batch.py"

# Global state
_state = {
    "process": None,
    "is_running": False,
    "log_lines": [],
    "total_docs": 0,
    "completed_docs": 0,
    "status": "ready",
    "results": [],       # parsed results for the results table
    "results_dir": "",   # path to results directory
    "stop_requested": False,  # user clicked Stop at least once (graceful)
    "stop_force": False,      # user clicked Stop twice (force terminate)
}
_state_lock = threading.Lock()


# =============================================================================
# HTML TEMPLATE  (single-page app)
# =============================================================================

def get_html():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Upstage Batch Processor</title>
<style>
  /* System font stack only — no external font fetch, so the GUI works fully
     offline and never phones home to a third party. */

  :root {
    --bg: #191722;
    --card: #1E1B2E;
    --card-border: #2D2A3E;
    --input-bg: #252237;
    --input-fg: #E8E6F0;
    --input-border: #3A3650;
    --accent: #6C63FF;
    --accent-hover: #7B73FF;
    --accent-active: #5A52E0;
    --text: #E8E6F0;
    --text-dim: #8B87A0;
    --text-muted: #5D5977;
    --success: #4ADE80;
    --error: #F87171;
    --warning: #FBBF24;
    --log-bg: #13111D;
    --log-fg: #C4C0D8;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; padding: 32px 24px;
  }
  .container { max-width: 840px; margin: 0 auto; }

  /* Header */
  .header h1 {
    font-size: 24px; font-weight: 700; letter-spacing: -0.5px;
    background: linear-gradient(90deg, #6C63FF, #C2C6FF, #F3BCFC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .header .subtitle { color: var(--text-muted); font-size: 12px; margin-top: 4px; }

  /* Cards */
  .card {
    background: var(--card); border: 1px solid var(--card-border);
    border-radius: 12px; padding: 20px 24px; margin-top: 16px;
  }
  .card-title {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.5px; color: var(--text-dim); margin-bottom: 14px;
  }

  /* Form */
  label { display: block; font-size: 12px; color: var(--text-dim); margin-bottom: 4px; margin-top: 12px; }
  label:first-child { margin-top: 0; }
  input[type="text"], input[type="password"] {
    width: 100%; padding: 8px 12px; font-size: 13px;
    font-family: 'Inter', sans-serif;
    background: var(--input-bg); color: var(--input-fg);
    border: 1px solid var(--input-border); border-radius: 6px;
    outline: none; transition: border-color 0.2s;
  }
  input:focus { border-color: var(--accent); }
  input[type="number"] {
    width: 64px; padding: 6px 8px; font-size: 13px;
    font-family: 'Inter', sans-serif;
    background: var(--input-bg); color: var(--input-fg);
    border: 1px solid var(--input-border); border-radius: 6px; outline: none;
  }

  /* Mode pills */
  .mode-group { display: flex; gap: 4px; flex-wrap: wrap; }
  .mode-group .pill {
    display: inline-flex; align-items: center; padding: 6px 14px;
    border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 500;
    color: var(--text-dim); border: 1px solid var(--card-border);
    transition: all 0.15s; user-select: none;
  }
  .mode-group .pill:hover { border-color: var(--accent); color: var(--text); }
  .mode-group .pill.active {
    background: rgba(108,99,255,0.15); border-color: var(--accent); color: var(--text);
  }

  /* Path input with browse button */
  .path-row { display: flex; gap: 8px; align-items: center; }
  .path-row input { flex: 1; }
  .btn-browse {
    padding: 7px 14px; font-size: 11px; font-weight: 500;
    font-family: 'Inter', sans-serif;
    color: var(--text-dim); background: var(--card-border); border: none;
    border-radius: 6px; cursor: pointer; white-space: nowrap;
    transition: all 0.15s;
  }
  .btn-browse:hover { color: var(--text); background: var(--input-border); }

  /* Key row */
  .key-row { display: flex; gap: 8px; align-items: center; }
  .key-row input { flex: 1; }
  .key-toggle {
    font-size: 11px; color: var(--text-muted); cursor: pointer;
    background: none; border: 1px solid var(--card-border);
    border-radius: 4px; padding: 4px 10px; font-family: 'Inter', sans-serif;
  }
  .key-toggle:hover { color: var(--text); border-color: var(--text-muted); }

  /* Drop zone */
  .drop-zone {
    border: 2px dashed var(--card-border); border-radius: 8px;
    padding: 20px; text-align: center; color: var(--text-muted);
    font-size: 12px; transition: all 0.2s; margin-top: 8px; cursor: pointer;
  }
  .drop-zone.drag-over {
    border-color: var(--accent); background: rgba(108,99,255,0.08);
    color: var(--text);
  }
  .drop-zone .drop-icon { font-size: 24px; margin-bottom: 4px; display: block; }
  .drop-zone .drop-hint { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

  /* Options */
  .options-row { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  .options-row label {
    display: inline-flex; align-items: center; gap: 6px;
    margin: 0; cursor: pointer; font-size: 12px;
  }
  .options-row input[type="checkbox"] { accent-color: var(--accent); }

  /* Buttons */
  .btn-row { margin-top: 20px; display: flex; align-items: center; gap: 12px; }
  .btn-run {
    padding: 10px 28px; font-size: 14px; font-weight: 600;
    font-family: 'Inter', sans-serif;
    color: #fff; background: var(--accent); border: none;
    border-radius: 8px; cursor: pointer; transition: background 0.15s;
  }
  .btn-run:hover { background: var(--accent-hover); }
  .btn-run:active { background: var(--accent-active); }
  .btn-run:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-stop {
    padding: 8px 20px; font-size: 13px; font-weight: 500;
    font-family: 'Inter', sans-serif;
    color: var(--error); background: var(--card);
    border: 1px solid var(--card-border); border-radius: 8px;
    cursor: pointer; transition: border-color 0.15s;
  }
  .btn-stop:hover { border-color: var(--error); }
  .btn-stop:disabled { opacity: 0.3; cursor: not-allowed; }
  .status-text { margin-left: auto; font-size: 12px; color: var(--text-dim); }

  /* Progress */
  .progress-wrap {
    margin-top: 16px; height: 6px; background: var(--card-border);
    border-radius: 3px; overflow: hidden;
  }
  .progress-fill {
    height: 100%; width: 0%; background: var(--accent);
    border-radius: 3px; transition: width 0.3s ease;
  }
  .progress-text { font-size: 11px; color: var(--text-muted); margin-top: 4px; text-align: right; }

  /* Log */
  .log-area {
    background: var(--log-bg); border: 1px solid var(--card-border);
    border-radius: 8px; padding: 12px 16px; height: 280px; overflow-y: auto;
    font-family: 'SF Mono','Menlo','Consolas', monospace;
    font-size: 11px; line-height: 1.5; color: var(--log-fg);
    white-space: pre-wrap; word-break: break-all;
  }
  .log-area .ok { color: var(--success); }
  .log-area .fail { color: var(--error); }
  .log-area .warn { color: var(--warning); }
  .log-area .dim { color: var(--text-muted); }

  .help-text { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

  /* =========== FOLDER BROWSER MODAL =========== */
  .modal-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.6); z-index: 1000;
    align-items: center; justify-content: center;
  }
  .modal-overlay.show { display: flex; }
  .modal {
    background: var(--card); border: 1px solid var(--card-border);
    border-radius: 14px; width: 520px; max-height: 80vh;
    display: flex; flex-direction: column; overflow: hidden;
  }
  .modal-header {
    padding: 16px 20px; border-bottom: 1px solid var(--card-border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .modal-header h3 { font-size: 14px; font-weight: 600; }
  .modal-close {
    background: none; border: none; color: var(--text-muted);
    font-size: 18px; cursor: pointer; padding: 4px;
  }
  .modal-close:hover { color: var(--text); }

  .modal-breadcrumb {
    padding: 8px 20px; font-size: 11px; color: var(--text-muted);
    border-bottom: 1px solid var(--card-border);
    display: flex; align-items: center; gap: 4px; flex-wrap: wrap;
    background: var(--input-bg);
  }
  .modal-breadcrumb .crumb {
    cursor: pointer; color: var(--accent); padding: 2px 4px; border-radius: 3px;
  }
  .modal-breadcrumb .crumb:hover { background: rgba(108,99,255,0.15); }
  .modal-breadcrumb .sep { color: var(--text-muted); }

  .modal-body {
    flex: 1; overflow-y: auto; padding: 8px 0;
    min-height: 200px; max-height: 400px;
  }
  .folder-item {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 20px; cursor: pointer; font-size: 12px;
    color: var(--text); transition: background 0.1s;
  }
  .folder-item:hover { background: rgba(108,99,255,0.08); }
  .folder-item.selected { background: rgba(108,99,255,0.15); }
  .folder-item .icon { font-size: 16px; width: 20px; text-align: center; }
  .folder-item .name { flex: 1; }
  .folder-item .meta { font-size: 10px; color: var(--text-muted); }

  .modal-footer {
    padding: 12px 20px; border-top: 1px solid var(--card-border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .modal-footer .selected-path {
    font-size: 11px; color: var(--text-muted); flex: 1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .btn-select {
    padding: 6px 20px; font-size: 12px; font-weight: 600;
    font-family: 'Inter', sans-serif;
    color: #fff; background: var(--accent); border: none;
    border-radius: 6px; cursor: pointer; margin-left: 12px;
  }
  .btn-select:hover { background: var(--accent-hover); }

  .folder-loading {
    padding: 40px; text-align: center; color: var(--text-muted); font-size: 12px;
  }

  /* =========== RESULTS TABLE =========== */
  .results-section {
    display: none; margin-top: 16px;
    animation: fadeIn 0.3s ease;
  }
  .results-section.show { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

  .results-stats {
    display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap;
  }
  .stat-chip {
    padding: 8px 16px; border-radius: 8px; font-size: 12px;
    background: rgba(255,255,255,0.04); border: 1px solid var(--card-border);
  }
  .stat-chip .stat-val { font-size: 20px; font-weight: 700; display: block; }
  .stat-chip .stat-lbl { font-size: 10px; color: var(--text-muted); text-transform: uppercase; }

  .results-controls {
    display: flex; gap: 8px; align-items: center; margin-bottom: 10px;
  }
  .results-controls input[type="text"] {
    width: 240px; padding: 6px 10px; font-size: 11px;
  }
  .filter-pills { display: flex; gap: 4px; }
  .filter-pill {
    padding: 4px 10px; font-size: 10px; font-weight: 500;
    border: 1px solid var(--card-border); border-radius: 4px;
    cursor: pointer; color: var(--text-dim); background: none;
    font-family: 'Inter', sans-serif; transition: all 0.15s;
  }
  .filter-pill:hover { border-color: var(--accent); }
  .filter-pill.active { background: rgba(108,99,255,0.15); border-color: var(--accent); color: var(--text); }

  .results-table {
    width: 100%; border-collapse: collapse; font-size: 11px;
  }
  .results-table th {
    text-align: left; padding: 8px 10px; font-size: 10px;
    font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px;
    color: var(--text-muted); border-bottom: 1px solid var(--card-border);
    cursor: pointer; user-select: none; white-space: nowrap;
  }
  .results-table th:hover { color: var(--text); }
  .results-table th .sort-arrow { margin-left: 4px; font-size: 9px; }
  .results-table td {
    padding: 7px 10px; border-bottom: 1px solid rgba(255,255,255,0.04);
    vertical-align: top;
  }
  .results-table tr:hover { background: rgba(255,255,255,0.03); }
  .results-table .status-ok { color: var(--success); font-weight: 600; }
  .results-table .status-fail { color: var(--error); font-weight: 600; }
  .results-table .doc-name {
    max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .results-table .btn-json {
    padding: 2px 8px; font-size: 10px; font-family: 'Inter', sans-serif;
    background: rgba(108,99,255,0.1); color: var(--accent); border: 1px solid rgba(108,99,255,0.3);
    border-radius: 4px; cursor: pointer; transition: all 0.15s;
  }
  .results-table .btn-json:hover { background: rgba(108,99,255,0.2); }

  /* JSON viewer row */
  .json-row td { padding: 0; }
  .json-viewer {
    background: var(--log-bg); padding: 12px 16px; border-radius: 0;
    font-family: 'SF Mono','Menlo','Consolas', monospace;
    font-size: 11px; line-height: 1.5; color: var(--log-fg);
    max-height: 400px; overflow: auto; white-space: pre-wrap;
    border-top: 1px solid var(--card-border);
    border-bottom: 1px solid var(--card-border);
  }
  .json-key { color: #C2C6FF; }
  .json-str { color: #F3BCFC; }
  .json-num { color: var(--success); }
  .json-bool { color: var(--warning); }
  .json-null { color: var(--text-muted); }
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>Upstage Batch Processor</h1>
    <div class="subtitle">Web GUI v""" + __version__ + r"""</div>
  </div>

  <!-- MODE -->
  <div class="card">
    <div class="card-title">Processing Mode</div>
    <div class="mode-group" id="modeGroup">
      <div class="pill active" onclick="setMode('agent', this)">Studio Agent (V2)</div>
      <div class="pill" onclick="setMode('v1-parse', this)">Parse (V1)</div>
      <div class="pill" onclick="setMode('v1-extract', this)">Extract (V1)</div>
      <div class="pill" onclick="setMode('v1-classify', this)">Classify (V1)</div>
    </div>
  </div>

  <!-- CONFIG -->
  <div class="card">
    <div class="card-title">Configuration</div>

    <label>API Key</label>
    <div class="key-row">
      <input type="password" id="apiKey" placeholder="up_your-key-here">
      <button class="key-toggle" onclick="toggleKey()">Show</button>
    </div>

    <div id="agentIdRow">
      <label>Agent ID</label>
      <input type="text" id="agentId" placeholder="agt_XXXXX">
      <label style="margin-top:8px;">Config ID <span style="color:#888;font-size:0.85em;">(optional — uses default if blank)</span></label>
      <input type="text" id="configId" placeholder="cfg_XXXXX or leave blank">
    </div>

    <div id="schemaRow" style="display:none;">
      <label>Schema File</label>
      <div class="path-row">
        <input type="text" id="schemaPath" placeholder="/path/to/schema.json">
        <button class="btn-browse" onclick="openBrowser('schemaPath','file')">Browse</button>
      </div>
    </div>

    <label>Documents Folder</label>
    <div class="path-row">
      <input type="text" id="docsPath" placeholder="/path/to/documents/">
      <button class="btn-browse" onclick="openBrowser('docsPath','folder')">Browse</button>
    </div>

    <!-- Drop zone -->
    <div class="drop-zone" id="dropZone"
         ondragover="dragOver(event)" ondragleave="dragLeave(event)" ondrop="dropHandler(event)">
      <span class="drop-icon">&#128194;</span>
      Drop a folder here, or drag a folder from Finder / Explorer
      <span class="drop-hint">The full folder path will be used</span>
    </div>

    <label>Output Folder <span style="color:var(--text-muted)">(optional)</span></label>
    <div class="path-row">
      <input type="text" id="outputPath" placeholder="Auto-generated if blank">
      <button class="btn-browse" onclick="openBrowser('outputPath','folder')">Browse</button>
    </div>
  </div>

  <!-- OPTIONS -->
  <div class="card">
    <div class="card-title">Options</div>
    <div class="options-row">
      <label>Workers: <input type="number" id="parallel" value="5" min="1" max="20"></label>
      <label><input type="checkbox" id="resume"> Resume</label>
      <label><input type="checkbox" id="dryrun"> Dry Run</label>
      <label><input type="checkbox" id="report" checked> HTML Report</label>
      <label><input type="checkbox" id="verbose" checked> Verbose</label>
    </div>
  </div>

  <!-- ACTIONS -->
  <div class="btn-row">
    <button class="btn-run" id="btnRun" onclick="runBatch()">&#9654; Run Batch</button>
    <button class="btn-stop" id="btnStop" onclick="stopBatch()" disabled
            title="Graceful stop: in-flight work finishes, checkpoint saved. Click twice to force-exit.">&#9632; Stop</button>
    <span class="status-text" id="statusText">Ready</span>
  </div>

  <!-- PROGRESS -->
  <div class="progress-wrap"><div class="progress-fill" id="progressBar"></div></div>
  <div class="progress-text" id="progressText"></div>

  <!-- LOG -->
  <div class="card" style="padding:0; margin-top:16px;">
    <div class="log-area" id="logArea"></div>
  </div>

  <!-- RESULTS (shown inline after batch completes) -->
  <div class="results-section" id="resultsSection">
    <div class="card">
      <div class="card-title">Results</div>

      <div class="results-stats" id="resultsStats"></div>

      <div class="results-controls">
        <input type="text" id="resultsSearch" placeholder="Search documents..." oninput="filterResults()">
        <div class="filter-pills">
          <button class="filter-pill active" onclick="setFilter('all', this)">All</button>
          <button class="filter-pill" onclick="setFilter('ok', this)">Passed</button>
          <button class="filter-pill" onclick="setFilter('fail', this)">Failed</button>
        </div>
      </div>

      <div style="overflow-x:auto;">
        <table class="results-table">
          <thead>
            <tr>
              <th onclick="sortResults('document')">Document <span class="sort-arrow" id="sort_document"></span></th>
              <th onclick="sortResults('success')" style="width:70px">Status <span class="sort-arrow" id="sort_success"></span></th>
              <th onclick="sortResults('duration_seconds')" style="width:80px">Duration <span class="sort-arrow" id="sort_duration_seconds"></span></th>
              <th onclick="sortResults('file_size')" style="width:70px">Size <span class="sort-arrow" id="sort_file_size"></span></th>
              <th style="width:60px">Data</th>
            </tr>
          </thead>
          <tbody id="resultsBody"></tbody>
        </table>
      </div>
    </div>
  </div>

</div>

<!-- FOLDER BROWSER MODAL -->
<div class="modal-overlay" id="browserModal">
  <div class="modal">
    <div class="modal-header">
      <h3 id="browserTitle">Select Folder</h3>
      <button class="modal-close" onclick="closeBrowser()">&times;</button>
    </div>
    <div class="modal-breadcrumb" id="browserBreadcrumb"></div>
    <div class="modal-body" id="browserBody">
      <div class="folder-loading">Loading...</div>
    </div>
    <div class="modal-footer">
      <div class="selected-path" id="browserSelectedPath">/</div>
      <button class="btn-select" onclick="selectBrowserPath()">Select</button>
    </div>
  </div>
</div>

<script>
// =============================================================================
// STATE
// =============================================================================
let currentMode = 'agent';
let polling = null;
let logOffset = 0;
let allResults = [];
let currentFilter = 'all';
let currentSort = { key: 'document', asc: true };
let browserTargetInput = null;
let browserCurrentPath = '';
let browserSelectMode = 'folder'; // 'folder' or 'file'
let openJsonRow = null;

// =============================================================================
// MODE
// =============================================================================
function setMode(mode, el) {
  currentMode = mode;
  document.querySelectorAll('#modeGroup .pill').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('agentIdRow').style.display = mode === 'agent' ? '' : 'none';
  document.getElementById('schemaRow').style.display =
    (mode === 'v1-extract' || mode === 'v1-classify') ? '' : 'none';
}

function toggleKey() {
  const inp = document.getElementById('apiKey');
  const btn = inp.nextElementSibling;
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = 'Hide'; }
  else { inp.type = 'password'; btn.textContent = 'Show'; }
}

// =============================================================================
// DRAG AND DROP
// =============================================================================
function dragOver(e) {
  e.preventDefault(); e.stopPropagation();
  document.getElementById('dropZone').classList.add('drag-over');
}
function dragLeave(e) {
  e.preventDefault(); e.stopPropagation();
  document.getElementById('dropZone').classList.remove('drag-over');
}
function dropHandler(e) {
  e.preventDefault(); e.stopPropagation();
  document.getElementById('dropZone').classList.remove('drag-over');

  // Try to get path from dropped items
  const items = e.dataTransfer.items;
  if (items && items.length > 0) {
    // For files/folders dropped from OS, we can get the path from text data
    const textData = e.dataTransfer.getData('text/plain');
    if (textData && textData.startsWith('/')) {
      document.getElementById('docsPath').value = textData.trim();
      return;
    }
    // For actual file entries (webkitGetAsEntry)
    const entry = items[0].webkitGetAsEntry ? items[0].webkitGetAsEntry() : null;
    if (entry) {
      // We can get the name but not the full path from browser security
      // Show a helpful message
      document.getElementById('docsPath').value = entry.name;
      document.getElementById('docsPath').focus();
    }
  }

  // Fallback: try files
  const files = e.dataTransfer.files;
  if (files.length > 0) {
    // Can't get full path from browser, but show the name
    const path = e.dataTransfer.getData('text/uri-list') || files[0].name;
    document.getElementById('docsPath').value = path;
  }
}

// =============================================================================
// FOLDER BROWSER
// =============================================================================
function openBrowser(inputId, mode) {
  browserTargetInput = inputId;
  browserSelectMode = mode;
  document.getElementById('browserTitle').textContent =
    mode === 'folder' ? 'Select Folder' : 'Select File';

  // Start from current value or home
  const currentVal = document.getElementById(inputId).value.trim();
  const startPath = currentVal || '~';

  document.getElementById('browserModal').classList.add('show');
  navigateTo(startPath);
}

function closeBrowser() {
  document.getElementById('browserModal').classList.remove('show');
}

function navigateTo(path) {
  browserCurrentPath = path;
  document.getElementById('browserSelectedPath').textContent = path;
  document.getElementById('browserBody').innerHTML = '<div class="folder-loading">Loading...</div>';

  fetch('/api/browse?path=' + encodeURIComponent(path))
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        document.getElementById('browserBody').innerHTML =
          '<div class="folder-loading" style="color:var(--error)">' + data.error + '</div>';
        return;
      }

      browserCurrentPath = data.current;
      document.getElementById('browserSelectedPath').textContent = data.current;

      // Breadcrumb
      renderBreadcrumb(data.current);

      // Items
      let html = '';

      // Parent directory
      if (data.parent) {
        html += '<div class="folder-item" ondblclick="navigateTo(\'' + escPath(data.parent) + '\')">' +
          '<span class="icon">&#128193;</span><span class="name">..</span>' +
          '<span class="meta">Parent folder</span></div>';
      }

      // Folders first, then files
      const folders = data.items.filter(i => i.is_dir);
      const files = data.items.filter(i => !i.is_dir);

      folders.forEach(item => {
        html += '<div class="folder-item" ' +
          'onclick="selectItem(this, \'' + escPath(item.path) + '\')" ' +
          'ondblclick="navigateTo(\'' + escPath(item.path) + '\')">' +
          '<span class="icon">&#128194;</span>' +
          '<span class="name">' + esc(item.name) + '</span>' +
          '<span class="meta">' + (item.count !== undefined ? item.count + ' items' : '') + '</span></div>';
      });

      if (browserSelectMode === 'file') {
        files.forEach(item => {
          html += '<div class="folder-item" onclick="selectItem(this, \'' + escPath(item.path) + '\')">' +
            '<span class="icon">&#128196;</span>' +
            '<span class="name">' + esc(item.name) + '</span>' +
            '<span class="meta">' + item.size + '</span></div>';
        });
      }

      if (!html) {
        html = '<div class="folder-loading">Empty folder</div>';
      }

      document.getElementById('browserBody').innerHTML = html;
    })
    .catch(err => {
      document.getElementById('browserBody').innerHTML =
        '<div class="folder-loading" style="color:var(--error)">Failed to load: ' + err + '</div>';
    });
}

function renderBreadcrumb(path) {
  const parts = path.split('/').filter(Boolean);
  let html = '<span class="crumb" onclick="navigateTo(\'/\')">/</span>';
  let cumulative = '';
  parts.forEach((part, i) => {
    cumulative += '/' + part;
    const p = cumulative;
    html += '<span class="sep">/</span>';
    if (i < parts.length - 1) {
      html += '<span class="crumb" onclick="navigateTo(\'' + escPath(p) + '\')">' + esc(part) + '</span>';
    } else {
      html += '<span style="color:var(--text)">' + esc(part) + '</span>';
    }
  });
  document.getElementById('browserBreadcrumb').innerHTML = html;
}

function selectItem(el, path) {
  document.querySelectorAll('.folder-item').forEach(f => f.classList.remove('selected'));
  el.classList.add('selected');
  browserCurrentPath = path;
  document.getElementById('browserSelectedPath').textContent = path;
}

function selectBrowserPath() {
  if (browserTargetInput && browserCurrentPath) {
    document.getElementById(browserTargetInput).value = browserCurrentPath;
  }
  closeBrowser();
}

function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escPath(s) { return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'"); }

// =============================================================================
// BATCH RUN
// =============================================================================
function runBatch() {
  const payload = {
    mode: currentMode,
    api_key: document.getElementById('apiKey').value.trim(),
    agent_id: document.getElementById('agentId').value.trim(),
    config_id: document.getElementById('configId').value.trim(),
    schema_path: (document.getElementById('schemaPath') || {}).value || '',
    docs_path: document.getElementById('docsPath').value.trim(),
    output_path: document.getElementById('outputPath').value.trim(),
    parallel: document.getElementById('parallel').value,
    resume: document.getElementById('resume').checked,
    dryrun: document.getElementById('dryrun').checked,
    report: document.getElementById('report').checked,
    verbose: document.getElementById('verbose').checked,
  };

  if (!payload.api_key) return alert('API Key is required');
  if (payload.mode === 'agent' && !payload.agent_id) return alert('Agent ID is required');
  if ((payload.mode === 'v1-extract' || payload.mode === 'v1-classify') && !payload.schema_path)
    return alert('Schema file path is required');
  if (!payload.docs_path) return alert('Documents folder is required');

  // Reset
  document.getElementById('logArea').innerHTML = '';
  document.getElementById('progressBar').style.width = '0%';
  document.getElementById('progressText').textContent = '';
  document.getElementById('resultsSection').classList.remove('show');
  logOffset = 0; allResults = [];

  document.getElementById('btnRun').disabled = true;
  document.getElementById('btnStop').disabled = false;
  document.getElementById('statusText').textContent = 'Starting...';
  document.getElementById('statusText').style.color = 'var(--text-dim)';

  fetch('/api/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }).then(r => r.json()).then(data => {
    if (data.error) {
      alert(data.error);
      document.getElementById('btnRun').disabled = false;
      document.getElementById('btnStop').disabled = true;
      document.getElementById('statusText').textContent = 'Error';
      return;
    }
    document.getElementById('statusText').textContent = 'Running...';
    startPolling();
  }).catch(err => {
    alert('Failed to start: ' + err);
    document.getElementById('btnRun').disabled = false;
    document.getElementById('btnStop').disabled = true;
  });
}

function stopBatch() {
  fetch('/api/stop', { method: 'POST' });
  document.getElementById('statusText').textContent = 'Stopping...';
}

function startPolling() {
  if (polling) clearInterval(polling);
  polling = setInterval(pollStatus, 800);
}

function pollStatus() {
  fetch('/api/status?offset=' + logOffset)
    .then(r => r.json())
    .then(data => {
      const logArea = document.getElementById('logArea');
      if (data.new_lines && data.new_lines.length > 0) {
        data.new_lines.forEach(entry => {
          const span = document.createElement('span');
          span.className = entry.tag || '';
          span.textContent = entry.text + '\n';
          logArea.appendChild(span);
        });
        logArea.scrollTop = logArea.scrollHeight;
        logOffset = data.log_length;
      }

      if (data.total_docs > 0) {
        const pct = Math.round((data.completed_docs / data.total_docs) * 100);
        document.getElementById('progressBar').style.width = pct + '%';
        document.getElementById('progressText').textContent =
          data.completed_docs + ' / ' + data.total_docs + ' documents (' + pct + '%)';
      }

      const status = data.status;
      const statusEl = document.getElementById('statusText');
      if (status === 'completed') {
        statusEl.textContent = 'Completed \u2713'; statusEl.style.color = 'var(--success)';
        done(); loadResults();
      } else if (status === 'failed') {
        statusEl.textContent = 'Failed'; statusEl.style.color = 'var(--error)';
        done(); loadResults();
      } else if (status === 'stopped') {
        statusEl.textContent = 'Stopped'; statusEl.style.color = 'var(--warning)';
        done(); loadResults();
      }
    }).catch(() => {});
}

function done() {
  clearInterval(polling); polling = null;
  document.getElementById('btnRun').disabled = false;
  document.getElementById('btnStop').disabled = true;
}

// =============================================================================
// RESULTS TABLE
// =============================================================================
function loadResults() {
  fetch('/api/results').then(r => r.json()).then(data => {
    if (!data.results || data.results.length === 0) return;
    allResults = data.results;
    showResults();
  }).catch(() => {});
}

function showResults() {
  const section = document.getElementById('resultsSection');
  section.classList.add('show');

  // Stats
  const total = allResults.length;
  const ok = allResults.filter(r => r.success).length;
  const failed = total - ok;
  const avgDur = total > 0 ? (allResults.reduce((s, r) => s + (r.duration_seconds || 0), 0) / total) : 0;

  document.getElementById('resultsStats').innerHTML =
    '<div class="stat-chip"><span class="stat-val">' + total + '</span><span class="stat-lbl">Total</span></div>' +
    '<div class="stat-chip"><span class="stat-val" style="color:var(--success)">' + ok + '</span><span class="stat-lbl">Passed</span></div>' +
    '<div class="stat-chip"><span class="stat-val" style="color:var(--error)">' + failed + '</span><span class="stat-lbl">Failed</span></div>' +
    '<div class="stat-chip"><span class="stat-val">' + avgDur.toFixed(1) + 's</span><span class="stat-lbl">Avg Duration</span></div>';

  renderResultsTable();

  // Scroll to results
  setTimeout(() => section.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100);
}

function renderResultsTable() {
  let filtered = [...allResults];

  // Filter
  if (currentFilter === 'ok') filtered = filtered.filter(r => r.success);
  else if (currentFilter === 'fail') filtered = filtered.filter(r => !r.success);

  // Search
  const q = (document.getElementById('resultsSearch').value || '').toLowerCase();
  if (q) filtered = filtered.filter(r => (r.document || '').toLowerCase().includes(q));

  // Sort
  filtered.sort((a, b) => {
    let va = a[currentSort.key], vb = b[currentSort.key];
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    if (va < vb) return currentSort.asc ? -1 : 1;
    if (va > vb) return currentSort.asc ? 1 : -1;
    return 0;
  });

  // Render
  let html = '';
  filtered.forEach((r, idx) => {
    const statusCls = r.success ? 'status-ok' : 'status-fail';
    const statusTxt = r.success ? '\u2713 OK' : '\u2717 FAIL';
    const dur = (r.duration_seconds || 0).toFixed(1) + 's';
    const size = formatSize(r.file_size || 0);
    const hasData = r.final_output || r.extracted || r.error;
    const rowId = 'row_' + idx;

    html += '<tr>' +
      '<td class="doc-name" title="' + esc(r.document || '') + '">' + esc(r.document || '?') + '</td>' +
      '<td class="' + statusCls + '">' + statusTxt + '</td>' +
      '<td>' + dur + '</td>' +
      '<td>' + size + '</td>' +
      '<td>' + (hasData ? '<button class="btn-json" onclick="toggleJson(' + idx + ', \'' + rowId + '\')">View</button>' : '-') + '</td>' +
      '</tr>';
    html += '<tr class="json-row" id="' + rowId + '" style="display:none"><td colspan="5"></td></tr>';
  });

  document.getElementById('resultsBody').innerHTML = html;
}

function setFilter(f, el) {
  currentFilter = f;
  document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  renderResultsTable();
}

function filterResults() { renderResultsTable(); }

function sortResults(key) {
  if (currentSort.key === key) currentSort.asc = !currentSort.asc;
  else { currentSort.key = key; currentSort.asc = true; }
  // Update arrows
  document.querySelectorAll('.sort-arrow').forEach(a => a.textContent = '');
  const arrow = document.getElementById('sort_' + key);
  if (arrow) arrow.textContent = currentSort.asc ? '\u25B2' : '\u25BC';
  renderResultsTable();
}

function toggleJson(idx, rowId) {
  const row = document.getElementById(rowId);
  if (row.style.display === 'none') {
    // Close any other open row
    if (openJsonRow && openJsonRow !== rowId) {
      document.getElementById(openJsonRow).style.display = 'none';
    }
    openJsonRow = rowId;
    const r = allResults[idx];
    const data = r.final_output || r.extracted || { error: r.error } || {};
    row.querySelector('td').innerHTML = '<div class="json-viewer">' + syntaxHighlight(data) + '</div>';
    row.style.display = '';
  } else {
    row.style.display = 'none';
    openJsonRow = null;
  }
}

function syntaxHighlight(obj) {
  const json = JSON.stringify(obj, null, 2);
  if (!json) return '';
  return json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"([^"]+)":/g, '<span class="json-key">"$1"</span>:')
    .replace(/: "([^"]*)"/g, ': <span class="json-str">"$1"</span>')
    .replace(/: (\d+\.?\d*)/g, ': <span class="json-num">$1</span>')
    .replace(/: (true|false)/g, ': <span class="json-bool">$1</span>')
    .replace(/: (null)/g, ': <span class="json-null">$1</span>');
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}
</script>
</body>
</html>"""


# =============================================================================
# REQUEST HANDLER
# =============================================================================

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default logs

    def _host_ok(self):
        """Only serve requests whose Host header is loopback on our port.

        The server binds to 127.0.0.1, but a malicious web page the user visits
        can use DNS-rebinding to point its own hostname at 127.0.0.1:<port> and
        drive these endpoints (which include a filesystem browser). The browser
        still sends the attacker's hostname in the Host header, so rejecting any
        Host that isn't loopback:port closes that hole.
        """
        host = (self.headers.get("Host") or "").strip()
        allowed = {f"127.0.0.1:{SERVER_PORT}", f"localhost:{SERVER_PORT}"}
        if host in allowed:
            return True
        self.send_error(403, "Forbidden: unexpected Host header")
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._html(get_html())

        elif parsed.path == "/api/status":
            params = parse_qs(parsed.query)
            offset = int(params.get("offset", [0])[0])
            with _state_lock:
                new_lines = _state["log_lines"][offset:]
                resp = {
                    "status": _state["status"],
                    "total_docs": _state["total_docs"],
                    "completed_docs": _state["completed_docs"],
                    "log_length": len(_state["log_lines"]),
                    "new_lines": new_lines,
                }
            self._json(resp)

        elif parsed.path == "/api/browse":
            params = parse_qs(parsed.query)
            path = params.get("path", ["~"])[0]
            result = browse_directory(path)
            self._json(result)

        elif parsed.path == "/api/results":
            with _state_lock:
                self._json({"results": _state["results"]})

        else:
            self.send_error(404)

    def do_POST(self):
        if not self._host_ok():
            return
        parsed = urlparse(self.path)

        if parsed.path == "/api/run":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            result = start_batch(body)
            self._json(result)

        elif parsed.path == "/api/stop":
            stop_batch()
            self._json({"ok": True})

        else:
            self.send_error(404)

    def _html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode("utf-8"))


# =============================================================================
# FOLDER BROWSER
# =============================================================================

def browse_directory(path_str):
    """List contents of a directory for the browser modal."""
    try:
        # Expand ~ and resolve
        path = Path(os.path.expanduser(path_str)).resolve()

        if not path.exists():
            return {"error": f"Path does not exist: {path}"}
        if not path.is_dir():
            # If it's a file, go to its parent
            path = path.parent

        items = []
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return {"error": f"Permission denied: {path}"}

        for entry in entries:
            # Skip hidden files/folders
            if entry.name.startswith("."):
                continue
            try:
                item = {
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": entry.is_dir(),
                }
                if entry.is_dir():
                    # Count visible children
                    try:
                        item["count"] = sum(1 for c in entry.iterdir() if not c.name.startswith("."))
                    except (PermissionError, OSError):
                        item["count"] = 0
                else:
                    size = entry.stat().st_size
                    if size < 1024:
                        item["size"] = f"{size} B"
                    elif size < 1048576:
                        item["size"] = f"{size / 1024:.1f} KB"
                    else:
                        item["size"] = f"{size / 1048576:.1f} MB"
                items.append(item)
            except (PermissionError, OSError):
                continue

        # Parent directory
        parent = str(path.parent) if path.parent != path else None

        return {
            "current": str(path),
            "parent": parent,
            "items": items,
        }

    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# BATCH EXECUTION
# =============================================================================

def start_batch(config):
    """Validate and launch the batch subprocess."""
    with _state_lock:
        if _state["is_running"]:
            return {"error": "A batch is already running"}

    api_key = config.get("api_key", "").strip()
    if not api_key:
        return {"error": "API Key is required"}

    mode = config.get("mode", "agent")
    docs_path = config.get("docs_path", "").strip()
    if not docs_path:
        return {"error": "Documents folder is required"}

    # Expand ~ and resolve to absolute path
    docs_path = str(Path(os.path.expanduser(docs_path)).resolve())
    if not Path(docs_path).exists():
        return {"error": f"Documents folder does not exist: {docs_path}"}

    if mode == "agent" and not config.get("agent_id", "").strip():
        return {"error": "Agent ID is required for Studio Agent mode"}
    if mode in ("v1-extract", "v1-classify") and not config.get("schema_path", "").strip():
        return {"error": "Schema file required for V1 Extract/Classify"}

    if not BATCH_SCRIPT.exists():
        return {"error": f"Batch script not found at: {BATCH_SCRIPT}\nPlace upstage_batch.py in the same or parent directory."}

    # Build command
    cmd = [sys.executable, str(BATCH_SCRIPT), mode]

    if mode == "agent":
        cmd.extend(["--agent-id", config["agent_id"].strip()])
        config_id = config.get("config_id", "").strip()
        if config_id:
            cmd.extend(["--config-id", config_id])
    elif mode in ("v1-extract", "v1-classify"):
        schema = str(Path(os.path.expanduser(config["schema_path"].strip())).resolve())
        cmd.extend(["--schema", schema])

    cmd.extend(["--docs", docs_path])
    # The API key is passed to the child ONLY via the environment (below), never
    # on argv — command-line args are visible to any local user via `ps`.

    output_path = config.get("output_path", "").strip()
    if output_path:
        output_path = str(Path(os.path.expanduser(output_path)).resolve())
        cmd.extend(["--output", output_path])

    cmd.extend(["--workers", str(config.get("parallel", "5"))])

    if config.get("resume"):
        cmd.append("--resume")
    if config.get("dryrun"):
        cmd.append("--dry-run")
    if config.get("report"):
        cmd.append("--report")
    if config.get("verbose"):
        cmd.append("--verbose")

    # Reset state
    with _state_lock:
        _state["log_lines"] = []
        _state["total_docs"] = 0
        _state["completed_docs"] = 0
        _state["status"] = "running"
        _state["is_running"] = True
        _state["results"] = []
        _state["results_dir"] = ""
        _state["stop_requested"] = False
        _state["stop_force"] = False

    display_cmd = " ".join(cmd).replace(api_key, "up_****")
    _append_log(f"$ {display_cmd}", "dim")

    env = os.environ.copy()
    env["UPSTAGE_API_KEY"] = api_key
    env["PYTHONUNBUFFERED"] = "1"

    thread = threading.Thread(target=_run_process, args=(cmd, env), daemon=True)
    thread.start()

    return {"ok": True}


def stop_batch():
    """Request a graceful shutdown of the batch process.

    First call: send SIGINT so the child script runs its graceful-shutdown
    handler — finishes in-flight polls, writes the checkpoint, flushes logs,
    exits cleanly. Resume works normally afterward.

    Second call (within the same run): escalate to SIGTERM for a force
    terminate. Checkpoint state is still preserved for completed docs.
    """
    with _state_lock:
        proc = _state["process"]
        already_requested = _state["stop_requested"]
        _state["stop_requested"] = True
        if already_requested:
            _state["stop_force"] = True

    if not proc:
        return

    try:
        if already_requested:
            # Second click — force terminate
            _append_log(
                "Force-stop requested — terminating process. "
                "Completed docs are still saved; use Resume to continue.",
                "warn",
            )
            proc.terminate()
        else:
            # First click — graceful SIGINT. Child will finish in-flight work,
            # write the checkpoint, and exit cleanly.
            _append_log(
                "Stop requested — letting in-flight documents finish, then "
                "saving checkpoint. (Click Stop again to force-exit.)",
                "warn",
            )
            # On Windows, send_signal(SIGINT) only works for console groups;
            # fall back to terminate() for portability.
            if platform.system() == "Windows":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGINT)
    except Exception:
        pass


def _run_process(cmd, env):
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, bufsize=1,
        )
        with _state_lock:
            _state["process"] = proc

        for line in proc.stdout:
            line = line.rstrip("\n")
            _parse_line(line)

        proc.wait()
        rc = proc.returncode

        with _state_lock:
            user_stopped = _state["stop_requested"]

            # Graceful stop: child returns 0 from its clean-exit path,
            # but we should still label this "stopped" (not "completed")
            # so the user knows there's remaining work to resume.
            if user_stopped and rc in (0, -2, -15, 130):
                _state["status"] = "stopped"
                _append_log(
                    "Batch stopped cleanly. Checkpoint saved — click Resume "
                    "to pick up where this left off.",
                    "warn",
                )
            elif rc == 0:
                _state["status"] = "completed"
                _append_log("Batch completed successfully.", "ok")
            elif rc in (-15, -9):
                _state["status"] = "stopped"
                _append_log("Batch terminated.", "warn")
            else:
                _state["status"] = "failed"
                _append_log(f"Batch finished with exit code {rc}.", "fail")

        # Load results from disk
        _load_results()

    except Exception as e:
        _append_log(f"Error: {e}", "fail")
        with _state_lock:
            _state["status"] = "failed"
    finally:
        with _state_lock:
            _state["process"] = None
            _state["is_running"] = False


def _parse_line(line):
    m = re.search(r"Found (\d+) document", line)
    if m:
        with _state_lock:
            _state["total_docs"] = int(m.group(1))

    m = re.search(r"Processing (\d+) document", line)
    if m:
        with _state_lock:
            _state["total_docs"] = int(m.group(1))

    m = re.search(r"\[(\d+)/(\d+)\]\s+(OK|FAIL|STILL FAILING)", line)
    if m:
        with _state_lock:
            _state["completed_docs"] = int(m.group(1))
            _state["total_docs"] = int(m.group(2))

    m = re.search(r"Progress:\s+(\d+)/(\d+)\s+done", line)
    if m:
        with _state_lock:
            _state["completed_docs"] = int(m.group(1))
            _state["total_docs"] = int(m.group(2))

    # Capture results directory path
    m = re.search(r"Results:\s+(.+/results/?)$", line)
    if m:
        with _state_lock:
            _state["results_dir"] = m.group(1).strip().rstrip("/")

    # Capture summary path
    m = re.search(r"Summary:\s+(.+summary\.json)", line)
    if m:
        with _state_lock:
            if not _state["results_dir"]:
                # Derive results dir from summary path
                summary_path = Path(m.group(1).strip())
                _state["results_dir"] = str(summary_path.parent / "results")

    if "OK" in line and "[" in line:
        tag = "ok"
    elif "FAIL" in line or "ERROR" in line.upper():
        tag = "fail"
    elif "WARNING" in line.upper() or "Retrying" in line:
        tag = "warn"
    elif line.lstrip().startswith(">>") or "DRY RUN" in line:
        tag = "dim"
    else:
        tag = ""

    _append_log(line, tag)


def _append_log(text, tag=""):
    with _state_lock:
        _state["log_lines"].append({"text": text, "tag": tag})


def _load_results():
    """Load individual result JSON files from the results directory."""
    with _state_lock:
        results_dir = _state["results_dir"]

    if not results_dir:
        # Try to find summary.json from the log
        return

    results_path = Path(results_dir)
    if not results_path.exists():
        return

    results = []
    for f in sorted(results_path.glob("*.json")):
        try:
            with open(f) as fh:
                data = json.load(fh)
                results.append(data)
        except (json.JSONDecodeError, OSError):
            continue

    with _state_lock:
        _state["results"] = results


# =============================================================================
# MAIN
# =============================================================================

def main():
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # Try the requested port, then fall back to nearby ports
    server = None
    max_attempts = 20
    for attempt in range(max_attempts):
        try:
            server = http.server.HTTPServer((HOST, port), Handler)
            break
        except OSError as e:
            if e.errno == 48 or "Address already in use" in str(e):
                if attempt == 0:
                    print(f"Port {port} is in use, finding an open port...")
                port += 1
            else:
                raise

    if server is None:
        print(f"ERROR: Could not find an open port after {max_attempts} attempts.")
        sys.exit(1)

    # Record the actual bound port so the Host-header check accepts the real URL.
    global SERVER_PORT
    SERVER_PORT = port

    url = f"http://{HOST}:{port}"

    print(f"Upstage Batch Processor — Web GUI v{__version__}")
    print(f"Running at {url}")
    print(f"Batch script: {BATCH_SCRIPT}")
    print("Press Ctrl+C to stop.\n")

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def shutdown(sig, frame):
        print("\nShutting down...")
        stop_batch()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
