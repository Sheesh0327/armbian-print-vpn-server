import os
import subprocess
import tempfile
import time
import json
import re
import sqlite3
import hashlib
import secrets
import shutil
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, Response

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static')
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024  # 150MB file size limit

DB_PATH = "/var/lib/webprint/webprint.db"
SPOOL_PINNED_DIR = "/var/spool/webprint/pinned"
TEMP_DIR_BASE = "/tmp/webprint"

ACTIVE_TOKENS = {}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(SPOOL_PINNED_DIR, exist_ok=True)
    conn = get_db()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pinned_jobs (
                job_id TEXT PRIMARY KEY,
                file_path TEXT,
                filename TEXT,
                pin_hash TEXT,
                pages INTEGER,
                copies INTEGER,
                colormodel TEXT,
                printer TEXT,
                options_json TEXT,
                cost REAL,
                created_at INTEGER,
                expires_at INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS print_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                job_title TEXT,
                printer TEXT,
                pages INTEGER,
                copies INTEGER,
                total_pages INTEGER,
                colormodel TEXT,
                cost REAL,
                client_ip TEXT,
                status TEXT
            )
        """)
        defaults = {
            'admin_hash': hashlib.sha256('admin'.encode('utf-8')).hexdigest(),
            'currency_symbol': '₱',
            'rate_bw': '2.00',
            'rate_color': '5.00',
            'pin_expire_hours': '24'
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

def get_setting(key, default=''):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        if row:
            return row['value']
    except Exception:
        pass
    return default

def get_rate(is_color=False):
    key = 'rate_color' if is_color else 'rate_bw'
    val = get_setting(key, '5.00' if is_color else '2.00')
    try:
        return float(val)
    except Exception:
        return 5.0 if is_color else 2.0

def verify_admin_token(token):
    if not token:
        return False
    exp = ACTIVE_TOKENS.get(token)
    if exp and exp > time.time():
        return True
    if exp:
        del ACTIVE_TOKENS[token]
    return False

def record_ledger(title, printer, pages, copies, total_pages, colormodel, cost, client_ip, status):
    try:
        conn = get_db()
        with conn:
            conn.execute("""
                INSERT INTO print_ledger 
                (timestamp, job_title, printer, pages, copies, total_pages, colormodel, cost, client_ip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (int(time.time()), title, printer, pages, copies, total_pages, colormodel, cost, client_ip, status))
    except Exception:
        pass

def get_printers():
    printers = []
    default_printer = None
    try:
        res = subprocess.run(['lpstat', '-d'], capture_output=True, text=True, timeout=5)
        for line in res.stdout.splitlines():
            if 'system default destination:' in line:
                default_printer = line.split(':', 1)[1].strip()
    except Exception:
        pass

    try:
        res = subprocess.run(['lpstat', '-e'], capture_output=True, text=True, timeout=5)
        for line in res.stdout.splitlines():
            p_name = line.strip()
            if p_name and p_name not in printers:
                printers.append(p_name)
    except Exception:
        pass

    if not printers:
        try:
            res = subprocess.run(['lpstat', '-p'], capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                m = re.match(r'printer\s+(\S+)', line)
                if m and m.group(1) not in printers:
                    printers.append(m.group(1))
        except Exception:
            pass

    if not printers and default_printer:
        printers.append(default_printer)

    printer_details = []
    for p in printers:
        is_default = (p == default_printer)
        status = "idle"
        status_text = "Ready"
        try:
            stat_res = subprocess.run(['lpstat', '-p', p], capture_output=True, text=True, timeout=5)
            out_lower = stat_res.stdout.lower()
            if 'printing' in out_lower:
                status = "printing"
                status_text = "Printing"
            elif 'stopped' in out_lower or 'disabled' in out_lower:
                status = "stopped"
                status_text = "Stopped"
        except Exception:
            pass

        printer_details.append({
            'name': p,
            'is_default': is_default,
            'status': status,
            'status_text': status_text
        })

    return printer_details, default_printer

def get_pdf_page_count(pdf_path):
    try:
        res = subprocess.run(['pdfinfo', pdf_path], capture_output=True, text=True, timeout=10)
        for line in res.stdout.splitlines():
            if line.startswith('Pages:'):
                return int(line.split(':', 1)[1].strip())
    except Exception:
        pass
    return 1

def convert_to_pdf(input_path, original_filename):
    ext = os.path.splitext(original_filename)[1].lower()
    temp_dir = tempfile.mkdtemp(prefix="conv_")
    output_pdf = os.path.join(temp_dir, "converted.pdf")

    if ext == '.pdf':
        return input_path, None

    if ext in ['.heic', '.heif', '.avif', '.webp', '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif']:
        subprocess.run(['convert', input_path, '-auto-orient', output_pdf], check=True, timeout=30)
        return output_pdf, temp_dir

    if ext in ['.docx', '.doc', '.odt', '.rtf', '.txt', '.csv', '.xlsx', '.xls', '.pptx', '.ppt']:
        subprocess.run([
            'libreoffice', '--headless', '--convert-to', 'pdf',
            input_path, '--outdir', temp_dir
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
        base = os.path.splitext(os.path.basename(input_path))[0]
        conv_file = os.path.join(temp_dir, f"{base}.pdf")
        if os.path.exists(conv_file):
            return conv_file, temp_dir

    return input_path, None

INDEX_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="gray">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Print Server Appliance</title>
    <script>
        (function() {
            const savedTheme = localStorage.getItem('theme') || 'gray';
            document.documentElement.setAttribute('data-theme', savedTheme);
        })();
    </script>
    <script src="/static/pdf.min.js" onerror="this.onerror=null;this.src='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js'"></script>
    <script>
        window.switchTab = function(tabName) {
            try {
                var btns = document.querySelectorAll('.nav-btn');
                btns.forEach(function(b) {
                    b.classList.toggle('active', b.getAttribute('data-tab') === tabName);
                });
                var tabs = document.querySelectorAll('.tab-content');
                tabs.forEach(function(c) {
                    c.classList.toggle('active', c.id === 'tab-' + tabName);
                });
                if (tabName === 'idmaker') {
                    if (typeof initIdStudio === 'function') initIdStudio();
                } else if (tabName === 'pin') {
                    if (typeof loadPinList === 'function') loadPinList();
                } else if (tabName === 'history') {
                    if (typeof loadHistory === 'function') loadHistory();
                } else if (tabName === 'settings') {
                    if (typeof loadTelemetry === 'function') loadTelemetry();
                }
            } catch (err) {
                console.error("Tab switch error:", err);
            }
        };
    </script>
    <style>
        :root, html[data-theme="gray"], html[data-theme="dark"] {
            --radius-sm: 4px;
            --radius-md: 6px;
            --radius-lg: 8px;
            --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --transition-theme: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease;

            /* Chromium Gray Minimal Dark Palette */
            --bg: #202124;
            --surface: #282828;
            --surface-elevated: #35363a;
            --surface-hover: #3c4043;
            --border: #3c4043;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-hover: #5f6368;
            --desk-bg: #525659;
            --paper-bg: #ffffff;
            --paper-text: #202124;
            --paper-shadow: 0 4px 20px rgba(0, 0, 0, 0.45), 0 1px 4px rgba(0, 0, 0, 0.25);
            --margin-line: rgba(66, 133, 244, 0.6);
            --text-primary: #e8eaed;
            --text-secondary: #9aa0a6;
            --text-tertiary: #80868b;
            --accent: #8ab4f8;
            --accent-hover: #aecbfa;
            --btn-print-bg: #ffffff;
            --btn-print-text: #202124;
            --btn-cancel-border: #5f6368;
            --btn-cancel-text: #e8eaed;
            --input-bg: #35363a;
            --input-border: #4a4d51;
            --input-focus: #8ab4f8;
            --row-hover: rgba(255, 255, 255, 0.03);
            --badge-bg: #35363a;
            --success: #81c995;
            --danger: #f28b82;
            --warning: #fdd663;
        }

        html[data-theme="light"] {
            --radius-sm: 4px;
            --radius-md: 6px;
            --radius-lg: 8px;
            --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --transition-theme: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease;

            /* Chromium Light Palette */
            --bg: #f1f3f4;
            --surface: #ffffff;
            --surface-elevated: #f8f9fa;
            --surface-hover: #e8eaed;
            --border: #dadce0;
            --border-subtle: rgba(0, 0, 0, 0.06);
            --border-hover: #bdc1c6;
            --desk-bg: #d3d3d3;
            --paper-bg: #ffffff;
            --paper-text: #202124;
            --paper-shadow: 0 4px 16px rgba(0, 0, 0, 0.15), 0 1px 3px rgba(0, 0, 0, 0.1);
            --margin-line: rgba(26, 115, 232, 0.5);
            --text-primary: #202124;
            --text-secondary: #5f6368;
            --text-tertiary: #80868b;
            --accent: #1a73e8;
            --accent-hover: #174ea6;
            --btn-print-bg: #1a73e8;
            --btn-print-text: #ffffff;
            --btn-cancel-border: #dadce0;
            --btn-cancel-text: #1a73e8;
            --input-bg: #ffffff;
            --input-border: #dadce0;
            --input-focus: #1a73e8;
            --row-hover: rgba(0, 0, 0, 0.02);
            --badge-bg: #f1f3f4;
            --success: #1e8e3e;
            --danger: #d93025;
            --warning: #f9ab00;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background-color: var(--bg);
            color: var(--text-primary);
            font-family: var(--font);
            font-size: 13px;
            line-height: 1.5;
            -webkit-font-smoothing: antialiased;
            height: 100vh;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: var(--transition-theme);
        }

        /* Top Header Bar */
        header {
            height: 46px;
            border-bottom: 1px solid var(--border);
            padding: 0 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--surface);
            z-index: 50;
            flex-shrink: 0;
            gap: 16px;
        }

        .header-brand {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .header-title {
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.03em;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 6px;
            text-transform: uppercase;
        }

        .header-title .tag {
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            padding: 2px 6px;
            border-radius: 3px;
            background: var(--surface-elevated);
            color: var(--text-secondary);
            border: 1px solid var(--border);
        }

        .printer-select-wrapper {
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 3px 8px;
            font-size: 12px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--success);
            flex-shrink: 0;
        }
        .status-dot.stopped { background: var(--danger); }
        .status-dot.printing { background: var(--warning); animation: pulse 1.5s infinite; }

        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.4; }
            100% { opacity: 1; }
        }

        .header-nav {
            display: flex;
            gap: 3px;
            align-items: center;
            overflow-x: auto;
        }

        .nav-btn {
            background: transparent;
            border: 1px solid transparent;
            color: var(--text-secondary);
            font-size: 12px;
            font-weight: 500;
            padding: 5px 12px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.12s ease;
        }

        .nav-btn:hover {
            background: var(--surface-elevated);
            color: var(--text-primary);
        }
        .nav-btn.active {
            background: var(--surface-elevated);
            color: var(--text-primary);
            border-color: var(--border);
            font-weight: 600;
        }

        .header-controls {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .btn-theme, .btn-lock {
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 4px 9px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.12s;
        }
        .btn-theme:hover, .btn-lock:hover {
            color: var(--text-primary);
            border-color: var(--border-hover);
        }

        /* Main Viewport Container */
        main {
            flex: 1;
            width: 100%;
            height: calc(100vh - 46px);
            margin: 0;
            padding: 0;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .tab-content {
            display: none;
            width: 100%;
            height: 100%;
        }
        .tab-content.active {
            display: flex;
            flex-direction: column;
        }

        /* Chromium 2-Pane Dialog Layout */
        .cr-layout {
            display: flex;
            width: 100%;
            height: 100%;
            overflow: hidden;
        }

        @media (max-width: 900px) {
            .cr-layout {
                flex-direction: column;
                overflow-y: auto;
            }
            .cr-desk {
                min-height: 480px;
                height: 55vh;
            }
            .cr-sidebar {
                width: 100% !important;
                max-width: 100% !important;
                border-left: none !important;
                border-top: 1px solid var(--border);
                height: auto !important;
            }
        }

        /* Left Desk Canvas Area */
        .cr-desk {
            flex: 1;
            min-width: 0;
            background: var(--desk-bg);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: space-between;
            padding: 16px 20px;
            position: relative;
            overflow: hidden;
            user-select: none;
        }

        .cr-desk-viewport {
            flex: 1;
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            overflow: hidden;
        }

        .cr-desk-top-toolbar {
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 8px;
            z-index: 5;
        }

        .cr-doc-title {
            font-size: 13px;
            font-weight: 500;
            color: #ffffff;
            text-shadow: 0 1px 2px rgba(0,0,0,0.5);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 420px;
        }

        .cr-desk-pagination {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(0, 0, 0, 0.45);
            backdrop-filter: blur(4px);
            padding: 3px 10px;
            border-radius: var(--radius-sm);
            font-size: 12px;
            color: #ffffff;
        }

        .cr-page-btn {
            background: transparent;
            border: none;
            color: #ffffff;
            width: 22px;
            height: 22px;
            border-radius: 3px;
            cursor: pointer;
            font-size: 14px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .cr-page-btn:hover {
            background: rgba(255, 255, 255, 0.2);
        }

        .paper-sheet {
            background: var(--paper-bg);
            color: var(--paper-text);
            box-shadow: var(--paper-shadow);
            border-radius: 2px;
            position: relative;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            transition: width 0.2s cubic-bezier(0.16, 1, 0.3, 1),
                        height 0.2s cubic-bezier(0.16, 1, 0.3, 1),
                        filter 0.2s ease;
        }

        .printable-area {
            position: absolute;
            border: 1px dashed var(--margin-line);
            border-radius: 1px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            pointer-events: none;
            transition: top 0.15s ease, bottom 0.15s ease, left 0.15s ease, right 0.15s ease;
        }

        .content-render-wrap {
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .content-render-wrap canvas, .content-render-wrap img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            display: block;
        }
        .content-render-wrap.scale-fill canvas, .content-render-wrap.scale-fill img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .cr-desk-footer-pill {
            margin-top: 8px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            font-weight: 500;
            color: #e8eaed;
            background: rgba(0, 0, 0, 0.45);
            backdrop-filter: blur(4px);
            padding: 4px 14px;
            border-radius: 9999px;
        }

        .cr-empty-desk {
            text-align: center;
            color: #bdc1c6;
            position: absolute;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
            cursor: pointer;
            padding: 24px;
            border-radius: var(--radius-md);
            transition: background 0.15s;
        }
        .cr-empty-desk:hover {
            background: rgba(255, 255, 255, 0.04);
        }

        /* Right Settings Sidebar */
        .cr-sidebar {
            width: 330px;
            min-width: 330px;
            max-width: 340px;
            background: var(--surface);
            border-left: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            height: 100%;
            overflow-y: auto;
            overflow-x: hidden;
            user-select: none;
        }

        .cr-sidebar-header {
            padding: 16px 20px 12px;
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            border-bottom: 1px solid var(--border-subtle);
        }

        .cr-sidebar-title {
            font-size: 16px;
            font-weight: 500;
            color: var(--text-primary);
        }

        .cr-sidebar-sheet-count {
            font-size: 12px;
            color: var(--text-secondary);
        }

        .cr-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 9px 20px;
            min-height: 42px;
            transition: background 0.1s ease;
        }

        .cr-row:hover {
            background: var(--row-hover);
        }

        .cr-row-label {
            font-size: 13px;
            color: var(--text-secondary);
            font-weight: 400;
            flex: 1;
            padding-right: 8px;
        }

        .cr-row-control {
            width: 175px;
            display: flex;
            justify-content: flex-end;
            align-items: center;
        }

        .cr-select {
            width: 100%;
            background-color: var(--input-bg);
            color: var(--text-primary);
            border: 1px solid var(--input-border);
            border-radius: 4px;
            padding: 5px 24px 5px 10px;
            font-size: 12px;
            outline: none;
            appearance: none;
            background-image: url('data:image/svg+xml;charset=US-ASCII,<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6"><path fill="%239aa0a6" d="M0 0l5 5 5-5z"/></svg>');
            background-repeat: no-repeat;
            background-position: right 8px center;
            cursor: pointer;
            text-overflow: ellipsis;
            white-space: nowrap;
            overflow: hidden;
        }
        .cr-select:focus, .cr-input:focus {
            border-color: var(--input-focus);
        }

        .cr-input {
            width: 100%;
            background-color: var(--input-bg);
            color: var(--text-primary);
            border: 1px solid var(--input-border);
            border-radius: 4px;
            padding: 5px 8px;
            font-size: 12px;
            outline: none;
        }

        .cr-input-number {
            width: 70px;
            text-align: left;
        }

        .cr-checkbox-row {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 13px;
            color: var(--text-primary);
            cursor: pointer;
            width: 100%;
        }

        .cr-checkbox-row input[type="checkbox"] {
            width: 15px;
            height: 15px;
            accent-color: var(--accent);
            cursor: pointer;
        }

        /* More Settings Accordion */
        .cr-more-toggle {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 20px;
            color: var(--accent);
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            user-select: none;
            transition: background 0.1s;
            margin-top: 4px;
            border-top: 1px solid var(--border-subtle);
        }

        .cr-more-toggle:hover {
            background: var(--row-hover);
        }

        .cr-more-content {
            display: none;
            flex-direction: column;
        }

        .cr-more-content.open {
            display: flex;
        }

        .cr-system-hint {
            padding: 10px 20px 4px;
            font-size: 12px;
            color: var(--text-tertiary);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        /* Bottom Actions Bar */
        .cr-bottom-bar {
            margin-top: auto;
            padding: 14px 20px;
            border-top: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: flex-end;
            gap: 10px;
            background: var(--surface);
            position: sticky;
            bottom: 0;
            z-index: 10;
        }

        .cr-btn-pill {
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 500;
            padding: 6px 18px;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            transition: all 0.15s ease;
            outline: none;
        }

        .cr-btn-cancel {
            background: transparent;
            border: 1px solid var(--btn-cancel-border);
            color: var(--btn-cancel-text);
        }

        .cr-btn-cancel:hover {
            background: rgba(255, 255, 255, 0.08);
        }

        .cr-btn-print {
            background: var(--btn-print-bg);
            color: var(--btn-print-text);
            border: none;
            font-weight: 600;
            padding: 6px 22px;
        }

        .cr-btn-print:hover {
            filter: brightness(0.92);
        }
        .cr-btn-print:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        /* Generic Center Page Container for PIN, History, Settings */
        .cr-page-container {
            max-width: 1200px;
            width: 100%;
            margin: 20px auto;
            padding: 0 20px 40px;
            overflow-y: auto;
            flex: 1;
        }

        .cr-card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }

        .cr-table-wrapper {
            width: 100%;
            overflow-x: auto;
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }

        th {
            background: var(--surface-elevated);
            padding: 9px 14px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            color: var(--text-secondary);
            border-bottom: 1px solid var(--border);
        }

        td {
            padding: 10px 14px;
            border-bottom: 1px solid var(--border);
            color: var(--text-primary);
        }

        tr:last-child td { border-bottom: none; }
        tr:hover td { background: var(--row-hover); }

        /* Toast & Modal */
        .toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            box-shadow: 0 10px 25px rgba(0,0,0,0.3);
            border-radius: var(--radius-sm);
            padding: 10px 18px;
            font-size: 13px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 10px;
            z-index: 100;
            transform: translateY(150%);
            transition: transform 0.2s ease;
        }
        .toast.show { transform: translateY(0); }
        .toast.success { border-color: var(--success); }
        .toast.error { border-color: var(--danger); }

        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.65);
            backdrop-filter: blur(4px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 90;
        }
        .modal-overlay.show { display: flex; }
        .modal-card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 24px;
            max-width: 400px;
            width: 90%;
            box-shadow: 0 20px 40px rgba(0,0,0,0.5);
        }
    </style>
</head>
<body>

<header>
    <div class="header-brand">
        <div class="header-title">
            PRINTSERVER
            <span class="tag">APPLIANCE</span>
        </div>
        <div class="printer-select-wrapper">
            <span class="status-dot" id="headerStatusDot"></span>
            <select id="headerPrinterSelect" style="border:none; background:transparent; color:var(--text-primary); padding:0; font-size:12px; font-weight:500; outline:none; cursor:pointer;">
                <option value="">Detecting printers...</option>
            </select>
        </div>
    </div>

    <nav class="header-nav">
        <button class="nav-btn active" data-tab="print" onclick="switchTab('print')">Print</button>
        <button class="nav-btn" data-tab="idmaker" onclick="switchTab('idmaker')">ID Photo Maker</button>
        <button class="nav-btn" data-tab="pin" onclick="switchTab('pin')">PIN Release</button>
        <button class="nav-btn" data-tab="history" onclick="switchTab('history')">History</button>
        <button class="nav-btn" data-tab="settings" onclick="switchTab('settings')">Settings</button>
    </nav>

    <div class="header-controls">
        <button class="btn-lock" id="btnAdminLock">🔒 Locked</button>
        <button class="btn-theme" id="btnThemeToggle" title="Toggle theme">🌓</button>
    </div>
</header>

<main>
    <!-- TAB 1: EXACT CHROMIUM PRINT PREVIEW DIALOG -->
    <div class="tab-content active" id="tab-print">
        <div class="cr-layout">
            <!-- Left: Virtual Desk Canvas Preview -->
            <div class="cr-desk" id="printPreviewDesk">
                <div class="cr-desk-top-toolbar" id="previewTopToolbar" style="display: none;">
                    <div class="cr-doc-title" id="previewDocTitle">Document.pdf</div>
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <div class="cr-desk-pagination" id="previewPagination" style="display: none;">
                            <button type="button" class="cr-page-btn" id="btnPrev">‹</button>
                            <span id="pageIndicator">1 / 1</span>
                            <button type="button" class="cr-page-btn" id="btnNext">›</button>
                        </div>
                        <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnClearPreview" style="padding: 2px 10px; font-size: 11px;">✕ Clear</button>
                    </div>
                </div>

                <div class="cr-desk-viewport" id="printDeskViewport">
                    <div class="paper-sheet" id="paperSheet" style="width: 440px; height: 622px;">
                        <div class="printable-area" id="printableArea">
                            <div class="content-render-wrap" id="renderWrap">
                                <canvas id="pdfCanvas" style="display: none;"></canvas>
                                <img id="imgPreview" style="display: none;" alt="Preview">
                                <div id="txtPreview" style="display: none; padding: 20px; font-size: 12px; color: #333; overflow: hidden; width:100%; height:100%; word-break: break-all;"></div>
                            </div>
                        </div>
                    </div>

                    <div id="previewEmptyState" class="cr-empty-desk" onclick="document.getElementById('printFileInput').click()">
                        <div style="font-size: 40px; margin-bottom: 4px; opacity: 0.85;">📄</div>
                        <div style="font-weight: 500; font-size: 15px; color: var(--text-primary);">No document loaded</div>
                        <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 8px;">Drop PDF, Word, Excel, or Image to inspect</div>
                        <button type="button" class="cr-btn-pill cr-btn-cancel" style="font-size: 12px; pointer-events: none;">Choose File...</button>
                    </div>
                </div>

                <div class="cr-desk-footer-pill" id="previewDimensionsTag" style="display: none;">
                    <span id="sheetFormatTag">A4 • 210 × 297 mm</span>
                    <span>•</span>
                    <span id="sheetOrientationTag">Portrait</span>
                    <span>•</span>
                    <span id="sheetMarginTag">Printable Area</span>
                    <span>•</span>
                    <span id="sheetColorTag">Color</span>
                </div>
            </div>

            <!-- Right: Exact Chromium Settings Sidebar -->
            <div class="cr-sidebar">
                <!-- Sidebar Header -->
                <div class="cr-sidebar-header">
                    <span class="cr-sidebar-title">Print</span>
                    <span class="cr-sidebar-sheet-count">
                        <span id="costSheets">0 sheets of paper</span> • <span id="costTotal" style="color: var(--accent); font-weight: 500;">₱0.00</span>
                    </span>
                </div>

                <!-- Document Selection Row -->
                <input type="file" id="printFileInput" accept=".pdf,.doc,.docx,.odt,.rtf,.txt,.csv,.xlsx,.xls,.pptx,.ppt,.png,.jpg,.jpeg,.webp,.heic,.heif,.avif,.tiff,.bmp" style="display: none;">
                <div class="cr-row" style="border-bottom: 1px solid var(--border-subtle);">
                    <span class="cr-row-label">Document</span>
                    <div class="cr-row-control">
                        <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnSelectDoc" style="padding: 4px 10px; font-size: 11px; width: 100%; text-overflow: ellipsis; overflow: hidden; white-space: nowrap;" onclick="document.getElementById('printFileInput').click()">
                            <span id="printFileName">Choose file...</span>
                        </button>
                    </div>
                </div>

                <!-- 1. Destination Row -->
                <div class="cr-row">
                    <span class="cr-row-label">Destination</span>
                    <div class="cr-row-control">
                        <select id="printDestination" class="cr-select">
                            <option value="">Detecting...</option>
                        </select>
                    </div>
                </div>

                <!-- 2. Pages Row -->
                <div class="cr-row">
                    <span class="cr-row-label">Pages</span>
                    <div class="cr-row-control">
                        <select id="printPagesSelect" class="cr-select">
                            <option value="all" selected>All</option>
                            <option value="custom">Custom</option>
                        </select>
                    </div>
                </div>

                <!-- Pages Custom Range (Hidden unless Custom is selected) -->
                <div class="cr-row" id="customPageRangeRow" style="display: none; padding-top: 0;">
                    <span class="cr-row-label" style="font-size: 11px; color: var(--text-tertiary);">e.g. 1-3, 5</span>
                    <div class="cr-row-control">
                        <input type="text" id="printPageRange" class="cr-input" placeholder="e.g. 1-3, 5">
                    </div>
                </div>

                <!-- 3. Copies Row -->
                <div class="cr-row">
                    <span class="cr-row-label">Copies</span>
                    <div class="cr-row-control">
                        <input type="number" id="printCopies" class="cr-input cr-input-number" min="1" max="99" value="1">
                    </div>
                </div>

                <!-- 4. Color Row -->
                <div class="cr-row">
                    <span class="cr-row-label">Color</span>
                    <div class="cr-row-control">
                        <select id="printColorModel" class="cr-select">
                            <option value="RGB" selected>Color</option>
                            <option value="KGray">Black and white</option>
                        </select>
                    </div>
                </div>

                <!-- Collapsible More Settings Accordion -->
                <div class="cr-more-toggle" id="crMoreToggle">
                    <span>More settings</span>
                    <span id="crMoreArrow" style="font-size: 11px;">▾</span>
                </div>

                <div class="cr-more-content" id="crMoreContent">
                    <!-- Paper size -->
                    <div class="cr-row">
                        <span class="cr-row-label">Paper size</span>
                        <div class="cr-row-control">
                            <select id="printPageSize" class="cr-select">
                                <option value="A4" selected>A4</option>
                                <option value="Letter">Letter</option>
                                <option value="Legal">Legal</option>
                                <option value="A5">A5</option>
                                <option value="A6">A6</option>
                                <option value="B5">B5</option>
                                <option value="4x6in">4 × 6 in</option>
                                <option value="5x7in">5 × 7 in</option>
                                <option value="8x10">8 × 10 in</option>
                                <option value="EnvelopeDL">Envelope DL</option>
                            </select>
                        </div>
                    </div>

                    <!-- Orientation -->
                    <div class="cr-row">
                        <span class="cr-row-label">Orientation</span>
                        <div class="cr-row-control">
                            <select id="printOrientation" class="cr-select">
                                <option value="auto" selected>Auto-detect</option>
                                <option value="3">Portrait</option>
                                <option value="4">Landscape</option>
                            </select>
                        </div>
                    </div>

                    <!-- Scale -->
                    <div class="cr-row">
                        <span class="cr-row-label">Scale</span>
                        <div class="cr-row-control">
                            <select id="printScaling" class="cr-select">
                                <option value="true" selected>Fit to printable area</option>
                                <option value="false">Fit to paper (Fill/Crop)</option>
                            </select>
                        </div>
                    </div>

                    <!-- Margins -->
                    <div class="cr-row">
                        <span class="cr-row-label">Margins</span>
                        <div class="cr-row-control">
                            <select id="printMarginMode" class="cr-select">
                                <option value="printable" selected>Default</option>
                                <option value="none">None (0 mm)</option>
                                <option value="normal">Normal (12.7 mm)</option>
                                <option value="custom">Custom...</option>
                            </select>
                        </div>
                    </div>

                    <!-- Custom Margins Box -->
                    <div id="customMarginBox" class="cr-row" style="display: none; flex-direction: column; align-items: stretch; gap: 8px; background: rgba(0,0,0,0.15); padding: 8px 20px;">
                        <div style="font-size: 11px; color: var(--text-secondary);">Custom Margins (mm)</div>
                        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px;">
                            <div><span style="font-size: 10px; color: var(--text-tertiary);">Top</span><input type="number" id="mTop" class="cr-input" value="10" min="0" max="60"></div>
                            <div><span style="font-size: 10px; color: var(--text-tertiary);">Bottom</span><input type="number" id="mBottom" class="cr-input" value="10" min="0" max="60"></div>
                            <div><span style="font-size: 10px; color: var(--text-tertiary);">Left</span><input type="number" id="mLeft" class="cr-input" value="10" min="0" max="60"></div>
                            <div><span style="font-size: 10px; color: var(--text-tertiary);">Right</span><input type="number" id="mRight" class="cr-input" value="10" min="0" max="60"></div>
                        </div>
                    </div>

                    <!-- Resolution DPI -->
                    <div class="cr-row">
                        <span class="cr-row-label">Quality</span>
                        <div class="cr-row-control">
                            <select id="printDpi" class="cr-select">
                                <option value="150">150 dpi (Draft)</option>
                                <option value="300" selected>300 dpi</option>
                                <option value="600">600 dpi</option>
                                <option value="1200">1200 dpi</option>
                            </select>
                        </div>
                    </div>

                    <!-- Print Quality Mode -->
                    <div class="cr-row">
                        <span class="cr-row-label">Print Quality</span>
                        <div class="cr-row-control">
                            <select id="printQuality" class="cr-select">
                                <option value="Normal" selected>Normal</option>
                                <option value="Best">Best Quality</option>
                                <option value="Photo">High-Res Photo</option>
                            </select>
                        </div>
                    </div>

                    <!-- Media Type -->
                    <div class="cr-row">
                        <span class="cr-row-label">Media type</span>
                        <div class="cr-row-control">
                            <select id="printMediaType" class="cr-select">
                                <option value="Plain" selected>Plain Paper</option>
                                <option value="Glossy">Glossy Photo</option>
                                <option value="FastGlossy">Everyday Photo</option>
                                <option value="CoatedMatte">Matte Paper</option>
                                <option value="CoatedBrochure">Brochure</option>
                            </select>
                        </div>
                    </div>

                    <!-- Two-sided (Duplex) -->
                    <div class="cr-row">
                        <span class="cr-row-label">Two-sided</span>
                        <div class="cr-row-control" style="justify-content: flex-start;">
                            <label class="cr-checkbox-row">
                                <input type="checkbox" id="chkDuplex">
                                <span style="font-size: 13px;">Print on both sides</span>
                            </label>
                        </div>
                    </div>
                    <!-- Duplex options if checked -->
                    <div class="cr-row" id="duplexOptionsRow" style="display: none; padding-top: 0;">
                        <span class="cr-row-label" style="font-size: 11px; color: var(--text-tertiary);">Flip style</span>
                        <div class="cr-row-control">
                            <select id="printDuplex" class="cr-select">
                                <option value="DuplexNoTumble" selected>Flip on long edge</option>
                                <option value="DuplexTumble">Flip on short edge</option>
                            </select>
                        </div>
                    </div>

                    <!-- Confidential PIN Queue -->
                    <div class="cr-row">
                        <span class="cr-row-label">Confidential</span>
                        <div class="cr-row-control" style="justify-content: flex-start;">
                            <label class="cr-checkbox-row">
                                <input type="checkbox" id="chkPinHold">
                                <span style="font-size: 13px;">Hold in PIN Queue</span>
                            </label>
                        </div>
                    </div>
                    <div class="cr-row" id="pinInputWrapper" style="display: none; padding-top: 0;">
                        <span class="cr-row-label" style="font-size: 11px; color: var(--text-tertiary);">4-Digit PIN</span>
                        <div class="cr-row-control">
                            <input type="password" id="printPin" class="cr-input" maxlength="4" placeholder="••••" style="text-align: center; font-size: 16px; letter-spacing: 0.25em; font-family: var(--font-mono); width: 80px;">
                        </div>
                    </div>
                </div>

                <!-- System / Spool Hint -->
                <div class="cr-system-hint">
                    <span id="costDetails">0 pages @ ₱2.00/pg</span>
                    <span style="font-size: 11px; color: var(--text-tertiary);">CUPS IPP 2.0</span>
                </div>

                <!-- Bottom Actions Bar -->
                <div class="cr-bottom-bar">
                    <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnCancelPrint">Cancel</button>
                    <button type="button" class="cr-btn-pill cr-btn-print" id="btnSubmitPrint">Print</button>
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 2: ID PHOTO MAKER (MATCHING CHROMIUM WORKFLOW) -->
    <div class="tab-content" id="tab-idmaker">
        <div class="cr-layout">
            <!-- Left: ID Virtual Desk Live Sheet Preview -->
            <div class="cr-desk" id="idPreviewDesk">
                <div class="cr-desk-top-toolbar" style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                    <div class="cr-doc-title" id="idSheetTitleTag">4×6" Glossy Sheet • Combo Rush (2 pcs 2×2" + 16 pcs 1×1")</div>
                    <div style="display: flex; gap: 8px;">
                        <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnIdDownload" style="padding: 3px 10px; font-size: 11px;">💾 Save PNG</button>
                        <button type="button" class="cr-btn-pill cr-btn-print" id="btnIdDirectPrint" style="padding: 3px 14px; font-size: 11px;">🖨️ Print ID Sheet</button>
                    </div>
                </div>

                <div class="cr-desk-viewport">
                    <div class="paper-sheet" id="idPaperSheet" style="display: flex; align-items: center; justify-content: center; background: #ffffff; border-radius: 2px; overflow: hidden; position: relative;">
                        <canvas id="idSheetCanvas" style="width: 100%; height: 100%; object-fit: contain; display: none;"></canvas>
                    </div>
                    <div id="idEmptyState" class="cr-empty-desk" onclick="document.getElementById('idFileInput').click()">
                        <div style="font-size: 40px; margin-bottom: 4px; opacity: 0.85;">📸</div>
                        <div style="font-weight: 500; font-size: 15px; color: var(--text-primary);">Print Shop ID Studio Ready</div>
                        <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 8px;">Upload portrait photo on right to auto-generate packages</div>
                        <button type="button" class="cr-btn-pill cr-btn-cancel" style="font-size: 12px; pointer-events: none;">Choose Portrait...</button>
                    </div>
                </div>

                <div class="cr-desk-footer-pill">
                    <span id="idSheetSpecTag">4 × 6 in Photo Paper (101.6 × 152.4 mm)</span>
                    <span>•</span>
                    <span>300 DPI High Resolution</span>
                    <span>•</span>
                    <span id="idCutGuideTag">With Cutting Guides</span>
                </div>
            </div>

            <!-- Right: ID Controls Sidebar -->
            <div class="cr-sidebar">
                <div class="cr-sidebar-header">
                    <span class="cr-sidebar-title">ID Studio</span>
                    <span class="cr-sidebar-sheet-count">High-Res 300 DPI</span>
                </div>

                <!-- Portrait Photo Ingestion -->
                <input type="file" id="idFileInput" accept="image/*" style="display: none;">
                <div class="cr-row" style="border-bottom: 1px solid var(--border-subtle);">
                    <span class="cr-row-label">Portrait Photo</span>
                    <div class="cr-row-control">
                        <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnSelectIdPhoto" style="padding: 4px 10px; font-size: 11px; width: 100%; text-overflow: ellipsis; overflow: hidden; white-space: nowrap;" onclick="document.getElementById('idFileInput').click()">
                            <span id="idFileName">Choose portrait...</span>
                        </button>
                    </div>
                </div>

                <!-- Crop & Adjustments (Visible when photo is loaded) -->
                <div id="idEditControls" style="display: none;">
                    <div class="cr-row">
                        <span class="cr-row-label">Aspect ratio</span>
                        <div class="cr-row-control" style="gap: 4px;">
                            <button type="button" class="cr-btn-pill cr-btn-cancel id-crop-btn active" data-aspect="1:1" style="padding: 3px 8px; font-size: 11px;">1:1</button>
                            <button type="button" class="cr-btn-pill cr-btn-cancel id-crop-btn" data-aspect="35:45" style="padding: 3px 8px; font-size: 11px;">35:45</button>
                            <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnIdRotate" style="padding: 3px 8px; font-size: 11px;" title="Rotate 90°">↻</button>
                        </div>
                    </div>

                    <div class="cr-row">
                        <span class="cr-row-label">Face Zoom (<span id="lblIdZoom">100%</span>)</span>
                        <div class="cr-row-control">
                            <input type="range" id="idZoomRange" min="50" max="300" value="100" style="width: 100%; accent-color: var(--accent);">
                        </div>
                    </div>

                    <div class="cr-row">
                        <span class="cr-row-label">Vertical Align</span>
                        <div class="cr-row-control">
                            <input type="range" id="idOffsetYRange" min="-100" max="100" value="0" style="width: 100%; accent-color: var(--accent);">
                        </div>
                    </div>

                    <div class="cr-row">
                        <span class="cr-row-label">Brightness (<span id="lblIdBright">0</span>)</span>
                        <div class="cr-row-control">
                            <input type="range" id="idBrightRange" min="-50" max="50" value="0" style="width: 100%; accent-color: var(--accent);">
                        </div>
                    </div>

                    <div class="cr-row">
                        <span class="cr-row-label">Contrast (<span id="lblIdContrast">0</span>)</span>
                        <div class="cr-row-control">
                            <input type="range" id="idContrastRange" min="-50" max="50" value="0" style="width: 100%; accent-color: var(--accent);">
                        </div>
                    </div>

                    <div class="cr-row">
                        <span class="cr-row-label">Background</span>
                        <div class="cr-row-control" style="gap: 3px;">
                            <button type="button" class="cr-btn-pill cr-btn-cancel id-bg-btn active" data-bg="original" style="padding: 2px 6px; font-size: 10px;">Orig</button>
                            <button type="button" class="cr-btn-pill cr-btn-cancel id-bg-btn" data-bg="white" style="padding: 2px 6px; font-size: 10px;">White</button>
                            <button type="button" class="cr-btn-pill cr-btn-cancel id-bg-btn" data-bg="blue" style="padding: 2px 6px; font-size: 10px; color: #8ab4f8;">Blue</button>
                            <button type="button" class="cr-btn-pill cr-btn-cancel id-bg-btn" data-bg="red" style="padding: 2px 6px; font-size: 10px; color: #f28b82;">Red</button>
                        </div>
                    </div>

                    <div class="cr-row">
                        <span class="cr-row-label">Formal Name Tag</span>
                        <div class="cr-row-control" style="justify-content: flex-start;">
                            <label class="cr-checkbox-row">
                                <input type="checkbox" id="chkIdNameTag">
                                <span style="font-size: 12px;">Add Banner</span>
                            </label>
                        </div>
                    </div>
                    <div class="cr-row" id="idNameTagBox" style="display: none; padding-top: 0;">
                        <span class="cr-row-label" style="font-size: 11px; color: var(--text-tertiary);">Name (Uppercase)</span>
                        <div class="cr-row-control">
                            <input type="text" id="idNameTagText" class="cr-input" placeholder="DELA CRUZ, JUAN P." style="text-transform: uppercase; font-size: 11px;">
                        </div>
                    </div>
                </div>

                <!-- Package & Sheet Options -->
                <div class="cr-row" style="border-top: 1px solid var(--border-subtle); margin-top: 4px;">
                    <span class="cr-row-label">Package</span>
                    <div class="cr-row-control">
                        <select id="idPackageSelect" class="cr-select">
                            <option value="rush_combo_a" selected>Combo Rush: 2 pcs 2×2" + 16 pcs 1×1"</option>
                            <option value="rush_combo_b">Combo All: 2 pcs 2×2" + 6 Passport + 4 1×1"</option>
                            <option value="all_2x2">All 2 × 2 in (6 pcs)</option>
                            <option value="all_1x1">All 1 × 1 in (24 pcs)</option>
                            <option value="all_passport">All Passport (12 pcs)</option>
                            <option value="wallet">Wallet 2.5 × 3.5 in (4 pcs)</option>
                        </select>
                    </div>
                </div>

                <div class="cr-row">
                    <span class="cr-row-label">Paper size</span>
                    <div class="cr-row-control">
                        <select id="idSheetPaperSize" class="cr-select">
                            <option value="4x6in" selected>4 × 6 in Photo Card</option>
                            <option value="A4">A4 Photo Sheet</option>
                        </select>
                    </div>
                </div>

                <div class="cr-row">
                    <span class="cr-row-label">Copies</span>
                    <div class="cr-row-control">
                        <input type="number" id="idCopies" class="cr-input cr-input-number" min="1" max="50" value="1">
                    </div>
                </div>

                <div class="cr-row">
                    <span class="cr-row-label">Cutting Guides</span>
                    <div class="cr-row-control" style="justify-content: flex-start;">
                        <label class="cr-checkbox-row">
                            <input type="checkbox" id="chkIdCutLines" checked>
                            <span style="font-size: 12px;">Dashed lines</span>
                        </label>
                    </div>
                </div>

                <!-- Bottom Actions Bar -->
                <div class="cr-bottom-bar">
                    <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnIdDownloadBottom">Save PNG</button>
                    <button type="button" class="cr-btn-pill cr-btn-print" id="btnIdGeneratePrint">Print Sheet</button>
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 3: CONFIDENTIAL PIN RELEASE -->
    <div class="tab-content" id="tab-pin">
        <div class="cr-page-container">
            <div class="cr-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                    <div>
                        <h2 style="font-size: 16px; font-weight: 600; color: var(--text-primary);">Confidential Print Queue</h2>
                        <p style="font-size: 12px; color: var(--text-secondary);">Jobs held in memory until released with your 4-digit PIN</p>
                    </div>
                    <button class="cr-btn-pill cr-btn-cancel" id="btnRefreshPinList" style="font-size: 12px;">↻ Refresh</button>
                </div>

                <div class="cr-table-wrapper">
                    <table>
                        <thead>
                            <tr>
                                <th>Time Held</th>
                                <th>Document Title</th>
                                <th>Pages</th>
                                <th>Copies</th>
                                <th>Color</th>
                                <th>Est. Cost</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody id="pinTableBody">
                            <tr><td colspan="7" style="text-align: center; color: var(--text-tertiary);">No confidential jobs held</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 4: HISTORY & ACCOUNTING -->
    <div class="tab-content" id="tab-history">
        <div class="cr-page-container">
            <div class="cr-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                    <div>
                        <h2 style="font-size: 16px; font-weight: 600; color: var(--text-primary);">Print Accounting & Audit Ledger</h2>
                        <p style="font-size: 12px; color: var(--text-secondary);">Complete log of all print jobs processed</p>
                    </div>
                    <a href="/api/reports/export.csv" class="cr-btn-pill cr-btn-cancel" style="font-size: 12px; text-decoration: none;" download>📥 Export CSV</a>
                </div>

                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 20px;">
                    <div style="background: var(--surface-elevated); padding: 14px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
                        <div style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase;">Total Jobs</div>
                        <div style="font-size: 22px; font-weight: 600; margin-top: 4px;" id="statTotalJobs">0</div>
                    </div>
                    <div style="background: var(--surface-elevated); padding: 14px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
                        <div style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase;">Total Pages</div>
                        <div style="font-size: 22px; font-weight: 600; margin-top: 4px;" id="statTotalPages">0</div>
                    </div>
                    <div style="background: var(--surface-elevated); padding: 14px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
                        <div style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase;">Total Billed Cost</div>
                        <div style="font-size: 22px; font-weight: 600; color: var(--accent); margin-top: 4px;" id="statTotalRevenue">₱0.00</div>
                    </div>
                </div>

                <div class="cr-table-wrapper">
                    <table>
                        <thead>
                            <tr>
                                <th>Timestamp</th>
                                <th>Document</th>
                                <th>Printer</th>
                                <th>Pages</th>
                                <th>Copies</th>
                                <th>Color</th>
                                <th>Cost</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="historyTableBody">
                            <tr><td colspan="8" style="text-align: center; color: var(--text-tertiary);">No audit history recorded</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 5: APPLIANCE SETTINGS -->
    <div class="tab-content" id="tab-settings">
        <div class="cr-page-container">
            <div class="cr-card">
                <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 12px; color: var(--text-primary);">Hardware Health & Telemetry</h3>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 12px;">
                    <div style="background: var(--surface-elevated); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
                        <div style="font-size: 11px; color: var(--text-secondary);">CPU Load (1m)</div>
                        <div id="telemetryCpu" style="font-size: 18px; font-weight: 600; margin-top: 4px;">0.00</div>
                    </div>
                    <div style="background: var(--surface-elevated); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
                        <div style="font-size: 11px; color: var(--text-secondary);">RAM Usage</div>
                        <div id="telemetryRam" style="font-size: 18px; font-weight: 600; margin-top: 4px;">0 / 0 MB</div>
                    </div>
                    <div style="background: var(--surface-elevated); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
                        <div style="font-size: 11px; color: var(--text-secondary);">CPU Temperature</div>
                        <div id="telemetryTemp" style="font-size: 18px; font-weight: 600; margin-top: 4px;">0°C</div>
                    </div>
                    <div style="background: var(--surface-elevated); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
                        <div style="font-size: 11px; color: var(--text-secondary);">Storage Free</div>
                        <div id="telemetryDisk" style="font-size: 18px; font-weight: 600; margin-top: 4px;">0% Free</div>
                    </div>
                </div>
                <div style="font-size: 12px; color: var(--text-secondary);" id="telemetryUptime">Uptime: --</div>
            </div>

            <div class="cr-card">
                <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 12px; color: var(--text-primary);">Admin Management</h3>
                <div id="adminLockedState">
                    <p style="font-size: 12px; color: var(--text-secondary); margin-bottom: 12px;">
                        Appliance controls are password-protected.
                    </p>
                    <div style="display: flex; gap: 8px; max-width: 320px;">
                        <input type="password" id="adminPassInput" class="cr-input" placeholder="Password (default: admin)">
                        <button class="cr-btn-pill cr-btn-print" id="btnUnlockAdmin">Unlock</button>
                    </div>
                </div>

                <div id="adminUnlockedState" style="display: none;">
                    <div style="font-size: 12px; color: var(--success); font-weight: 600; margin-bottom: 12px;">
                        ✓ Administrator Mode Active
                    </div>

                    <div style="margin-bottom: 16px;">
                        <label style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase; font-weight: 600; display: block; margin-bottom: 6px;">Rate Card Configuration</label>
                        <div style="display: flex; gap: 8px; max-width: 360px; margin-bottom: 8px;">
                            <input type="text" id="adminRateBw" class="cr-input" placeholder="B&W Page (₱)">
                            <input type="text" id="adminRateColor" class="cr-input" placeholder="Color Page (₱)">
                        </div>
                        <button class="cr-btn-pill cr-btn-cancel" id="btnSaveRates" style="font-size: 12px;">Save Rate Card</button>
                    </div>

                    <div style="padding-top: 14px; border-top: 1px solid var(--border);">
                        <label style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase; font-weight: 600; display: block; margin-bottom: 8px;">Hardware Power & Spool Actions</label>
                        <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                            <button class="cr-btn-pill cr-btn-cancel" id="btnAdminRestartCups">🔄 Restart CUPS Spooler</button>
                            <button class="cr-btn-pill cr-btn-cancel" id="btnAdminPurgeCache">🧹 Purge Temporary Cache</button>
                            <button class="cr-btn-pill cr-btn-cancel" id="btnAdminReboot" style="color: var(--warning);">⚡ Reboot Appliance</button>
                            <button class="cr-btn-pill cr-btn-cancel" id="btnAdminShutdown" style="color: var(--danger);">🛑 Shut Down</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</main>

<!-- PIN UNLOCK MODAL -->
<div class="modal-overlay" id="pinModal">
    <div class="modal-card">
        <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 8px;">Release Confidential Job</h3>
        <p style="font-size: 12px; color: var(--text-secondary); margin-bottom: 16px;" id="pinModalJobTitle">Document</p>
        <div style="margin-bottom: 14px;">
            <label style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">Enter 4-Digit Release PIN</label>
            <input type="password" id="pinModalCode" class="cr-input" maxlength="4" placeholder="••••" style="text-align: center; font-size: 22px; letter-spacing: 0.35em; font-family: var(--font-mono);">
        </div>
        <div style="display: flex; gap: 10px; justify-content: flex-end;">
            <button class="cr-btn-pill cr-btn-cancel" id="btnCancelPinModal">Cancel</button>
            <button class="cr-btn-pill cr-btn-print" id="btnConfirmReleasePin">Release Print</button>
        </div>
    </div>
</div>

<div class="toast" id="toastNotification">
    <span id="toastMessage"></span>
</div>

<script>
    if (window.pdfjsLib) {
        pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/pdf.worker.min.js';
    }

    const PAPER_SPECS = {
        'A4': { width: 210, height: 297, name: 'A4', desc: '210 × 297 mm' },
        'Letter': { width: 215.9, height: 279.4, name: 'Letter', desc: '8.5 × 11 in' },
        'Legal': { width: 215.9, height: 355.6, name: 'Legal', desc: '8.5 × 14 in' },
        'A5': { width: 148, height: 210, name: 'A5', desc: '148 × 210 mm' },
        'A6': { width: 105, height: 148, name: 'A6', desc: '105 × 148 mm' },
        'B5': { width: 176, height: 250, name: 'B5', desc: '176 × 250 mm' },
        '4x6in': { width: 101.6, height: 152.4, name: '4 × 6 in', desc: 'Photo Card' },
        '5x7in': { width: 127, height: 177.8, name: '5 × 7 in', desc: 'Photo Card' },
        '8x10': { width: 203.2, height: 254, name: '8 × 10 in', desc: 'Photo Print' },
        'EnvelopeDL': { width: 110, height: 220, name: 'Envelope DL', desc: '110 × 220 mm' }
    };

    let currentAdminToken = sessionStorage.getItem('admin_token') || '';
    let selectedPrinter = '';
    let pendingReleaseJobId = '';

    let currentPdf = null;
    let pdfPage = 1;
    let pdfPagesTotal = 1;
    let docNaturalAspect = 1.414;
    let inspectedPageCount = 1;

    // Toast utility
    function showToast(msg, type='info') {
        const toast = document.getElementById('toastNotification');
        toast.className = 'toast show ' + type;
        document.getElementById('toastMessage').textContent = msg;
        setTimeout(() => toast.classList.remove('show'), 3500);
    }

    // Theme toggle
    document.getElementById('btnThemeToggle').addEventListener('click', () => {
        const cur = document.documentElement.getAttribute('data-theme');
        const next = (cur === 'gray' || cur === 'dark') ? 'light' : 'gray';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
    });

    // Tab Navigation Event Listeners
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.getAttribute('data-tab');
            if (tab && window.switchTab) {
                window.switchTab(tab);
            }
        });
    });

    // More settings accordion toggle
    const crMoreToggle = document.getElementById('crMoreToggle');
    const crMoreContent = document.getElementById('crMoreContent');
    const crMoreArrow = document.getElementById('crMoreArrow');
    if (crMoreToggle) {
        crMoreToggle.addEventListener('click', () => {
            const isOpen = crMoreContent.classList.toggle('open');
            crMoreArrow.textContent = isOpen ? '▴' : '▾';
        });
    }

    // Custom Pages dropdown toggle
    document.getElementById('printPagesSelect').addEventListener('change', (e) => {
        const isCustom = e.target.value === 'custom';
        document.getElementById('customPageRangeRow').style.display = isCustom ? 'flex' : 'none';
        if (!isCustom) {
            document.getElementById('printPageRange').value = '';
        }
    });

    // Duplex checkbox toggle
    document.getElementById('chkDuplex').addEventListener('change', (e) => {
        document.getElementById('duplexOptionsRow').style.display = e.target.checked ? 'flex' : 'none';
        document.getElementById('printDuplex').value = e.target.checked ? 'DuplexNoTumble' : 'None';
    });

    // Printer Discovery & Population
    async function loadPrinters() {
        try {
            const res = await fetch('/api/printers');
            const data = await res.json();
            const selects = [document.getElementById('headerPrinterSelect'), document.getElementById('printDestination')];
            
            selects.forEach(select => {
                if (!select) return;
                select.innerHTML = '';
                if (data.printers && data.printers.length > 0) {
                    data.printers.forEach(p => {
                        const opt = document.createElement('option');
                        opt.value = p.name;
                        opt.textContent = p.name + (p.is_default ? ' (Default)' : '');
                        if (p.is_default) opt.selected = true;
                        select.appendChild(opt);
                    });
                } else {
                    select.innerHTML = '<option value="">No printers detected</option>';
                }
            });

            if (data.printers && data.printers.length > 0) {
                selectedPrinter = selects[0].value;
                document.getElementById('headerStatusDot').className = 'status-dot ' + (data.printers[0].status || 'idle');
            }
        } catch (e) {
            console.error(e);
        }
    }

    document.getElementById('headerPrinterSelect').addEventListener('change', (e) => {
        selectedPrinter = e.target.value;
        const dest = document.getElementById('printDestination');
        if (dest) dest.value = e.target.value;
    });
    document.getElementById('printDestination').addEventListener('change', (e) => {
        selectedPrinter = e.target.value;
        const hSel = document.getElementById('headerPrinterSelect');
        if (hSel) hSel.value = e.target.value;
    });

    // ==========================================
    // ADVANCED REAL-TIME PREVIEW & MARGIN ENGINE
    // ==========================================
    function updatePreviewLayout() {
        const sizeKey = document.getElementById('printPageSize').value;
        const spec = PAPER_SPECS[sizeKey] || PAPER_SPECS['A4'];

        const orientation = document.getElementById('printOrientation').value;
        let isLandscape = false;
        if (orientation === '4') {
            isLandscape = true;
        } else if (orientation === 'auto') {
            isLandscape = docNaturalAspect < 0.95;
        }

        const deskViewport = document.getElementById('printDeskViewport');
        const availW = deskViewport ? Math.max(deskViewport.clientWidth - 40, 480) : 600;
        const availH = deskViewport ? Math.max(deskViewport.clientHeight - 40, 520) : 750;

        let paperPhysW = isLandscape ? spec.height : spec.width;
        let paperPhysH = isLandscape ? spec.width : spec.height;
        let paperRatio = paperPhysH / paperPhysW;

        let sheetW, sheetH;
        if (availW * paperRatio <= availH) {
            sheetW = availW;
            sheetH = Math.round(availW * paperRatio);
        } else {
            sheetH = availH;
            sheetW = Math.round(availH / paperRatio);
        }

        const paperSheet = document.getElementById('paperSheet');
        paperSheet.style.width = sheetW + 'px';
        paperSheet.style.height = sheetH + 'px';

        let mTopMm = 4.2, mBottomMm = 4.2, mLeftMm = 4.2, mRightMm = 4.2;
        let marginLabel = 'Printable Area';

        const mMode = document.getElementById('printMarginMode').value;
        if (mMode === 'none') {
            mTopMm = 0; mBottomMm = 0; mLeftMm = 0; mRightMm = 0;
            marginLabel = 'Borderless (0mm)';
        } else if (mMode === 'normal') {
            mTopMm = 12.7; mBottomMm = 12.7; mLeftMm = 12.7; mRightMm = 12.7;
            marginLabel = 'Normal (12.7mm)';
        } else if (mMode === 'custom') {
            mTopMm = parseFloat(document.getElementById('mTop').value) || 0;
            mBottomMm = parseFloat(document.getElementById('mBottom').value) || 0;
            mLeftMm = parseFloat(document.getElementById('mLeft').value) || 0;
            mRightMm = parseFloat(document.getElementById('mRight').value) || 0;
            marginLabel = `Custom (${mTopMm}T, ${mLeftMm}L mm)`;
        }

        const scaleX = sheetW / paperPhysW;
        const scaleY = sheetH / paperPhysH;

        const topPx = Math.round(mTopMm * scaleY);
        const bottomPx = Math.round(mBottomMm * scaleY);
        const leftPx = Math.round(mLeftMm * scaleX);
        const rightPx = Math.round(mRightMm * scaleX);

        const printableArea = document.getElementById('printableArea');
        printableArea.style.top = topPx + 'px';
        printableArea.style.bottom = bottomPx + 'px';
        printableArea.style.left = leftPx + 'px';
        printableArea.style.right = rightPx + 'px';

        // Grayscale live preview
        const isGray = document.getElementById('printColorModel').value === 'KGray';
        paperSheet.style.filter = isGray ? 'grayscale(100%)' : 'none';

        // Scaling class
        const fitPage = document.getElementById('printScaling').value === 'true';
        document.getElementById('renderWrap').classList.toggle('scale-fill', !fitPage);

        // Tags
        document.getElementById('sheetFormatTag').textContent = `${spec.name} • ${spec.desc}`;
        document.getElementById('sheetOrientationTag').textContent = isLandscape ? 'Landscape' : 'Portrait';
        document.getElementById('sheetMarginTag').textContent = marginLabel;
        document.getElementById('sheetColorTag').textContent = isGray ? 'B&W' : 'Color';

        updateEstimatedCost();
    }

    function updateEstimatedCost() {
        const copies = parseInt(document.getElementById('printCopies').value) || 1;
        const isColor = document.getElementById('printColorModel').value === 'RGB';
        const rate = isColor ? 5.00 : 2.00;
        const total = rate * inspectedPageCount * copies;
        const totalSheets = inspectedPageCount * copies;

        document.getElementById('costDetails').textContent = `${inspectedPageCount} page(s) × ${copies} copy(ies) @ ₱${rate.toFixed(2)}/pg`;
        document.getElementById('costTotal').textContent = `₱${total.toFixed(2)}`;
        document.getElementById('costSheets').textContent = `${totalSheets} sheet${totalSheets !== 1 ? 's' : ''} of paper`;
    }

    ['printPageSize', 'printOrientation', 'printMarginMode', 'printColorModel', 'printScaling', 'printCopies'].forEach(id => {
        document.getElementById(id).addEventListener('change', updatePreviewLayout);
    });
    ['mTop', 'mBottom', 'mLeft', 'mRight'].forEach(id => {
        document.getElementById(id).addEventListener('input', updatePreviewLayout);
    });

    document.getElementById('printMarginMode').addEventListener('change', (e) => {
        document.getElementById('customMarginBox').style.display = (e.target.value === 'custom') ? 'flex' : 'none';
        updatePreviewLayout();
    });

    // Drag & Drop on Virtual Desk
    const printDesk = document.getElementById('printPreviewDesk');
    if (printDesk) {
        printDesk.addEventListener('dragover', (e) => {
            e.preventDefault();
            printDesk.style.outline = '2px dashed var(--accent)';
        });
        printDesk.addEventListener('dragleave', () => {
            printDesk.style.outline = 'none';
        });
        printDesk.addEventListener('drop', (e) => {
            e.preventDefault();
            printDesk.style.outline = 'none';
            if (e.dataTransfer.files && e.dataTransfer.files.length) {
                document.getElementById('printFileInput').files = e.dataTransfer.files;
                document.getElementById('printFileInput').dispatchEvent(new Event('change'));
            }
        });
    }

    // File Ingestion & Rendering
    const fileInput = document.getElementById('printFileInput');
    fileInput.addEventListener('change', async () => {
        if (!fileInput.files.length) return;
        const file = fileInput.files[0];
        document.getElementById('printFileName').textContent = file.name;
        document.getElementById('previewDocTitle').textContent = file.name;
        document.getElementById('previewTopToolbar').style.display = 'flex';
        document.getElementById('previewEmptyState').style.display = 'none';
        document.getElementById('previewDimensionsTag').style.display = 'flex';

        const formData = new FormData();
        formData.append('file', file);
        formData.append('copies', document.getElementById('printCopies').value);
        formData.append('colormodel', document.getElementById('printColorModel').value);

        try {
            const res = await fetch('/api/inspect', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                inspectedPageCount = data.pages;
                updateEstimatedCost();
            }
        } catch (e) {
            console.error(e);
        }

        const ext = file.name.split('.').pop().toLowerCase();
        const pdfCanvas = document.getElementById('pdfCanvas');
        const imgPreview = document.getElementById('imgPreview');
        const txtPreview = document.getElementById('txtPreview');

        pdfCanvas.style.display = 'none';
        imgPreview.style.display = 'none';
        txtPreview.style.display = 'none';
        document.getElementById('previewPagination').style.display = 'none';

        if (['jpg', 'jpeg', 'png', 'webp', 'bmp'].includes(ext)) {
            const reader = new FileReader();
            reader.onload = function(e) {
                imgPreview.src = e.target.result;
                imgPreview.style.display = 'block';
                const tmpImg = new Image();
                tmpImg.src = e.target.result;
                tmpImg.onload = function() {
                    docNaturalAspect = tmpImg.naturalHeight / tmpImg.naturalWidth;
                    updatePreviewLayout();
                };
            };
            reader.readAsDataURL(file);
        } else if (ext === 'pdf') {
            const reader = new FileReader();
            reader.onload = function(e) {
                pdfjsLib.getDocument(new Uint8Array(e.target.result)).promise.then(pdf => {
                    currentPdf = pdf;
                    pdfPagesTotal = pdf.numPages;
                    pdfPage = 1;
                    if (pdfPagesTotal > 1) document.getElementById('previewPagination').style.display = 'flex';
                    renderPdfPage(pdfPage);
                }).catch(() => {
                    txtPreview.style.display = 'block';
                    txtPreview.textContent = 'PDF ready to print.';
                    updatePreviewLayout();
                });
            };
            reader.readAsArrayBuffer(file);
        } else {
            txtPreview.style.display = 'block';
            txtPreview.textContent = `Document (${ext.toUpperCase()}) queued and ready to print.`;
            updatePreviewLayout();
        }
    });

    function renderPdfPage(num) {
        if (!currentPdf) return;
        currentPdf.getPage(num).then(page => {
            const pdfCanvas = document.getElementById('pdfCanvas');
            const viewport = page.getViewport({ scale: 1.5 });
            docNaturalAspect = viewport.height / viewport.width;
            pdfCanvas.height = viewport.height;
            pdfCanvas.width = viewport.width;
            pdfCanvas.style.display = 'block';
            page.render({
                canvasContext: pdfCanvas.getContext('2d'),
                viewport: viewport
            });
            document.getElementById('pageIndicator').textContent = `${num} / ${pdfPagesTotal}`;
            updatePreviewLayout();
        });
    }

    document.getElementById('btnPrev').addEventListener('click', () => {
        if (pdfPage > 1) { pdfPage--; renderPdfPage(pdfPage); }
    });
    document.getElementById('btnNext').addEventListener('click', () => {
        if (pdfPage < pdfPagesTotal) { pdfPage++; renderPdfPage(pdfPage); }
    });

    function clearPreviewState() {
        fileInput.value = '';
        currentPdf = null;
        inspectedPageCount = 1;
        document.getElementById('printFileName').textContent = 'Choose file...';
        document.getElementById('previewTopToolbar').style.display = 'none';
        document.getElementById('previewEmptyState').style.display = 'flex';
        document.getElementById('previewDimensionsTag').style.display = 'none';
        document.getElementById('pdfCanvas').style.display = 'none';
        document.getElementById('imgPreview').style.display = 'none';
        document.getElementById('txtPreview').style.display = 'none';
        document.getElementById('costDetails').textContent = '0 pages @ ₱2.00/pg';
        document.getElementById('costTotal').textContent = '₱0.00';
        document.getElementById('costSheets').textContent = '0 sheets of paper';
    }

    document.getElementById('btnClearPreview').addEventListener('click', clearPreviewState);
    document.getElementById('btnCancelPrint').addEventListener('click', clearPreviewState);

    document.getElementById('chkPinHold').addEventListener('change', (e) => {
        document.getElementById('pinInputWrapper').style.display = e.target.checked ? 'flex' : 'none';
        document.getElementById('btnSubmitPrint').textContent = e.target.checked ? 'Hold in PIN' : 'Print';
    });

    document.getElementById('btnSubmitPrint').addEventListener('click', async () => {
        if (!fileInput.files.length) {
            showToast('Please select a file to print', 'error');
            return;
        }

        const isPin = document.getElementById('chkPinHold').checked;
        const pin = document.getElementById('printPin').value.trim();
        if (isPin && (!pin || pin.length !== 4)) {
            showToast('Please enter a 4-digit PIN', 'error');
            return;
        }

        const formData = new FormData();
        formData.append('file', fileInput.files[0]);
        formData.append('printer', selectedPrinter);
        formData.append('copies', document.getElementById('printCopies').value);
        formData.append('pagesize', document.getElementById('printPageSize').value);
        formData.append('orientation', document.getElementById('printOrientation').value);
        formData.append('margin_mode', document.getElementById('printMarginMode').value);
        formData.append('m_top', document.getElementById('mTop').value);
        formData.append('m_bottom', document.getElementById('mBottom').value);
        formData.append('m_left', document.getElementById('mLeft').value);
        formData.append('m_right', document.getElementById('mRight').value);
        formData.append('mediatype', document.getElementById('printMediaType').value);
        formData.append('quality', document.getElementById('printQuality').value);
        formData.append('resolution', document.getElementById('printDpi').value);
        formData.append('colormodel', document.getElementById('printColorModel').value);
        formData.append('duplex', document.getElementById('printDuplex').value);
        formData.append('fitpage', document.getElementById('printScaling').value);
        formData.append('pagerange', document.getElementById('printPageRange').value);

        const endpoint = isPin ? '/api/print/pin-hold' : '/api/print';
        if (isPin) formData.append('pin', pin);

        showToast('Submitting print job...', 'info');
        try {
            const res = await fetch(endpoint, { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                showToast(isPin ? 'Document held in confidential queue!' : 'Print job queued successfully!', 'success');
            } else {
                showToast('Error: ' + data.error, 'error');
            }
        } catch (e) {
            showToast('Failed to reach print server', 'error');
        }
    });


    // ==========================================
    // PRINT SHOP ID PHOTO MAKER ENGINE (100% NATIVE HTML5)
    // ==========================================
    let idRawImage = null;
    let idRotation = 0;
    let idAspectMode = '1:1';
    let idBgTint = 'original';

    function initIdStudio() {
        if (idRawImage) renderIdSheet();
    }

    function loadIdImageFile(file) {
        if (!file || !file.type.startsWith('image/')) {
            showToast('Please upload an image file (JPG, PNG)', 'error');
            return;
        }
        document.getElementById('idFileName').textContent = file.name;
        const reader = new FileReader();
        reader.onload = function(ev) {
            const img = new Image();
            img.onload = function() {
                idRawImage = img;
                idRotation = 0;
                document.getElementById('idEditControls').style.display = 'block';
                updateIdTitleTag();
                renderIdSheet();
                showToast('Portrait loaded! Ready for ID package layout.', 'success');
            };
            img.src = ev.target.result;
        };
        reader.readAsDataURL(file);
    }

    function updateIdTitleTag() {
        const pkg = document.getElementById('idPackageSelect');
        const paper = document.getElementById('idSheetPaperSize').value;
        const pkgName = pkg.options[pkg.selectedIndex].text;
        document.getElementById('idSheetTitleTag').textContent = `${paper === 'A4' ? 'A4 Photo Sheet' : '4×6" Glossy Sheet'} • ${pkgName}`;
        document.getElementById('idSheetSpecTag').textContent = paper === 'A4' ? 'A4 Photo Paper (210 × 297 mm)' : '4 × 6 in Photo Paper (101.6 × 152.4 mm)';
    }

    function getProcessedSinglePhoto() {
        if (!idRawImage) return null;

        const isPassport = idAspectMode === '35:45';
        const photoW = isPassport ? 413 : 600;
        const photoH = isPassport ? 531 : 600;

        const off = document.createElement('canvas');
        off.width = photoW;
        off.height = photoH;
        const octx = off.getContext('2d');

        // Background Color Tint
        if (idBgTint === 'white') {
            octx.fillStyle = '#ffffff';
            octx.fillRect(0, 0, photoW, photoH);
        } else if (idBgTint === 'blue') {
            octx.fillStyle = '#1d4ed8';
            octx.fillRect(0, 0, photoW, photoH);
        } else if (idBgTint === 'red') {
            octx.fillStyle = '#b91c1c';
            octx.fillRect(0, 0, photoW, photoH);
        } else {
            octx.fillStyle = '#ffffff';
            octx.fillRect(0, 0, photoW, photoH);
        }

        // Brightness & Contrast
        const bright = parseInt(document.getElementById('idBrightRange').value) || 0;
        const contrast = parseInt(document.getElementById('idContrastRange').value) || 0;
        octx.filter = `brightness(${100 + bright}%) contrast(${100 + contrast}%)`;

        // Crop, Zoom, and Orientation
        const zoom = (parseInt(document.getElementById('idZoomRange').value) || 100) / 100;
        const offsetYPercent = (parseInt(document.getElementById('idOffsetYRange').value) || 0) / 100;

        octx.save();
        octx.translate(photoW / 2, photoH / 2);
        octx.rotate((idRotation * Math.PI) / 180);

        const isSideways = idRotation === 90 || idRotation === 270;
        const natW = isSideways ? idRawImage.height : idRawImage.width;
        const natH = isSideways ? idRawImage.width : idRawImage.height;

        const scale = Math.max(photoW / natW, photoH / natH) * zoom;
        const drawW = idRawImage.width * scale;
        const drawH = idRawImage.height * scale;
        const dy = offsetYPercent * (photoH / 2);

        octx.drawImage(idRawImage, -drawW / 2, -drawH / 2 + dy, drawW, drawH);
        octx.restore();

        octx.filter = 'none';

        // Optional Name Tag Banner Overlay (Bottom of Photo)
        const hasNameTag = document.getElementById('chkIdNameTag').checked;
        const nameText = document.getElementById('idNameTagText').value.trim().toUpperCase();
        if (hasNameTag && nameText) {
            const bannerH = Math.round(photoH * 0.16);
            octx.fillStyle = '#ffffff';
            octx.fillRect(0, photoH - bannerH, photoW, bannerH);
            
            octx.strokeStyle = '#000000';
            octx.lineWidth = Math.max(2, Math.round(photoW * 0.006));
            octx.strokeRect(0, photoH - bannerH, photoW, bannerH);

            octx.fillStyle = '#000000';
            const fontSize = Math.round(bannerH * 0.52);
            octx.font = `bold ${fontSize}px Arial, sans-serif`;
            octx.textAlign = 'center';
            octx.textBaseline = 'middle';
            octx.fillText(nameText, photoW / 2, photoH - (bannerH / 2));
        }

        return off;
    }

    function renderIdSheet() {
        if (!idRawImage) return;

        const sheetCanvas = document.getElementById('idSheetCanvas');
        const ctx = sheetCanvas.getContext('2d');

        const paperSize = document.getElementById('idSheetPaperSize').value;
        const isA4 = paperSize === 'A4';

        // 300 DPI high-resolution canvas buffer
        const sheetW = isA4 ? 2480 : 1200;
        const sheetH = isA4 ? 3508 : 1800;

        sheetCanvas.width = sheetW;
        sheetCanvas.height = sheetH;

        // Dynamically scale display preview to fill the viewport
        const idDesk = document.getElementById('idPreviewDesk');
        const idViewport = idDesk ? (idDesk.querySelector('.cr-desk-viewport') || idDesk) : null;
        const availW = idViewport ? Math.max(idViewport.clientWidth - 40, 460) : 560;
        const availH = idViewport ? Math.max(idViewport.clientHeight - 40, 520) : 750;

        let displayRatio = sheetH / sheetW;
        let dispW, dispH;
        if (availW * displayRatio <= availH) {
            dispW = availW;
            dispH = Math.round(availW * displayRatio);
        } else {
            dispH = availH;
            dispW = Math.round(availH / displayRatio);
        }

        const paperSheetEl = document.getElementById('idPaperSheet');
        paperSheetEl.style.width = dispW + 'px';
        paperSheetEl.style.height = dispH + 'px';

        // Paper background
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, sheetW, sheetH);

        const singlePhoto = getProcessedSinglePhoto();
        if (!singlePhoto) return;

        const pkg = document.getElementById('idPackageSelect').value;
        const showCutLines = document.getElementById('chkIdCutLines').checked;

        function drawPhotoItem(x, y, w, h) {
            ctx.drawImage(singlePhoto, x, y, w, h);
            if (showCutLines) {
                ctx.save();
                ctx.strokeStyle = '#94a3b8';
                ctx.lineWidth = Math.max(2, Math.round(sheetW * 0.0015));
                ctx.setLineDash([10, 8]);
                ctx.strokeRect(x, y, w, h);
                ctx.restore();
            }
        }

        if (pkg === 'rush_combo_a') {
            // Full Sheet Fill: 2 pcs 2x2" + 16 pcs 1x1"
            const size2x2 = isA4 ? 900 : 520;
            const startX = isA4 ? 260 : 60;
            const gap = isA4 ? 80 : 40;
            const y1 = isA4 ? 120 : 50;
            drawPhotoItem(startX, y1, size2x2, size2x2);
            drawPhotoItem(startX + size2x2 + gap, y1, size2x2, size2x2);

            const size1x1 = isA4 ? 430 : 250;
            const gap1x1 = isA4 ? 40 : 26;
            const startX1 = isA4 ? 260 : 60;
            const startY = y1 + size2x2 + (isA4 ? 80 : 40);

            const numRows = isA4 ? 6 : 4;
            for (let r = 0; r < numRows; r++) {
                for (let c = 0; c < 4; c++) {
                    drawPhotoItem(startX1 + c * (size1x1 + gap1x1), startY + r * (size1x1 + gap1x1), size1x1, size1x1);
                }
            }
        } else if (pkg === 'rush_combo_b') {
            // Full Sheet Fill: 2 pcs 2x2" + 6 pcs Passport + 4 pcs 1x1"
            const size2x2 = isA4 ? 900 : 520;
            const startX = isA4 ? 260 : 60;
            const gap = isA4 ? 80 : 40;
            const y1 = isA4 ? 100 : 50;
            drawPhotoItem(startX, y1, size2x2, size2x2);
            drawPhotoItem(startX + size2x2 + gap, y1, size2x2, size2x2);

            const pw = isA4 ? 430 : 340;
            const ph = isA4 ? 550 : 430;
            const pgap = isA4 ? 40 : 30;
            const y2 = y1 + size2x2 + (isA4 ? 60 : 35);
            const pStartX = Math.round((sheetW - (3 * pw + 2 * pgap)) / 2);
            for (let r = 0; r < 2; r++) {
                for (let c = 0; c < 3; c++) {
                    drawPhotoItem(pStartX + c * (pw + pgap), y2 + r * (ph + pgap), pw, ph);
                }
            }

            const size1x1 = isA4 ? 430 : 250;
            const gap1x1 = isA4 ? 40 : 26;
            const y3 = y2 + 2 * (ph + pgap) + (isA4 ? 40 : 20);
            for (let i = 0; i < 4; i++) {
                drawPhotoItem(startX + i * (size1x1 + gap1x1), y3, size1x1, size1x1);
            }
        } else if (pkg === 'all_2x2') {
            const size = isA4 ? 540 : 520;
            const cols = isA4 ? 4 : 2;
            const rows = isA4 ? 5 : 3;
            const gap = isA4 ? 40 : 40;
            const startX = Math.round((sheetW - (cols * size + (cols - 1) * gap)) / 2);
            const startY = isA4 ? 120 : 60;

            for (let r = 0; r < rows; r++) {
                for (let c = 0; c < cols; c++) {
                    drawPhotoItem(startX + c * (size + gap), startY + r * (size + gap), size, size);
                }
            }
        } else if (pkg === 'all_1x1') {
            const size = isA4 ? 320 : 250;
            const cols = isA4 ? 6 : 4;
            const rows = isA4 ? 9 : 6;
            const gap = isA4 ? 28 : 28;
            const startX = Math.round((sheetW - (cols * size + (cols - 1) * gap)) / 2);
            const startY = isA4 ? 100 : 50;

            for (let r = 0; r < rows; r++) {
                for (let c = 0; c < cols; c++) {
                    drawPhotoItem(startX + c * (size + gap), startY + r * (size + gap), size, size);
                }
            }
        } else if (pkg === 'all_passport') {
            const pw = isA4 ? 420 : 340;
            const ph = isA4 ? 540 : 400;
            const cols = isA4 ? 5 : 3;
            const rows = isA4 ? 5 : 4;
            const gap = isA4 ? 30 : 25;
            const startX = Math.round((sheetW - (cols * pw + (cols - 1) * gap)) / 2);
            const startY = isA4 ? 100 : 50;

            for (let r = 0; r < rows; r++) {
                for (let c = 0; c < cols; c++) {
                    drawPhotoItem(startX + c * (pw + gap), startY + r * (ph + gap), pw, ph);
                }
            }
        } else if (pkg === 'wallet') {
            const ww = isA4 ? 750 : 520;
            const wh = isA4 ? 1050 : 780;
            const cols = 2;
            const rows = 2;
            const gap = isA4 ? 100 : 40;
            const startX = Math.round((sheetW - (cols * ww + (cols - 1) * gap)) / 2);
            const startY = isA4 ? 150 : 60;

            for (let r = 0; r < rows; r++) {
                for (let c = 0; c < cols; c++) {
                    drawPhotoItem(startX + c * (ww + gap), startY + r * (wh + gap), ww, wh);
                }
            }
        }

        document.getElementById('idEmptyState').style.display = 'none';
        sheetCanvas.style.display = 'block';
    }

    async function printIdSheetDirect() {
        if (!idRawImage) {
            showToast('Please upload a portrait photo first', 'error');
            return;
        }
        const sheetCanvas = document.getElementById('idSheetCanvas');
        const paperSize = document.getElementById('idSheetPaperSize').value;
        const copies = document.getElementById('idCopies').value || '1';

        const btn = document.getElementById('btnIdDirectPrint');
        const btn2 = document.getElementById('btnIdGeneratePrint');
        btn.disabled = true; btn.textContent = 'Preparing...';
        btn2.disabled = true;

        sheetCanvas.toBlob(async (blob) => {
            try {
                const formData = new FormData();
                formData.append('file', blob, `ID_Sheet_${paperSize}.png`);
                formData.append('printer', selectedPrinter);
                formData.append('pagesize', paperSize);
                formData.append('mediatype', 'Glossy');
                formData.append('quality', 'Best');
                formData.append('colormodel', 'RGB');
                formData.append('orientation', '3');
                formData.append('copies', copies);
                formData.append('margin_mode', 'none');

                const res = await fetch('/api/print', { method: 'POST', body: formData });
                const data = await res.json();
                if (data.success) {
                    showToast('ID Photo Sheet sent to printer successfully!', 'success');
                } else {
                    showToast('Print error: ' + data.error, 'error');
                }
            } catch (e) {
                showToast('Failed to reach print server', 'error');
            } finally {
                btn.disabled = false; btn.textContent = '🖨️ Print ID Sheet';
                btn2.disabled = false;
            }
        }, 'image/png');
    }

    function downloadIdSheet() {
        if (!idRawImage) {
            showToast('Please upload a portrait photo first', 'error');
            return;
        }
        const sheetCanvas = document.getElementById('idSheetCanvas');
        const paperSize = document.getElementById('idSheetPaperSize').value;
        const link = document.createElement('a');
        link.download = `ID_Photos_${paperSize}_300DPI.png`;
        link.href = sheetCanvas.toDataURL('image/png');
        link.click();
        showToast('High-resolution ID sheet downloaded!', 'success');
    }

    // Attach ID Studio Event Listeners
    const idFileInput = document.getElementById('idFileInput');
    idFileInput.addEventListener('change', (e) => {
        if (e.target.files.length) loadIdImageFile(e.target.files[0]);
    });

    const idDeskEl = document.getElementById('idPreviewDesk');
    if (idDeskEl) {
        idDeskEl.addEventListener('dragover', (e) => {
            e.preventDefault();
            idDeskEl.style.outline = '2px dashed var(--accent)';
        });
        idDeskEl.addEventListener('dragleave', () => {
            idDeskEl.style.outline = 'none';
        });
        idDeskEl.addEventListener('drop', (e) => {
            e.preventDefault();
            idDeskEl.style.outline = 'none';
            if (e.dataTransfer.files && e.dataTransfer.files.length) {
                loadIdImageFile(e.dataTransfer.files[0]);
            }
        });
    }

    document.querySelectorAll('.id-crop-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.id-crop-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            idAspectMode = btn.getAttribute('data-aspect');
            renderIdSheet();
        });
    });

    document.getElementById('btnIdRotate').addEventListener('click', () => {
        idRotation = (idRotation + 90) % 360;
        renderIdSheet();
    });

    document.getElementById('idZoomRange').addEventListener('input', (e) => {
        document.getElementById('lblIdZoom').textContent = e.target.value + '%';
        renderIdSheet();
    });
    document.getElementById('idOffsetYRange').addEventListener('input', () => renderIdSheet());
    document.getElementById('idBrightRange').addEventListener('input', (e) => {
        document.getElementById('lblIdBright').textContent = (e.target.value > 0 ? '+' : '') + e.target.value;
        renderIdSheet();
    });
    document.getElementById('idContrastRange').addEventListener('input', (e) => {
        document.getElementById('lblIdContrast').textContent = (e.target.value > 0 ? '+' : '') + e.target.value;
        renderIdSheet();
    });

    document.querySelectorAll('.id-bg-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.id-bg-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            idBgTint = btn.getAttribute('data-bg');
            renderIdSheet();
        });
    });

    document.getElementById('chkIdNameTag').addEventListener('change', (e) => {
        document.getElementById('idNameTagBox').style.display = e.target.checked ? 'flex' : 'none';
        renderIdSheet();
    });
    document.getElementById('idNameTagText').addEventListener('input', () => renderIdSheet());

    document.getElementById('idPackageSelect').addEventListener('change', () => {
        updateIdTitleTag();
        renderIdSheet();
    });
    document.getElementById('idSheetPaperSize').addEventListener('change', () => {
        updateIdTitleTag();
        renderIdSheet();
    });
    document.getElementById('chkIdCutLines').addEventListener('change', () => renderIdSheet());

    document.getElementById('btnIdDirectPrint').addEventListener('click', printIdSheetDirect);
    document.getElementById('btnIdGeneratePrint').addEventListener('click', printIdSheetDirect);
    document.getElementById('btnIdDownload').addEventListener('click', downloadIdSheet);
    document.getElementById('btnIdDownloadBottom').addEventListener('click', downloadIdSheet);

    window.addEventListener('resize', () => {
        updatePreviewLayout();
        if (typeof renderIdSheet === 'function') renderIdSheet();
    });


    // ==========================================
    // CONFIDENTIAL PIN QUEUE
    // ==========================================
    async function loadPinList() {
        try {
            const res = await fetch('/api/pin/list');
            const data = await res.json();
            const tbody = document.getElementById('pinTableBody');
            tbody.innerHTML = '';
            if (data.jobs && data.jobs.length > 0) {
                data.jobs.forEach(j => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${j.created_at}</td>
                        <td style="font-weight: 500;">🔒 ${j.filename}</td>
                        <td>${j.pages}</td>
                        <td>${j.copies}</td>
                        <td>${j.colormodel}</td>
                        <td style="color: var(--accent); font-weight: 500;">₱${j.cost.toFixed(2)}</td>
                        <td><button class="cr-btn-pill cr-btn-print" style="padding: 2px 10px; font-size: 11px;" onclick="openPinModal('${j.job_id}', '${j.filename}')">Release</button></td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-tertiary);">No confidential jobs held</td></tr>';
            }
        } catch (e) {
            console.error(e);
        }
    }

    window.openPinModal = function(jobId, filename) {
        pendingReleaseJobId = jobId;
        document.getElementById('pinModalJobTitle').textContent = filename;
        document.getElementById('pinModalCode').value = '';
        document.getElementById('pinModal').classList.add('show');
    };
    document.getElementById('btnCancelPinModal').addEventListener('click', () => {
        document.getElementById('pinModal').classList.remove('show');
    });
    document.getElementById('btnConfirmReleasePin').addEventListener('click', async () => {
        const pin = document.getElementById('pinModalCode').value.trim();
        if (!pin) return;

        const formData = new FormData();
        formData.append('job_id', pendingReleaseJobId);
        formData.append('pin', pin);

        try {
            const res = await fetch('/api/pin/release', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                showToast('Job unlocked and sent to printer!', 'success');
                document.getElementById('pinModal').classList.remove('show');
                loadPinList();
            } else {
                showToast('Error: ' + data.error, 'error');
            }
        } catch (e) {
            showToast('Release failed', 'error');
        }
    });

    // ==========================================
    // HISTORY & TELEMETRY
    // ==========================================
    async function loadHistory() {
        try {
            const res = await fetch('/api/reports/ledger');
            const data = await res.json();
            if (data.stats) {
                document.getElementById('statTotalJobs').textContent = data.stats.total_jobs;
                document.getElementById('statTotalPages').textContent = data.stats.total_pages;
                document.getElementById('statTotalRevenue').textContent = data.stats.total_revenue;
            }
            const tbody = document.getElementById('historyTableBody');
            tbody.innerHTML = '';
            if (data.jobs && data.jobs.length > 0) {
                data.jobs.forEach(j => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${j.timestamp}</td>
                        <td style="font-weight: 500;">${j.title}</td>
                        <td>${j.printer}</td>
                        <td>${j.pages}</td>
                        <td>${j.copies}</td>
                        <td>${j.colormodel}</td>
                        <td style="color: var(--accent); font-weight: 500;">${j.cost}</td>
                        <td><span style="font-size: 11px; padding: 2px 6px; border-radius: 3px; background: var(--surface-elevated);">${j.status}</span></td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-tertiary);">No audit history recorded</td></tr>';
            }
        } catch (e) {
            console.error(e);
        }
    }

    async function loadTelemetry() {
        try {
            const res = await fetch('/api/status');
            const d = await res.json();
            document.getElementById('telemetryCpu').textContent = d.cpu_load || '0.00';
            document.getElementById('telemetryRam').textContent = `${d.ram_used || 0} / ${d.ram_total || 0} MB`;
            document.getElementById('telemetryTemp').textContent = `${d.temp_c || 0}°C`;
            document.getElementById('telemetryDisk').textContent = `${d.disk_free || 0}% Free`;
            document.getElementById('telemetryUptime').textContent = 'Uptime: ' + (d.uptime || '--');
        } catch (e) {}
    }

    // ==========================================
    // ADMIN ACTIONS
    // ==========================================
    document.getElementById('btnUnlockAdmin').addEventListener('click', async () => {
        const pass = document.getElementById('adminPassInput').value;
        const formData = new FormData();
        formData.append('password', pass);

        const res = await fetch('/api/admin/auth', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.success) {
            currentAdminToken = data.token;
            sessionStorage.setItem('admin_token', currentAdminToken);
            document.getElementById('adminLockedState').style.display = 'none';
            document.getElementById('adminUnlockedState').style.display = 'block';
            document.getElementById('btnAdminLock').textContent = '🔓 Admin';
            showToast('Administrator privileges unlocked', 'success');
        } else {
            showToast('Invalid password', 'error');
        }
    });

    async function executeAdminAction(action, extraData={}) {
        const formData = new FormData();
        formData.append('action', action);
        formData.append('token', currentAdminToken);
        for (let k in extraData) formData.append(k, extraData[k]);

        const res = await fetch('/api/admin/action', {
            method: 'POST',
            headers: { 'X-Admin-Token': currentAdminToken },
            body: formData
        });
        const data = await res.json();
        if (data.success) showToast(data.message, 'success');
        else showToast('Error: ' + data.error, 'error');
    }

    document.getElementById('btnSaveRates').addEventListener('click', () => {
        executeAdminAction('update_rates', {
            rate_bw: document.getElementById('adminRateBw').value,
            rate_color: document.getElementById('adminRateColor').value,
            currency_symbol: '₱'
        });
    });
    document.getElementById('btnAdminRestartCups').addEventListener('click', () => executeAdminAction('restart_cups'));
    document.getElementById('btnAdminPurgeCache').addEventListener('click', () => executeAdminAction('purge_cache'));
    document.getElementById('btnAdminReboot').addEventListener('click', () => {
        if (confirm('Reboot print server appliance now?')) executeAdminAction('reboot');
    });
    document.getElementById('btnAdminShutdown').addEventListener('click', () => {
        if (confirm('Shut down print server appliance now?')) executeAdminAction('shutdown');
    });

    // Initial boot
    loadPrinters();
    updatePreviewLayout();
</script>
</body>
</html>"""

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/api/status')
def api_status():
    temp_c = 0
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp_c = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass

    ram_used = 0
    ram_total = 0
    try:
        res = subprocess.run(['free', '-m'], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if line.startswith('Mem:'):
                parts = line.split()
                ram_total = int(parts[1])
                ram_used = int(parts[2])
    except Exception:
        pass

    disk_free = 0
    try:
        st = os.statvfs('/')
        disk_free = round((st.f_bavail / st.f_blocks) * 100, 1)
    except Exception:
        pass

    cpu_load = "0.00"
    try:
        cpu_load = str(os.getloadavg()[0])
    except Exception:
        pass

    uptime_str = "--"
    try:
        res = subprocess.run(['uptime', '-p'], capture_output=True, text=True)
        uptime_str = res.stdout.strip()
    except Exception:
        pass

    return jsonify({
        'temp_c': temp_c,
        'ram_used': ram_used,
        'ram_total': ram_total,
        'disk_free': disk_free,
        'cpu_load': cpu_load,
        'uptime': uptime_str
    })

@app.route('/api/printers')
def api_printers():
    printers, def_p = get_printers()
    return jsonify({'printers': printers, 'default': def_p})

@app.route('/api/inspect', methods=['POST'])
def api_inspect():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    file = request.files['file']
    colormodel = request.form.get('colormodel', 'RGB')
    copies = int(request.form.get('copies', '1'))

    suffix = os.path.splitext(file.filename)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        raw_path = tmp.name

    temp_dir = None
    try:
        conv_pdf, temp_dir = convert_to_pdf(raw_path, file.filename)
        pages = get_pdf_page_count(conv_pdf)
        rate = get_rate(colormodel == 'RGB')
        cost = rate * pages * copies
        symbol = get_setting('currency_symbol', '₱')
        return jsonify({
            'success': True,
            'pages': pages,
            'copies': copies,
            'cost': cost,
            'cost_formatted': f"{symbol}{cost:.2f}",
            'filename': file.filename
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(raw_path):
            try: os.remove(raw_path)
            except Exception: pass

@app.route('/api/print', methods=['POST'])
def api_print():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Empty filename'}), 400

    printer = request.form.get('printer', '')
    if not printer:
        _, def_p = get_printers()
        printer = def_p or 'HP_Smart_Tank_520'

    copies = request.form.get('copies', '1')
    pagesize = request.form.get('pagesize', 'A4')
    orientation = request.form.get('orientation', 'auto')
    margin_mode = request.form.get('margin_mode', 'printable')
    mediatype = request.form.get('mediatype', 'Plain')
    quality = request.form.get('quality', 'Normal')
    colormodel = request.form.get('colormodel', 'RGB')
    duplex = request.form.get('duplex', 'None')
    resolution = request.form.get('resolution', '300')
    fitpage = request.form.get('fitpage', 'true')
    pagerange = request.form.get('pagerange', '').strip()

    suffix = os.path.splitext(file.filename)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        raw_path = tmp.name

    temp_dir = None
    try:
        conv_pdf, temp_dir = convert_to_pdf(raw_path, file.filename)
        pages = get_pdf_page_count(conv_pdf)
        copies_int = int(copies)
        rate = get_rate(colormodel == 'RGB')
        cost = rate * pages * copies_int

        cmd = ['lp', '-d', printer, '-n', str(copies)]
        if pagesize: cmd.extend(['-o', f'PageSize={pagesize}'])
        if mediatype: cmd.extend(['-o', f'MediaType={mediatype}'])
        if quality: cmd.extend(['-o', f'OutputMode={quality}'])
        if colormodel: cmd.extend(['-o', f'ColorModel={colormodel}'])
        if duplex and duplex != 'None': cmd.extend(['-o', f'Duplex={duplex}'])
        if orientation in ['3', '4']: cmd.extend(['-o', f'orientation-requested={orientation}'])
        if fitpage == 'true': cmd.extend(['-o', 'fit-to-page'])
        if pagerange: cmd.extend(['-o', f'page-ranges={pagerange}'])
        if resolution in ['150', '300', '600', '1200']: cmd.extend(['-o', f'resolution={resolution}dpi'])

        if margin_mode == 'none':
            cmd.extend(['-o', 'page-top=0', '-o', 'page-bottom=0', '-o', 'page-left=0', '-o', 'page-right=0'])
        elif margin_mode == 'normal':
            cmd.extend(['-o', 'page-top=36', '-o', 'page-bottom=36', '-o', 'page-left=36', '-o', 'page-right=36'])
        elif margin_mode == 'custom':
            try:
                m_top = int(float(request.form.get('m_top', '10')) * 2.83465)
                m_bottom = int(float(request.form.get('m_bottom', '10')) * 2.83465)
                m_left = int(float(request.form.get('m_left', '10')) * 2.83465)
                m_right = int(float(request.form.get('m_right', '10')) * 2.83465)
                cmd.extend([
                    '-o', f'page-top={m_top}',
                    '-o', f'page-bottom={m_bottom}',
                    '-o', f'page-left={m_left}',
                    '-o', f'page-right={m_right}'
                ])
            except Exception:
                pass

        cmd.append(conv_pdf)

        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        job_id = res.stdout.strip()

        record_ledger(file.filename, printer, pages, copies_int, pages * copies_int, colormodel, cost, request.remote_addr, "COMPLETED")
        return jsonify({'success': True, 'job_id': job_id, 'pages': pages, 'cost': cost})
    except subprocess.CalledProcessError as e:
        return jsonify({'success': False, 'error': e.stderr.strip() or str(e)}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(raw_path):
            try: os.remove(raw_path)
            except Exception: pass

@app.route('/api/print/pin-hold', methods=['POST'])
def api_pin_hold():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    file = request.files['file']
    pin = request.form.get('pin', '').strip()
    if not pin or len(pin) != 4 or not pin.isdigit():
        return jsonify({'success': False, 'error': 'PIN must be exactly 4 digits'}), 400

    printer = request.form.get('printer', '')
    if not printer:
        _, def_p = get_printers()
        printer = def_p or 'HP_Smart_Tank_520'

    copies = int(request.form.get('copies', '1'))
    pagesize = request.form.get('pagesize', 'A4')
    orientation = request.form.get('orientation', 'auto')
    margin_mode = request.form.get('margin_mode', 'printable')
    mediatype = request.form.get('mediatype', 'Plain')
    quality = request.form.get('quality', 'Normal')
    colormodel = request.form.get('colormodel', 'RGB')
    duplex = request.form.get('duplex', 'None')
    resolution = request.form.get('resolution', '300')
    fitpage = request.form.get('fitpage', 'true')
    pagerange = request.form.get('pagerange', '').strip()

    job_id = f"pin_{int(time.time())}_{secrets.token_hex(4)}"
    pin_hash = hashlib.sha256(pin.encode('utf-8')).hexdigest()

    suffix = os.path.splitext(file.filename)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        raw_path = tmp.name

    temp_dir = None
    try:
        conv_pdf, temp_dir = convert_to_pdf(raw_path, file.filename)
        dest_path = os.path.join(SPOOL_PINNED_DIR, f"{job_id}.pdf")
        shutil.copy2(conv_pdf, dest_path)
        os.chmod(dest_path, 0o600)

        pages = get_pdf_page_count(dest_path)
        rate = get_rate(colormodel == 'RGB')
        cost = rate * pages * copies

        options = {
            'printer': printer,
            'copies': copies,
            'pagesize': pagesize,
            'orientation': orientation,
            'margin_mode': margin_mode,
            'm_top': request.form.get('m_top', '10'),
            'm_bottom': request.form.get('m_bottom', '10'),
            'm_left': request.form.get('m_left', '10'),
            'm_right': request.form.get('m_right', '10'),
            'mediatype': mediatype,
            'quality': quality,
            'colormodel': colormodel,
            'duplex': duplex,
            'resolution': resolution,
            'fitpage': fitpage,
            'pagerange': pagerange
        }

        conn = get_db()
        with conn:
            conn.execute("""
                INSERT INTO pinned_jobs 
                (job_id, file_path, filename, pin_hash, pages, copies, colormodel, printer, options_json, cost, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id, dest_path, file.filename, pin_hash, pages, copies, colormodel, printer,
                json.dumps(options), cost, int(time.time()), int(time.time() + 24 * 3600)
            ))

        return jsonify({
            'success': True,
            'job_id': job_id,
            'pages': pages,
            'copies': copies,
            'cost': cost,
            'message': 'Document securely held in PIN queue.'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(raw_path):
            try: os.remove(raw_path)
            except Exception: pass

@app.route('/api/pin/list', methods=['GET'])
def api_pin_list():
    conn = get_db()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute("SELECT job_id, file_path FROM pinned_jobs WHERE expires_at < ?", (now,))
    expired = cur.fetchall()
    for row in expired:
        try: os.remove(row['file_path'])
        except Exception: pass
    cur.execute("DELETE FROM pinned_jobs WHERE expires_at < ?", (now,))
    conn.commit()

    cur.execute("SELECT job_id, filename, pages, copies, colormodel, printer, cost, created_at FROM pinned_jobs ORDER BY created_at DESC")
    jobs = []
    for row in cur.fetchall():
        jobs.append({
            'job_id': row['job_id'],
            'filename': row['filename'],
            'pages': row['pages'],
            'copies': row['copies'],
            'colormodel': row['colormodel'],
            'printer': row['printer'],
            'cost': row['cost'],
            'created_at': datetime.fromtimestamp(row['created_at']).strftime('%Y-%m-%d %H:%M:%S')
        })
    return jsonify({'success': True, 'jobs': jobs})

@app.route('/api/pin/release', methods=['POST'])
def api_pin_release():
    job_id = request.form.get('job_id', '').strip()
    pin = request.form.get('pin', '').strip()
    if not job_id or not pin:
        return jsonify({'success': False, 'error': 'Missing Job ID or PIN'}), 400

    pin_hash = hashlib.sha256(pin.encode('utf-8')).hexdigest()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM pinned_jobs WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({'success': False, 'error': 'Job not found or expired'}), 404

    if row['pin_hash'] != pin_hash:
        return jsonify({'success': False, 'error': 'Incorrect PIN. Please re-enter.'}), 401

    options = json.loads(row['options_json'])
    printer = options.get('printer') or 'HP_Smart_Tank_520'
    copies = options.get('copies', 1)
    file_path = row['file_path']

    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': 'Spool file missing'}), 500

    try:
        cmd = ['lp', '-d', printer, '-n', str(copies)]
        if options.get('pagesize'): cmd.extend(['-o', f"PageSize={options['pagesize']}"])
        if options.get('mediatype'): cmd.extend(['-o', f"MediaType={options['mediatype']}"])
        if options.get('quality'): cmd.extend(['-o', f"OutputMode={options['quality']}"])
        if options.get('colormodel'): cmd.extend(['-o', f"ColorModel={options['colormodel']}"])
        if options.get('duplex') and options['duplex'] != 'None': cmd.extend(['-o', f"Duplex={options['duplex']}"])
        if options.get('orientation') in ['3', '4']: cmd.extend(['-o', f"orientation-requested={options['orientation']}"])
        if options.get('fitpage') == 'true': cmd.extend(['-o', 'fit-to-page'])
        if options.get('pagerange'): cmd.extend(['-o', f"page-ranges={options['pagerange']}"])
        if options.get('resolution'): cmd.extend(['-o', f"resolution={options['resolution']}dpi"])

        m_mode = options.get('margin_mode', 'printable')
        if m_mode == 'none':
            cmd.extend(['-o', 'page-top=0', '-o', 'page-bottom=0', '-o', 'page-left=0', '-o', 'page-right=0'])
        elif m_mode == 'normal':
            cmd.extend(['-o', 'page-top=36', '-o', 'page-bottom=36', '-o', 'page-left=36', '-o', 'page-right=36'])
        elif m_mode == 'custom':
            try:
                m_top = int(float(options.get('m_top', '10')) * 2.83465)
                m_bottom = int(float(options.get('m_bottom', '10')) * 2.83465)
                m_left = int(float(options.get('m_left', '10')) * 2.83465)
                m_right = int(float(options.get('m_right', '10')) * 2.83465)
                cmd.extend([
                    '-o', f'page-top={m_top}',
                    '-o', f'page-bottom={m_bottom}',
                    '-o', f'page-left={m_left}',
                    '-o', f'page-right={m_right}'
                ])
            except Exception:
                pass

        cmd.append(file_path)

        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        cups_job = res.stdout.strip()

        try: os.remove(file_path)
        except Exception: pass
        cur.execute("DELETE FROM pinned_jobs WHERE job_id = ?", (job_id,))
        conn.commit()

        record_ledger(row['filename'], printer, row['pages'], row['copies'], row['pages'] * row['copies'],
                      row['colormodel'], row['cost'], request.remote_addr, "RELEASED")

        return jsonify({'success': True, 'cups_job': cups_job, 'message': 'Job released to printer.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/reports/ledger', methods=['GET'])
def api_reports_ledger():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as total_jobs, SUM(total_pages) as total_pages, SUM(cost) as total_revenue FROM print_ledger")
    stats = cur.fetchone()

    cur.execute("SELECT * FROM print_ledger ORDER BY timestamp DESC LIMIT 100")
    rows = cur.fetchall()
    jobs = []
    symbol = get_setting('currency_symbol', '₱')
    for r in rows:
        jobs.append({
            'id': r['id'],
            'timestamp': datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M:%S'),
            'title': r['job_title'],
            'printer': r['printer'],
            'pages': r['pages'],
            'copies': r['copies'],
            'total_pages': r['total_pages'],
            'colormodel': r['colormodel'],
            'cost': f"{symbol}{r['cost']:.2f}",
            'client_ip': r['client_ip'],
            'status': r['status']
        })
    return jsonify({
        'success': True,
        'stats': {
            'total_jobs': stats['total_jobs'] or 0,
            'total_pages': stats['total_pages'] or 0,
            'total_revenue': f"{symbol}{(stats['total_revenue'] or 0):.2f}"
        },
        'jobs': jobs
    })

@app.route('/api/reports/export.csv', methods=['GET'])
def api_reports_export_csv():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM print_ledger ORDER BY timestamp DESC")
    rows = cur.fetchall()

    def generate_csv():
        yield "ID,Timestamp,Job Title,Printer,Pages,Copies,Total Pages,Color Mode,Cost,Client IP,Status\n"
        for r in rows:
            ts = datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            title = r['job_title'].replace('"', '""')
            yield f'"{r["id"]}","{ts}","{title}","{r["printer"]}","{r["pages"]}","{r["copies"]}","{r["total_pages"]}","{r["colormodel"]}","{r["cost"]:.2f}","{r["client_ip"]}","{r["status"]}"\n'

    return Response(generate_csv(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=print_ledger.csv'})

@app.route('/api/admin/auth', methods=['POST'])
def api_admin_auth():
    password = request.form.get('password', '')
    admin_hash = get_setting('admin_hash', hashlib.sha256('admin'.encode('utf-8')).hexdigest())
    req_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
    if req_hash == admin_hash:
        token = secrets.token_hex(16)
        ACTIVE_TOKENS[token] = time.time() + 7200
        return jsonify({'success': True, 'token': token})
    return jsonify({'success': False, 'error': 'Invalid administrator password'}), 401

@app.route('/api/admin/action', methods=['POST'])
def api_admin_action():
    token = request.headers.get('X-Admin-Token') or request.form.get('token')
    if not verify_admin_token(token):
        return jsonify({'success': False, 'error': 'Unauthorized. Admin login required.'}), 401

    action = request.form.get('action')
    if action == 'reboot':
        subprocess.Popen(['reboot'])
        return jsonify({'success': True, 'message': 'System rebooting in 3 seconds...'})
    elif action == 'shutdown':
        subprocess.Popen(['poweroff'])
        return jsonify({'success': True, 'message': 'System shutting down...'})
    elif action == 'restart_cups':
        subprocess.run(['systemctl', 'restart', 'cups'], check=True)
        return jsonify({'success': True, 'message': 'CUPS print spooler restarted successfully.'})
    elif action == 'purge_cache':
        subprocess.run('rm -rf /tmp/webprint* /tmp/conv_* /tmp/scan_*', shell=True)
        return jsonify({'success': True, 'message': 'Temporary spool cache purged.'})
    elif action == 'update_rates':
        rate_bw = request.form.get('rate_bw', '2.00')
        rate_color = request.form.get('rate_color', '5.00')
        conn = get_db()
        with conn:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'rate_bw'", (rate_bw,))
            conn.execute("UPDATE settings SET value = ? WHERE key = 'rate_color'", (rate_color,))
        return jsonify({'success': True, 'message': 'Rates updated successfully.'})
    elif action == 'change_password':
        new_pass = request.form.get('new_password', '')
        if len(new_pass) < 4:
            return jsonify({'success': False, 'error': 'Password must be at least 4 characters'}), 400
        new_hash = hashlib.sha256(new_pass.encode('utf-8')).hexdigest()
        conn = get_db()
        with conn:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'admin_hash'", (new_hash,))
        return jsonify({'success': True, 'message': 'Admin password changed.'})
    return jsonify({'success': False, 'error': 'Unknown action'}), 400

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 80))
    app.run(host='::', port=port)
