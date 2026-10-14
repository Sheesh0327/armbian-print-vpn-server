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

app = Flask(__name__)
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
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Print Server Appliance</title>
    <script>
        (function() {
            const savedTheme = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
            document.documentElement.setAttribute('data-theme', savedTheme);
        })();
    </script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/fabric.js/5.3.1/fabric.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    <style>
        :root {
            --radius-sm: 6px;
            --radius-md: 8px;
            --radius-lg: 12px;
            --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --transition-theme: background-color 0.2s ease, border-color 0.2s ease, color 0.2s ease;
        }

        html[data-theme="dark"] {
            --bg: #09090b;
            --surface: #121215;
            --surface-elevated: #18181b;
            --border: #27272a;
            --border-hover: #3f3f46;
            --text-primary: #f4f4f5;
            --text-secondary: #a1a1aa;
            --text-tertiary: #71717a;
            --btn-bg: #f4f4f5;
            --btn-text: #09090b;
            --badge-bg: #18181b;
            --desk-bg: #08080a;
            --paper-bg: #ffffff;
            --paper-text: #09090b;
            --paper-border: #e4e4e7;
            --paper-shadow: 0 16px 40px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(255, 255, 255, 0.08);
            --margin-line: rgba(59, 130, 246, 0.55);
            --accent: #3b82f6;
            --accent-hover: #2563eb;
            --success: #22c55e;
            --danger: #ef4444;
            --warning: #f59e0b;
        }

        html[data-theme="light"] {
            --bg: #f8fafc;
            --surface: #ffffff;
            --surface-elevated: #f1f5f9;
            --border: #e2e8f0;
            --border-hover: #cbd5e1;
            --text-primary: #0f172a;
            --text-secondary: #475569;
            --text-tertiary: #94a3b8;
            --btn-bg: #0f172a;
            --btn-text: #ffffff;
            --badge-bg: #f1f5f9;
            --desk-bg: #e2e8f0;
            --paper-bg: #ffffff;
            --paper-text: #0f172a;
            --paper-border: #cbd5e1;
            --paper-shadow: 0 14px 30px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0, 0, 0, 0.06);
            --margin-line: rgba(37, 99, 235, 0.45);
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --success: #16a34a;
            --danger: #dc2626;
            --warning: #d97706;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            background-color: var(--bg);
            color: var(--text-primary);
            font-family: var(--font);
            font-size: 14px;
            line-height: 1.5;
            -webkit-font-smoothing: antialiased;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            transition: var(--transition-theme);
        }

        header {
            border-bottom: 1px solid var(--border);
            padding: 10px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--surface);
            position: sticky;
            top: 0;
            z-index: 50;
            gap: 16px;
        }

        .header-brand {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .header-title {
            font-size: 15px;
            font-weight: 700;
            letter-spacing: -0.01em;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .header-title .tag {
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            padding: 2px 6px;
            border-radius: 4px;
            background: var(--surface-elevated);
            color: var(--accent);
            border: 1px solid var(--border);
        }

        .printer-select-wrapper {
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 4px 10px;
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
            gap: 2px;
            background: var(--surface-elevated);
            padding: 3px;
            border-radius: var(--radius-md);
            border: 1px solid var(--border);
            overflow-x: auto;
        }

        .nav-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-size: 12px;
            font-weight: 500;
            padding: 6px 14px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.15s ease;
        }

        .nav-btn:hover { color: var(--text-primary); }
        .nav-btn.active {
            background: var(--surface);
            color: var(--text-primary);
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
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
            padding: 6px 10px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s;
        }
        .btn-theme:hover, .btn-lock:hover { color: var(--text-primary); border-color: var(--border-hover); }

        main {
            flex: 1;
            max-width: 1600px;
            width: 100%;
            margin: 0 auto;
            padding: 16px 20px 40px;
        }

        .tab-content { display: none; }
        .tab-content.active { display: block; }

        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        }

        .grid-layout {
            display: grid;
            grid-template-columns: minmax(0, 1fr) 390px;
            gap: 20px;
            align-items: start;
        }

        @media (max-width: 1080px) {
            .grid-layout { grid-template-columns: 1fr; }
            header { flex-wrap: wrap; }
        }

        label {
            display: block;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }

        input[type="text"], input[type="number"], input[type="password"], select {
            width: 100%;
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 8px 12px;
            font-size: 13px;
            color: var(--text-primary);
            outline: none;
            transition: border-color 0.15s ease;
        }
        input:focus, select:focus { border-color: var(--accent); }

        .form-group { margin-bottom: 14px; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }

        .margin-input-col {
            display: flex;
            flex-direction: column;
            gap: 3px;
            font-size: 11px;
            color: var(--text-secondary);
        }

        .btn-primary {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            width: 100%;
            background: var(--accent);
            color: #ffffff;
            border: none;
            padding: 11px 16px;
            font-size: 13px;
            font-weight: 600;
            border-radius: var(--radius-md);
            cursor: pointer;
            transition: background 0.15s ease;
        }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

        .btn-secondary {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            background: var(--surface-elevated);
            color: var(--text-primary);
            border: 1px solid var(--border);
            padding: 7px 12px;
            font-size: 12px;
            font-weight: 500;
            border-radius: var(--radius-sm);
            cursor: pointer;
            transition: all 0.15s;
        }
        .btn-secondary:hover { border-color: var(--border-hover); }

        /* Cost Badge Card */
        .cost-card {
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 12px 14px;
            margin-bottom: 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .cost-title { font-size: 12px; color: var(--text-secondary); }
        .cost-amount { font-size: 18px; font-weight: 700; color: var(--accent); font-family: var(--font-mono); }

        /* Drag & Drop Upload Zone */
        .dropzone {
            border: 2px dashed var(--border);
            border-radius: var(--radius-lg);
            padding: 24px 16px;
            text-align: center;
            background: var(--surface);
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
        }
        .dropzone:hover, .dropzone.dragover {
            border-color: var(--accent);
            background: var(--surface-elevated);
        }
        .dropzone input[type="file"] {
            position: absolute;
            inset: 0;
            width: 100%;
            height: 100%;
            opacity: 0;
            cursor: pointer;
        }

        /* Large Preview Desk with Realistic Paper Sheet */
        .preview-desk {
            background: var(--desk-bg);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            min-height: 620px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 20px;
            position: relative;
            overflow: hidden;
            transition: var(--transition-theme);
        }

        .preview-top-toolbar {
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding-bottom: 12px;
            margin-bottom: 12px;
            border-bottom: 1px solid var(--border);
        }

        .paper-sheet {
            background: var(--paper-bg);
            color: var(--paper-text);
            box-shadow: var(--paper-shadow);
            border-radius: 3px;
            position: relative;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            transition: width 0.25s cubic-bezier(0.16, 1, 0.3, 1), 
                        height 0.25s cubic-bezier(0.16, 1, 0.3, 1),
                        filter 0.2s ease;
        }

        .printable-area {
            position: absolute;
            border: 1px dashed var(--margin-line);
            border-radius: 2px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            pointer-events: none;
            transition: top 0.2s ease, bottom 0.2s ease, left 0.2s ease, right 0.2s ease;
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

        .preview-dimensions-tag {
            margin-top: 14px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            font-weight: 500;
            color: var(--text-tertiary);
            background: var(--surface-elevated);
            padding: 4px 12px;
            border-radius: 9999px;
            border: 1px solid var(--border);
        }

        .pagination-controls {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            font-weight: 600;
        }
        .page-btn {
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            color: var(--text-primary);
            width: 26px;
            height: 26px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 13px;
        }
        .page-btn:hover { border-color: var(--border-hover); }

        /* ========================================================
           FULL-FLEDGED CANVA CLONE WORKSPACE STYLES
           ======================================================== */
        .canva-workspace {
            display: flex;
            height: calc(100vh - 120px);
            min-height: 680px;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0,0,0,0.15);
        }

        /* Left Dock (Icon Rail) */
        .canva-dock {
            width: 68px;
            background: #0d0d10;
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 12px 0;
            gap: 6px;
            flex-shrink: 0;
            z-index: 10;
        }

        .dock-item {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            width: 56px;
            height: 52px;
            background: transparent;
            border: none;
            color: #a1a1aa;
            cursor: pointer;
            border-radius: var(--radius-md);
            font-size: 10px;
            font-weight: 600;
            gap: 4px;
            transition: all 0.15s ease;
        }
        .dock-item:hover { color: #f4f4f5; background: rgba(255,255,255,0.06); }
        .dock-item.active { color: #ffffff; background: var(--accent); }
        .dock-item svg { width: 18px; height: 18px; fill: currentColor; }

        /* Side Drawer Panel */
        .canva-drawer {
            width: 310px;
            background: var(--surface);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            overflow-y: auto;
            padding: 16px;
            z-index: 9;
        }
        .drawer-section { display: none; }
        .drawer-section.active { display: block; }
        .drawer-title { font-size: 14px; font-weight: 700; margin-bottom: 12px; color: var(--text-primary); }

        /* Template Cards */
        .template-card {
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 12px;
            margin-bottom: 10px;
            cursor: pointer;
            transition: all 0.15s;
        }
        .template-card:hover { border-color: var(--accent); transform: translateY(-1px); }
        .template-card-title { font-size: 13px; font-weight: 600; margin-bottom: 2px; }
        .template-card-desc { font-size: 11px; color: var(--text-secondary); }

        /* Elements Grid */
        .elements-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 8px;
            margin-bottom: 16px;
        }
        .element-btn {
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 12px 6px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            color: var(--text-primary);
            font-size: 11px;
            font-weight: 500;
            gap: 6px;
            transition: all 0.15s;
        }
        .element-btn:hover { border-color: var(--accent); background: var(--surface); }

        /* Central Studio Canvas & Toolbar */
        .canva-main {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--desk-bg);
            overflow: hidden;
            position: relative;
        }

        .canva-topbar {
            height: 48px;
            background: var(--surface);
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 16px;
            gap: 12px;
            flex-shrink: 0;
            z-index: 8;
        }

        .canva-topbar-tools {
            display: flex;
            align-items: center;
            gap: 6px;
            overflow-x: auto;
        }

        .canva-btn {
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            color: var(--text-primary);
            padding: 5px 10px;
            border-radius: var(--radius-sm);
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            transition: all 0.15s;
            white-space: nowrap;
        }
        .canva-btn:hover { border-color: var(--border-hover); }
        .canva-btn.active { background: var(--accent); color: #ffffff; border-color: var(--accent); }

        .canva-viewport {
            flex: 1;
            overflow: auto;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 30px;
            position: relative;
        }

        .canva-sheet-container {
            box-shadow: 0 20px 60px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.06);
            background: #ffffff;
            transition: transform 0.1s ease;
        }

        .canva-bottom-bar {
            height: 32px;
            background: var(--surface);
            border-top: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 16px;
            font-size: 11px;
            color: var(--text-secondary);
            flex-shrink: 0;
        }

        /* Tables */
        .table-wrapper {
            width: 100%;
            overflow-x: auto;
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }
        th {
            background: var(--surface-elevated);
            padding: 10px 14px;
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

        /* Status Toast */
        .toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            box-shadow: 0 10px 25px rgba(0,0,0,0.2);
            border-radius: var(--radius-md);
            padding: 12px 20px;
            font-size: 13px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 10px;
            z-index: 100;
            transform: translateY(150%);
            transition: transform 0.25s ease;
        }
        .toast.show { transform: translateY(0); }
        .toast.success { border-color: var(--success); }
        .toast.error { border-color: var(--danger); }

        /* Modal */
        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.6);
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
            border-radius: var(--radius-lg);
            padding: 24px;
            max-width: 420px;
            width: 90%;
            box-shadow: 0 20px 40px rgba(0,0,0,0.4);
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
            <select id="headerPrinterSelect" style="border:none; background:transparent; padding:0; font-size:12px; font-weight:600;">
                <option value="">Detecting printers...</option>
            </select>
        </div>
    </div>

    <nav class="header-nav">
        <button class="nav-btn active" data-tab="print">Print</button>
        <button class="nav-btn" data-tab="studio">Design Studio</button>
        <button class="nav-btn" data-tab="scan">Scan</button>
        <button class="nav-btn" data-tab="pin">PIN Release</button>
        <button class="nav-btn" data-tab="history">History</button>
        <button class="nav-btn" data-tab="settings">Settings</button>
    </nav>

    <div class="header-controls">
        <button class="btn-lock" id="btnAdminLock">🔒 Locked</button>
        <button class="btn-theme" id="btnThemeToggle" title="Toggle theme">🌓</button>
    </div>
</header>

<main>
    <!-- TAB 1: ADVANCED PRINT (THE ONLY AND PERMANENT PRINT MODE) -->
    <div class="tab-content active" id="tab-print">
        <div class="grid-layout">
            <!-- Left: Dynamic Virtual Desk & Animated Paper Sheet Preview -->
            <div class="preview-desk">
                <div class="preview-top-toolbar" id="previewTopToolbar" style="display: none;">
                    <div style="font-weight: 600; font-size: 13px;" id="previewDocTitle">Document.pdf</div>
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <div class="pagination-controls" id="previewPagination" style="display: none;">
                            <button type="button" class="page-btn" id="btnPrev">‹</button>
                            <span id="pageIndicator">1 / 1</span>
                            <button type="button" class="page-btn" id="btnNext">›</button>
                        </div>
                        <button type="button" class="btn-secondary" id="btnClearPreview" style="padding: 3px 8px; font-size: 11px;">✕ Clear</button>
                    </div>
                </div>

                <div class="paper-sheet" id="paperSheet" style="width: 440px; height: 622px;">
                    <div class="printable-area" id="printableArea">
                        <div class="content-render-wrap" id="renderWrap">
                            <canvas id="pdfCanvas" style="display: none;"></canvas>
                            <img id="imgPreview" style="display: none;" alt="Preview">
                            <div id="txtPreview" style="display: none; padding: 20px; font-size: 12px; color: #333; overflow: hidden; width:100%; height:100%; word-break: break-all;"></div>
                        </div>
                    </div>
                </div>

                <div id="previewEmptyState" style="text-align: center; color: var(--text-tertiary); margin-top: 14px;">
                    <div style="font-size: 32px; margin-bottom: 6px;">📄</div>
                    <div style="font-weight: 500;">No document loaded</div>
                    <div style="font-size: 12px;">Drop any file on the right to inspect and preview</div>
                </div>

                <div class="preview-dimensions-tag" id="previewDimensionsTag" style="display: none;">
                    <span id="sheetFormatTag">A4 • 210 × 297 mm</span>
                    <span>•</span>
                    <span id="sheetOrientationTag">Portrait</span>
                    <span>•</span>
                    <span id="sheetMarginTag">Printable Area</span>
                    <span>•</span>
                    <span id="sheetColorTag">Color</span>
                </div>
            </div>

            <!-- Right: Full Advanced Controls Panel (Permanent) -->
            <div class="card">
                <div class="form-group">
                    <label>Document Ingestion</label>
                    <div class="dropzone" id="printDropzone">
                        <input type="file" id="printFileInput" accept=".pdf,.doc,.docx,.odt,.rtf,.txt,.csv,.xlsx,.xls,.pptx,.ppt,.png,.jpg,.jpeg,.webp,.heic,.heif,.avif,.tiff,.bmp">
                        <div style="font-size: 22px; margin-bottom: 4px;">📂</div>
                        <div style="font-weight: 600;" id="printFileName">Click or drop file here</div>
                        <div style="font-size: 11px; color: var(--text-tertiary); margin-top: 4px;">
                            PDF, Word, Excel, PPT, iPhone HEIC/AVIF, Images
                        </div>
                    </div>
                </div>

                <div class="cost-card">
                    <div>
                        <div class="cost-title">Pre-Flight Estimated Cost</div>
                        <div style="font-size: 11px; color: var(--text-secondary);" id="costDetails">0 pages</div>
                    </div>
                    <div class="cost-amount" id="costTotal">₱0.00</div>
                </div>

                <!-- Paper & Layout Group -->
                <div class="grid-2 form-group">
                    <div>
                        <label>Paper Size</label>
                        <select id="printPageSize">
                            <option value="A4" selected>A4 (210 × 297 mm)</option>
                            <option value="Letter">Letter (8.5 × 11 in)</option>
                            <option value="Legal">Legal (8.5 × 14 in)</option>
                            <option value="A5">A5 (148 × 210 mm)</option>
                            <option value="A6">A6 (105 × 148 mm)</option>
                            <option value="B5">B5 (176 × 250 mm)</option>
                            <option value="4x6in">4 × 6 in Photo</option>
                            <option value="5x7in">5 × 7 in Photo</option>
                            <option value="8x10">8 × 10 in Print</option>
                            <option value="EnvelopeDL">Envelope DL</option>
                        </select>
                    </div>
                    <div>
                        <label>Orientation</label>
                        <select id="printOrientation">
                            <option value="auto" selected>Auto-detect</option>
                            <option value="3">Portrait</option>
                            <option value="4">Landscape</option>
                        </select>
                    </div>
                </div>

                <!-- Margins Controls -->
                <div class="form-group">
                    <label>Margins</label>
                    <select id="printMarginMode">
                        <option value="printable" selected>Full Printable Area (Standard)</option>
                        <option value="none">Zero / Borderless (0 mm)</option>
                        <option value="normal">Normal (12.7 mm / 0.5 in)</option>
                        <option value="custom">Custom Margins (mm)...</option>
                    </select>
                </div>

                <!-- Custom Margins Box (Appears when Custom is selected) -->
                <div id="customMarginBox" class="form-group" style="display: none;">
                    <label style="font-size: 11px;">Custom Margins (mm)</label>
                    <div class="grid-4">
                        <div class="margin-input-col">
                            <span>Top</span>
                            <input type="number" id="mTop" value="10" min="0" max="60">
                        </div>
                        <div class="margin-input-col">
                            <span>Bottom</span>
                            <input type="number" id="mBottom" value="10" min="0" max="60">
                        </div>
                        <div class="margin-input-col">
                            <span>Left</span>
                            <input type="number" id="mLeft" value="10" min="0" max="60">
                        </div>
                        <div class="margin-input-col">
                            <span>Right</span>
                            <input type="number" id="mRight" value="10" min="0" max="60">
                        </div>
                    </div>
                </div>

                <!-- Media Type & Quality -->
                <div class="grid-2 form-group">
                    <div>
                        <label>Media Type</label>
                        <select id="printMediaType">
                            <option value="Plain" selected>Plain Paper</option>
                            <option value="Glossy">Glossy Photo</option>
                            <option value="FastGlossy">Everyday Photo</option>
                            <option value="CoatedMatte">Matte Paper</option>
                            <option value="CoatedBrochure">Brochure</option>
                        </select>
                    </div>
                    <div>
                        <label>Print Quality</label>
                        <select id="printQuality">
                            <option value="Normal" selected>Normal</option>
                            <option value="Best">Best Quality</option>
                            <option value="Photo">High-Res Photo</option>
                        </select>
                    </div>
                </div>

                <!-- DPI & Duplex -->
                <div class="grid-2 form-group">
                    <div>
                        <label>Resolution (DPI)</label>
                        <select id="printDpi">
                            <option value="150">150 DPI (Fast)</option>
                            <option value="300" selected>300 DPI (Standard)</option>
                            <option value="600">600 DPI (Ultra Fine)</option>
                            <option value="1200">1200 DPI (Extreme)</option>
                        </select>
                    </div>
                    <div>
                        <label>Duplex (Two-Sided)</label>
                        <select id="printDuplex">
                            <option value="None" selected>Single-Sided</option>
                            <option value="DuplexNoTumble">Long-Edge Duplex</option>
                            <option value="DuplexTumble">Short-Edge Duplex</option>
                        </select>
                    </div>
                </div>

                <!-- Color Mode & Scaling -->
                <div class="grid-2 form-group">
                    <div>
                        <label>Color Mode</label>
                        <select id="printColorModel">
                            <option value="RGB" selected>Full Color (RGB)</option>
                            <option value="KGray">Black & White (Grayscale)</option>
                        </select>
                    </div>
                    <div>
                        <label>Scaling</label>
                        <select id="printScaling">
                            <option value="true" selected>Fit to Printable Area</option>
                            <option value="false">Fill / Crop Page</option>
                        </select>
                    </div>
                </div>

                <!-- Page Range & Copies -->
                <div class="grid-2 form-group">
                    <div>
                        <label>Page Range</label>
                        <input type="text" id="printPageRange" placeholder="e.g. 1-3, 5 (blank: all)">
                    </div>
                    <div>
                        <label>Copies</label>
                        <input type="number" id="printCopies" min="1" max="99" value="1">
                    </div>
                </div>

                <!-- Confidential PIN Queue -->
                <div class="form-group" style="padding-top: 6px;">
                    <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; text-transform: none; font-size: 13px;">
                        <input type="checkbox" id="chkPinHold">
                        <span>Hold in Confidential PIN Queue</span>
                    </label>
                    <div id="pinInputWrapper" style="display: none; margin-top: 8px;">
                        <label>Enter 4-Digit Release PIN</label>
                        <input type="password" id="printPin" maxlength="4" placeholder="••••" style="text-align: center; font-size: 18px; letter-spacing: 0.3em; font-family: var(--font-mono);">
                    </div>
                </div>

                <button class="btn-primary" id="btnSubmitPrint">
                    <span>🖨️ Send to Printer</span>
                </button>
            </div>
        </div>
    </div>

    <!-- TAB 2: FULL-FLEDGED CANVA CLONE DESIGN STUDIO -->
    <div class="tab-content" id="tab-studio">
        <div class="canva-workspace">
            <!-- Left Dock Rail -->
            <div class="canva-dock">
                <button class="dock-item active" data-drawer="templates" title="Templates">
                    <svg viewBox="0 0 24 24"><path d="M4 4h7v7H4V4zm9 0h7v7h-7V4zm-9 9h7v7H4v-7zm9 0h7v7h-7v-7z"/></svg>
                    <span>Design</span>
                </button>
                <button class="dock-item" data-drawer="elements" title="Elements & Stamps">
                    <svg viewBox="0 0 24 24"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
                    <span>Shapes</span>
                </button>
                <button class="dock-item" data-drawer="text" title="Typography">
                    <svg viewBox="0 0 24 24"><path d="M5 4v3h5.5v12h3V7H19V4H5z"/></svg>
                    <span>Text</span>
                </button>
                <button class="dock-item" data-drawer="uploads" title="Upload Photos">
                    <svg viewBox="0 0 24 24"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM14 13v4h-4v-4H7l5-5 5 5h-3z"/></svg>
                    <span>Uploads</span>
                </button>
                <button class="dock-item" data-drawer="qrcode" title="QR Code Generator">
                    <svg viewBox="0 0 24 24"><path d="M2 2h8v8H2V2zm2 2v4h4V4H4zm10-2h8v8h-8V2zm2 2v4h4V4h-4zM2 14h8v8H2v-8zm2 2v4h4v-4H4zm14 0h4v4h-4v-4zm-4-2h2v2h-2v-2zm0 4h2v4h-2v-4zm2-2h4v2h-4v-2zm2 4h2v2h-2v-2z"/></svg>
                    <span>QR Code</span>
                </button>
                <button class="dock-item" data-drawer="draw" title="Freehand Pen">
                    <svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
                    <span>Draw</span>
                </button>
            </div>

            <!-- Drawer Content Panel -->
            <div class="canva-drawer">
                <!-- Section 1: Templates -->
                <div class="drawer-section active" id="drawer-templates">
                    <div class="drawer-title">Canvas Dimensions</div>
                    <div class="form-group">
                        <select id="studioPreset">
                            <option value="a4" selected>A4 Document (210 × 297 mm)</option>
                            <option value="letter">US Letter (8.5 × 11 in)</option>
                            <option value="photo46">4 × 6 in Photo Card</option>
                            <option value="flyer">A5 Flyer / Notice</option>
                            <option value="square">Square 1:1 (200 × 200 mm)</option>
                        </select>
                    </div>

                    <div class="drawer-title" style="margin-top: 16px;">1-Click Canva Templates</div>
                    <div class="template-card" onclick="loadCanvaTemplate('sale')">
                        <div class="template-card-title">🔥 Promotional Sale Poster</div>
                        <div class="template-card-desc">Bold header, 50% badge, offer box, address</div>
                    </div>
                    <div class="template-card" onclick="loadCanvaTemplate('notice')">
                        <div class="template-card-title">📋 Official Memo / Notice</div>
                        <div class="template-card-desc">Corporate header, memo body, signature line</div>
                    </div>
                    <div class="template-card" onclick="loadCanvaTemplate('wifi')">
                        <div class="template-card-title">📶 Guest Wi-Fi Sign & QR</div>
                        <div class="template-card-desc">Network name, password, scan-to-connect box</div>
                    </div>
                    <div class="template-card" onclick="loadCanvaTemplate('certificate')">
                        <div class="template-card-title">🏆 Certificate of Recognition</div>
                        <div class="template-card-desc">Formal border, recipient line, gold seal</div>
                    </div>
                    <div class="template-card" onclick="trigger2x2IdGrid()">
                        <div class="template-card-title">👤 2×2 ID Photo Sheet</div>
                        <div class="template-card-desc">Tiles 6x framed passport photos with cut guides</div>
                    </div>
                </div>

                <!-- Section 2: Elements & Stamps -->
                <div class="drawer-section" id="drawer-elements">
                    <div class="drawer-title">Basic Shapes</div>
                    <div class="elements-grid">
                        <div class="element-btn" onclick="addCanvaShape('rect')"><span>▭</span><span>Rect</span></div>
                        <div class="element-btn" onclick="addCanvaShape('roundrect')"><span>▢</span><span>Round</span></div>
                        <div class="element-btn" onclick="addCanvaShape('circle')"><span>◯</span><span>Circle</span></div>
                        <div class="element-btn" onclick="addCanvaShape('triangle')"><span>△</span><span>Triangle</span></div>
                        <div class="element-btn" onclick="addCanvaShape('line')"><span>—</span><span>Line</span></div>
                        <div class="element-btn" onclick="addCanvaShape('star')"><span>★</span><span>Star</span></div>
                    </div>

                    <div class="drawer-title">Enterprise Stamps & Seals</div>
                    <button class="stamp-btn" data-stamp="APPROVED" style="color: var(--success); border-color: rgba(34,197,94,0.3);">✓ APPROVED</button>
                    <button class="stamp-btn" data-stamp="CONFIDENTIAL" style="color: var(--danger); border-color: rgba(239,68,68,0.3);">🔒 CONFIDENTIAL</button>
                    <button class="stamp-btn" data-stamp="PAID" style="color: var(--accent); border-color: rgba(59,130,246,0.3);">💵 PAID</button>
                    <button class="stamp-btn" data-stamp="URGENT" style="color: var(--warning); border-color: rgba(245,158,11,0.3);">⚠️ URGENT</button>
                    <button class="stamp-btn" data-stamp="DRAFT" style="color: #94a3b8;">📝 DRAFT</button>
                    <button class="stamp-btn" data-stamp="SIGNATURE">✍ Authorized Signature</button>
                </div>

                <!-- Section 3: Typography & Text -->
                <div class="drawer-section" id="drawer-text">
                    <div class="drawer-title">Add Text Elements</div>
                    <button class="btn-primary" id="btnAddHeading" style="margin-bottom: 8px; justify-content: flex-start; padding: 12px 14px; font-size: 16px; font-weight: 800;">
                        + Add a Heading
                    </button>
                    <button class="btn-secondary" id="btnAddSubheading" style="width: 100%; margin-bottom: 8px; justify-content: flex-start; padding: 10px 14px; font-size: 13px; font-weight: 600;">
                        + Add a Subheading
                    </button>
                    <button class="btn-secondary" id="btnAddBodyText" style="width: 100%; margin-bottom: 16px; justify-content: flex-start; padding: 8px 14px; font-size: 12px;">
                        + Add Body Text
                    </button>

                    <div class="drawer-title">Font Combinations</div>
                    <div class="template-card" onclick="addStyledText('MODERN DISPLAY', 'Impact', 32, '#09090b')">
                        <div style="font-family: Impact; font-size: 18px; letter-spacing: 0.05em;">MODERN BOLD</div>
                        <div class="template-card-desc">High-impact title font</div>
                    </div>
                    <div class="template-card" onclick="addStyledText('Classical Elegance', 'Georgia', 26, '#0f172a', true)">
                        <div style="font-family: Georgia; font-style: italic; font-size: 16px;">Classical Elegance</div>
                        <div class="template-card-desc">Serif document heading</div>
                    </div>
                    <div class="template-card" onclick="addStyledText('CODE // MONO', 'Courier New', 22, '#2563eb')">
                        <div style="font-family: 'Courier New'; font-size: 15px; font-weight: bold;">CODE // MONO</div>
                        <div class="template-card-desc">Monospaced tech header</div>
                    </div>
                </div>

                <!-- Section 4: Uploads -->
                <div class="drawer-section" id="drawer-uploads">
                    <div class="drawer-title">Upload Image or Photo</div>
                    <div class="dropzone" id="studioDropzone" style="padding: 20px 12px; margin-bottom: 16px;">
                        <input type="file" id="studioFileInput" accept="image/*">
                        <div style="font-size: 20px;">📷</div>
                        <div style="font-size: 12px; font-weight: 600; margin-top: 4px;">Click or drop image</div>
                    </div>

                    <div class="drawer-title">Uploaded Media</div>
                    <div id="studioMediaThumbnails" style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                        <!-- Dynamically populated -->
                    </div>
                </div>

                <!-- Section 5: QR Code Generator -->
                <div class="drawer-section" id="drawer-qrcode">
                    <div class="drawer-title">Live QR Code Generator</div>
                    <p style="font-size: 12px; color: var(--text-secondary); margin-bottom: 12px;">
                        Embed a vector QR code directly onto your flyer or sign.
                    </p>
                    <div class="form-group">
                        <label>URL or Wi-Fi Text</label>
                        <input type="text" id="qrTextInput" placeholder="https://mywebsite.com or text" value="https://printserver.local">
                    </div>
                    <button class="btn-primary" id="btnInsertQrCode">Generate & Insert QR</button>
                    <div id="qrHiddenContainer" style="display: none;"></div>
                </div>

                <!-- Section 6: Freehand Draw -->
                <div class="drawer-section" id="drawer-draw">
                    <div class="drawer-title">Freehand Pen Tool</div>
                    <div class="form-group">
                        <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; text-transform: none; font-size: 13px;">
                            <input type="checkbox" id="chkDrawMode">
                            <span>Enable Freehand Drawing Mode</span>
                        </label>
                    </div>
                    <div class="form-group">
                        <label>Brush Color</label>
                        <input type="color" id="drawColor" value="#000000" style="height: 36px; padding: 2px;">
                    </div>
                    <div class="form-group">
                        <label>Brush Size (<span id="brushSizeVal">4px</span>)</label>
                        <input type="range" id="drawWidth" min="1" max="30" value="4">
                    </div>
                </div>
            </div>

            <!-- Central Studio Viewport & Contextual Toolbar -->
            <div class="canva-main">
                <!-- Contextual Toolbar -->
                <div class="canva-topbar">
                    <div class="canva-topbar-tools">
                        <button class="canva-btn" id="btnStudioUndo" title="Undo (Ctrl+Z)">↶</button>
                        <button class="canva-btn" id="btnStudioRedo" title="Redo (Ctrl+Y)">↷</button>
                        <span style="width: 1px; height: 18px; background: var(--border); margin: 0 4px;"></span>

                        <!-- Dynamic Contextual Object Controls -->
                        <div id="contextTextControls" style="display: none; align-items: center; gap: 6px;">
                            <select id="ctxFontFamily" style="width: auto; padding: 4px 8px; font-size: 12px;">
                                <option value="Arial">Arial</option>
                                <option value="Georgia">Georgia</option>
                                <option value="Courier New">Courier</option>
                                <option value="Impact">Impact</option>
                                <option value="Times New Roman">Times New Roman</option>
                            </select>
                            <input type="number" id="ctxFontSize" value="24" min="8" max="140" style="width: 52px; padding: 4px 6px; font-size: 12px;">
                            <input type="color" id="ctxTextColor" value="#000000" style="width: 28px; height: 28px; padding: 0; border: none; cursor: pointer;" title="Text Color">
                            <button class="canva-btn" id="btnCtxBold" title="Bold"><b>B</b></button>
                            <button class="canva-btn" id="btnCtxItalic" title="Italic"><i>I</i></button>
                        </div>

                        <div id="contextShapeControls" style="display: none; align-items: center; gap: 6px;">
                            <label style="margin: 0; font-size: 11px;">Fill:</label>
                            <input type="color" id="ctxFillColor" value="#3b82f6" style="width: 28px; height: 28px; padding: 0; border: none; cursor: pointer;">
                            <label style="margin: 0 0 0 4px; font-size: 11px;">Border:</label>
                            <input type="color" id="ctxStrokeColor" value="#1d4ed8" style="width: 28px; height: 28px; padding: 0; border: none; cursor: pointer;">
                        </div>

                        <div id="contextImageControls" style="display: none; align-items: center; gap: 6px;">
                            <select id="ctxImageFilter" style="width: auto; padding: 4px 8px; font-size: 12px;">
                                <option value="none">Original Colors</option>
                                <option value="grayscale">Grayscale (B&W)</option>
                                <option value="sepia">Sepia Vintage</option>
                                <option value="contrast">High Contrast</option>
                                <option value="invert">Invert Colors</option>
                            </select>
                        </div>

                        <!-- Common Selection Actions -->
                        <div id="contextCommonControls" style="display: none; align-items: center; gap: 4px;">
                            <span style="width: 1px; height: 18px; background: var(--border); margin: 0 4px;"></span>
                            <button class="canva-btn" id="btnCtxLayerUp" title="Bring to Front">⇧ Front</button>
                            <button class="canva-btn" id="btnCtxLayerDown" title="Send to Back">⇩ Back</button>
                            <button class="canva-btn" id="btnCtxDuplicate" title="Duplicate">⧉</button>
                            <button class="canva-btn" id="btnCtxDelete" style="color: var(--danger);" title="Delete">🗑️</button>
                        </div>
                    </div>

                    <div style="display: flex; align-items: center; gap: 8px;">
                        <select id="studioDpi" style="width: auto; padding: 4px 8px; font-size: 12px;">
                            <option value="150">150 DPI (Draft)</option>
                            <option value="300" selected>300 DPI (High-Res Print)</option>
                            <option value="600">600 DPI (Ultra Crisp)</option>
                        </select>
                        <button class="canva-btn" id="btnStudioDownloadPng">🖼️ PNG</button>
                        <button class="canva-btn" id="btnStudioDownloadPdf">💾 PDF</button>
                        <button class="btn-primary" id="btnStudioDirectPrint" style="width: auto; padding: 6px 16px; font-size: 12px;">
                            🖨️ Print Direct
                        </button>
                    </div>
                </div>

                <!-- Canvas Workspace Viewport -->
                <div class="canva-viewport" id="canvaViewport">
                    <div class="canva-sheet-container" id="canvaSheetContainer">
                        <canvas id="canvaStudioCanvas"></canvas>
                    </div>
                </div>

                <!-- Bottom Status Bar -->
                <div class="canva-bottom-bar">
                    <div id="canvaDimensionsTag">A4 Document • 210 × 297 mm • 300 DPI (2480 × 3508 px)</div>
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <button class="canva-btn" id="btnStudioClearAll" style="padding: 2px 8px; font-size: 11px;">Clear All</button>
                        <div id="canvaZoomLabel">Zoom: 100%</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 3: SCAN -->
    <div class="tab-content" id="tab-scan">
        <div class="grid-layout">
            <div class="preview-desk">
                <div id="scanPreviewContainer" style="display:none; text-align:center;">
                    <img id="scanPreviewImg" style="max-width: 100%; max-height: 520px; box-shadow: var(--paper-shadow); border-radius: 4px;">
                </div>
                <div id="scanEmptyState" style="text-align: center; color: var(--text-tertiary);">
                    <div style="font-size: 32px; margin-bottom: 8px;">📠</div>
                    <div style="font-weight: 500;">Flatbed Scanner Ready</div>
                    <div style="font-size: 12px;">Scans can be saved directly to \\\\printserver.local\\Public\\Scans</div>
                </div>
            </div>

            <div class="card">
                <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 16px;">Digitize Document</h3>
                <div class="form-group">
                    <label>Resolution</label>
                    <select id="scanResolution">
                        <option value="75">75 DPI (Screen Draft)</option>
                        <option value="150">150 DPI (Compact Document)</option>
                        <option value="300" selected>300 DPI (Standard Archive)</option>
                        <option value="600">600 DPI (High-Res Photo)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Color Mode</label>
                    <select id="scanMode">
                        <option value="Color" selected>Color</option>
                        <option value="Gray">Grayscale</option>
                        <option value="Lineart">Black & White (Lineart)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Output Format</label>
                    <select id="scanFormat">
                        <option value="pdf" selected>PDF Document</option>
                        <option value="png">PNG Image</option>
                        <option value="jpeg">JPEG Image</option>
                    </select>
                </div>
                <div class="form-group">
                    <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; text-transform: none; font-size: 13px;">
                        <input type="checkbox" id="scanSaveSamba" checked>
                        <span>Save to Samba Share (\\\\printserver.local\\Public\\Scans)</span>
                    </label>
                </div>
                <button class="btn-primary" id="btnTriggerScan">📠 Start Scanning</button>
            </div>
        </div>
    </div>

    <!-- TAB 4: PIN RELEASE -->
    <div class="tab-content" id="tab-pin">
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <div>
                    <h2 style="font-size: 16px; font-weight: 700;">Confidential Print Queue</h2>
                    <p style="font-size: 12px; color: var(--text-secondary);">Jobs held securely in memory until unlocked with your 4-digit PIN</p>
                </div>
                <button class="btn-secondary" id="btnRefreshPinList">↻ Refresh Queue</button>
            </div>

            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Time Held</th>
                            <th>Document Title</th>
                            <th>Pages</th>
                            <th>Copies</th>
                            <th>Color Mode</th>
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

    <!-- TAB 5: HISTORY & ACCOUNTING -->
    <div class="tab-content" id="tab-history">
        <div class="card" style="margin-bottom: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <div>
                    <h2 style="font-size: 16px; font-weight: 700;">Print Accounting & Audit Ledger</h2>
                    <p style="font-size: 12px; color: var(--text-secondary);">Complete log of all print and released jobs</p>
                </div>
                <a href="/api/reports/export.csv" class="btn-secondary" download>📥 Export CSV Ledger</a>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px;">
                <div class="card" style="background: var(--surface-elevated); padding: 14px;">
                    <div style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase;">Total Jobs</div>
                    <div style="font-size: 24px; font-weight: 700;" id="statTotalJobs">0</div>
                </div>
                <div class="card" style="background: var(--surface-elevated); padding: 14px;">
                    <div style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase;">Total Pages Printed</div>
                    <div style="font-size: 24px; font-weight: 700;" id="statTotalPages">0</div>
                </div>
                <div class="card" style="background: var(--surface-elevated); padding: 14px;">
                    <div style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase;">Total Billed Cost</div>
                    <div style="font-size: 24px; font-weight: 700; color: var(--accent);" id="statTotalRevenue">₱0.00</div>
                </div>
            </div>

            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Date / Time</th>
                            <th>Job Title</th>
                            <th>Printer</th>
                            <th>Pages</th>
                            <th>Copies</th>
                            <th>Mode</th>
                            <th>Cost</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody id="historyTableBody">
                        <tr><td colspan="8" style="text-align: center; color: var(--text-tertiary);">Loading audit records...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- TAB 6: SETTINGS & APPLIANCE ADMIN -->
    <div class="tab-content" id="tab-settings">
        <div class="grid-layout">
            <div class="card">
                <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 16px;">Appliance System Telemetry</h3>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px;">
                    <div class="cost-card">
                        <div>
                            <div class="cost-title">CPU Load</div>
                            <div id="telemetryCpu" style="font-weight: 600;">0.00</div>
                        </div>
                    </div>
                    <div class="cost-card">
                        <div>
                            <div class="cost-title">RAM Usage</div>
                            <div id="telemetryRam" style="font-weight: 600;">0 / 0 MB</div>
                        </div>
                    </div>
                    <div class="cost-card">
                        <div>
                            <div class="cost-title">S905X Temperature</div>
                            <div id="telemetryTemp" style="font-weight: 600;">0°C</div>
                        </div>
                    </div>
                    <div class="cost-card">
                        <div>
                            <div class="cost-title">Storage Space</div>
                            <div id="telemetryDisk" style="font-weight: 600;">0% Free</div>
                        </div>
                    </div>
                </div>
                <div style="font-size: 12px; color: var(--text-secondary);" id="telemetryUptime">Uptime: --</div>
            </div>

            <div class="card">
                <h3 style="font-size: 16px; font-weight: 600; margin-bottom: 12px;">Admin Management</h3>
                <div id="adminLockedState">
                    <p style="font-size: 12px; color: var(--text-secondary); margin-bottom: 12px;">
                        Appliance controls are password-protected to prevent unauthorized access.
                    </p>
                    <div class="form-group">
                        <label>Admin Password</label>
                        <input type="password" id="adminPassInput" placeholder="Enter password (default: admin)">
                    </div>
                    <button class="btn-primary" id="btnUnlockAdmin">Unlock Appliance</button>
                </div>

                <div id="adminUnlockedState" style="display: none;">
                    <div style="font-size: 12px; color: var(--success); font-weight: 600; margin-bottom: 12px;">
                        ✓ Administrator Mode Active
                    </div>

                    <div class="form-group">
                        <label>Rate Card Configuration</label>
                        <div style="display: flex; gap: 8px; margin-bottom: 8px;">
                            <input type="text" id="adminRateBw" placeholder="B&W Page (₱)">
                            <input type="text" id="adminRateColor" placeholder="Color Page (₱)">
                        </div>
                        <button class="btn-secondary" id="btnSaveRates" style="width: 100%;">Save Rate Card</button>
                    </div>

                    <div class="form-group" style="padding-top: 12px; border-top: 1px solid var(--border);">
                        <label>Hardware Power & Spool Actions</label>
                        <div style="display: flex; flex-direction: column; gap: 8px;">
                            <button class="btn-secondary" id="btnAdminRestartCups">🔄 Restart CUPS Spooler</button>
                            <button class="btn-secondary" id="btnAdminPurgeCache">🧹 Purge Temporary Cache</button>
                            <button class="btn-secondary" id="btnAdminReboot" style="color: var(--warning);">⚡ Reboot Appliance</button>
                            <button class="btn-secondary" id="btnAdminShutdown" style="color: var(--danger);">🛑 Shut Down Appliance</button>
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
        <h3 style="font-size: 16px; font-weight: 700; margin-bottom: 8px;">Release Confidential Job</h3>
        <p style="font-size: 12px; color: var(--text-secondary); margin-bottom: 16px;" id="pinModalJobTitle">Document</p>
        <div class="form-group">
            <label>Enter 4-Digit Release PIN</label>
            <input type="password" id="pinModalCode" maxlength="4" placeholder="••••" style="text-align: center; font-size: 24px; letter-spacing: 0.4em; font-family: var(--font-mono);">
        </div>
        <div style="display: flex; gap: 10px;">
            <button class="btn-secondary" id="btnCancelPinModal" style="flex:1;">Cancel</button>
            <button class="btn-primary" id="btnConfirmReleasePin" style="flex:1;">Release Print</button>
        </div>
    </div>
</div>

<div class="toast" id="toastNotification">
    <span id="toastMessage"></span>
</div>

<script>
    pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

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
        const next = cur === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
    });

    // Tab Navigation
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            const target = document.getElementById('tab-' + btn.getAttribute('data-tab'));
            if (target) target.classList.add('active');

            if (btn.getAttribute('data-tab') === 'studio') {
                if (!canvaStudioCanvas) initCanvaStudio();
            } else if (btn.getAttribute('data-tab') === 'pin') {
                loadPinList();
            } else if (btn.getAttribute('data-tab') === 'history') {
                loadHistory();
            } else if (btn.getAttribute('data-tab') === 'settings') {
                loadTelemetry();
            }
        });
    });

    // Printer Discovery & Population
    async function loadPrinters() {
        try {
            const res = await fetch('/api/printers');
            const data = await res.json();
            const select = document.getElementById('headerPrinterSelect');
            select.innerHTML = '';
            if (data.printers && data.printers.length > 0) {
                data.printers.forEach(p => {
                    const opt = document.createElement('option');
                    opt.value = p.name;
                    opt.textContent = p.name + (p.is_default ? ' (Default)' : '');
                    if (p.is_default) opt.selected = true;
                    select.appendChild(opt);
                });
                selectedPrinter = select.value;
                document.getElementById('headerStatusDot').className = 'status-dot ' + (data.printers[0].status || 'idle');
            } else {
                select.innerHTML = '<option value="">No printers detected</option>';
            }
        } catch (e) {
            console.error(e);
        }
    }
    document.getElementById('headerPrinterSelect').addEventListener('change', (e) => {
        selectedPrinter = e.target.value;
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

        const maxDeskWidth = 460;
        const maxDeskHeight = 560;
        let paperPhysW = isLandscape ? spec.height : spec.width;
        let paperPhysH = isLandscape ? spec.width : spec.height;
        let paperRatio = paperPhysH / paperPhysW;

        let sheetW, sheetH;
        if (maxDeskWidth * paperRatio <= maxDeskHeight) {
            sheetW = maxDeskWidth;
            sheetH = Math.round(maxDeskWidth * paperRatio);
        } else {
            sheetH = maxDeskHeight;
            sheetW = Math.round(maxDeskHeight / paperRatio);
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
        document.getElementById('costDetails').textContent = `${inspectedPageCount} page(s) × ${copies} copy(ies) @ ₱${rate.toFixed(2)}/pg`;
        document.getElementById('costTotal').textContent = `₱${total.toFixed(2)}`;
    }

    ['printPageSize', 'printOrientation', 'printMarginMode', 'printColorModel', 'printScaling', 'printCopies'].forEach(id => {
        document.getElementById(id).addEventListener('change', updatePreviewLayout);
    });
    ['mTop', 'mBottom', 'mLeft', 'mRight'].forEach(id => {
        document.getElementById(id).addEventListener('input', updatePreviewLayout);
    });

    document.getElementById('printMarginMode').addEventListener('change', (e) => {
        document.getElementById('customMarginBox').style.display = (e.target.value === 'custom') ? 'block' : 'none';
        updatePreviewLayout();
    });

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

    document.getElementById('btnClearPreview').addEventListener('click', () => {
        fileInput.value = '';
        currentPdf = null;
        inspectedPageCount = 1;
        document.getElementById('printFileName').textContent = 'Click or drop file here';
        document.getElementById('previewTopToolbar').style.display = 'none';
        document.getElementById('previewEmptyState').style.display = 'block';
        document.getElementById('previewDimensionsTag').style.display = 'none';
        document.getElementById('pdfCanvas').style.display = 'none';
        document.getElementById('imgPreview').style.display = 'none';
        document.getElementById('txtPreview').style.display = 'none';
        document.getElementById('costDetails').textContent = '0 pages';
        document.getElementById('costTotal').textContent = '₱0.00';
    });

    document.getElementById('chkPinHold').addEventListener('change', (e) => {
        document.getElementById('pinInputWrapper').style.display = e.target.checked ? 'block' : 'none';
        document.getElementById('btnSubmitPrint').textContent = e.target.checked ? '🔒 Hold in Confidential Queue' : '🖨️ Send to Printer';
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

    // ========================================================
    // FULL-FLEDGED CANVA CLONE ENGINE (100% BROWSER OFFLOADED)
    // ========================================================
    let canvaStudioCanvas = null;
    let studioHistory = [];
    let historyIndex = -1;
    let isHistoryUpdating = false;

    function initCanvaStudio() {
        canvaStudioCanvas = new fabric.Canvas('canvaStudioCanvas', {
            width: 480,
            height: 680,
            backgroundColor: '#ffffff',
            preserveObjectStacking: true
        });

        // Initialize dock tab switching
        document.querySelectorAll('.dock-item').forEach(item => {
            item.addEventListener('click', () => {
                document.querySelectorAll('.dock-item').forEach(d => d.classList.remove('active'));
                document.querySelectorAll('.drawer-section').forEach(s => s.classList.remove('active'));
                item.classList.add('active');
                const target = document.getElementById('drawer-' + item.getAttribute('data-drawer'));
                if (target) target.classList.add('active');
            });
        });

        // Selection listeners for contextual toolbar
        canvaStudioCanvas.on('selection:created', updateContextualBar);
        canvaStudioCanvas.on('selection:updated', updateContextualBar);
        canvaStudioCanvas.on('selection:cleared', clearContextualBar);

        // History recording
        canvaStudioCanvas.on('object:added', () => recordCanvaHistory());
        canvaStudioCanvas.on('object:modified', () => recordCanvaHistory());
        canvaStudioCanvas.on('object:removed', () => recordCanvaHistory());

        // Keyboard shortcuts
        window.addEventListener('keydown', (e) => {
            if (!canvaStudioCanvas) return;
            const active = canvaStudioCanvas.getActiveObject();
            if (active && (e.key === 'Delete' || e.key === 'Backspace')) {
                if (active.isEditing) return;
                canvaStudioCanvas.remove(active);
                e.preventDefault();
            } else if (e.ctrlKey && e.key === 'z') {
                undoCanva();
                e.preventDefault();
            } else if (e.ctrlKey && e.key === 'y') {
                redoCanva();
                e.preventDefault();
            } else if (e.ctrlKey && e.key === 'd' && active) {
                cloneActiveCanvaObject();
                e.preventDefault();
            }
        });

        setCanvaCanvasPreset('a4');
        recordCanvaHistory();
    }

    function recordCanvaHistory() {
        if (isHistoryUpdating || !canvaStudioCanvas) return;
        const json = JSON.stringify(canvaStudioCanvas.toJSON());
        if (historyIndex < studioHistory.length - 1) {
            studioHistory = studioHistory.slice(0, historyIndex + 1);
        }
        studioHistory.push(json);
        if (studioHistory.length > 25) studioHistory.shift();
        historyIndex = studioHistory.length - 1;
    }

    function undoCanva() {
        if (historyIndex > 0) {
            historyIndex--;
            isHistoryUpdating = true;
            canvaStudioCanvas.loadFromJSON(studioHistory[historyIndex], () => {
                canvaStudioCanvas.renderAll();
                isHistoryUpdating = false;
                clearContextualBar();
            });
        }
    }

    function redoCanva() {
        if (historyIndex < studioHistory.length - 1) {
            historyIndex++;
            isHistoryUpdating = true;
            canvaStudioCanvas.loadFromJSON(studioHistory[historyIndex], () => {
                canvaStudioCanvas.renderAll();
                isHistoryUpdating = false;
                clearContextualBar();
            });
        }
    }

    document.getElementById('btnStudioUndo').addEventListener('click', undoCanva);
    document.getElementById('btnStudioRedo').addEventListener('click', redoCanva);

    function updateContextualBar() {
        const obj = canvaStudioCanvas.getActiveObject();
        if (!obj) { clearContextualBar(); return; }

        document.getElementById('contextCommonControls').style.display = 'flex';
        document.getElementById('contextTextControls').style.display = (obj.type === 'i-text' || obj.type === 'text') ? 'flex' : 'none';
        document.getElementById('contextShapeControls').style.display = ['rect', 'circle', 'triangle', 'polygon'].includes(obj.type) ? 'flex' : 'none';
        document.getElementById('contextImageControls').style.display = (obj.type === 'image') ? 'flex' : 'none';

        if (obj.type === 'i-text' || obj.type === 'text') {
            document.getElementById('ctxFontFamily').value = obj.fontFamily || 'Arial';
            document.getElementById('ctxFontSize').value = obj.fontSize || 24;
            document.getElementById('ctxTextColor').value = obj.fill || '#000000';
        } else if (['rect', 'circle', 'triangle', 'polygon'].includes(obj.type)) {
            document.getElementById('ctxFillColor').value = obj.fill || '#3b82f6';
            document.getElementById('ctxStrokeColor').value = obj.stroke || '#1d4ed8';
        }
    }

    function clearContextualBar() {
        document.getElementById('contextTextControls').style.display = 'none';
        document.getElementById('contextShapeControls').style.display = 'none';
        document.getElementById('contextImageControls').style.display = 'none';
        document.getElementById('contextCommonControls').style.display = 'none';
    }

    // Context toolbar interaction events
    document.getElementById('ctxFontFamily').addEventListener('change', (e) => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj && (obj.type === 'i-text' || obj.type === 'text')) {
            obj.set('fontFamily', e.target.value);
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        }
    });
    document.getElementById('ctxFontSize').addEventListener('input', (e) => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj && (obj.type === 'i-text' || obj.type === 'text')) {
            obj.set('fontSize', parseInt(e.target.value) || 20);
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        }
    });
    document.getElementById('ctxTextColor').addEventListener('input', (e) => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj) {
            obj.set('fill', e.target.value);
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        }
    });
    document.getElementById('btnCtxBold').addEventListener('click', () => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj && (obj.type === 'i-text' || obj.type === 'text')) {
            obj.set('fontWeight', obj.fontWeight === 'bold' ? 'normal' : 'bold');
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        }
    });
    document.getElementById('btnCtxItalic').addEventListener('click', () => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj && (obj.type === 'i-text' || obj.type === 'text')) {
            obj.set('fontStyle', obj.fontStyle === 'italic' ? 'normal' : 'italic');
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        }
    });
    document.getElementById('ctxFillColor').addEventListener('input', (e) => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj) {
            obj.set('fill', e.target.value);
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        }
    });
    document.getElementById('ctxStrokeColor').addEventListener('input', (e) => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj) {
            obj.set('stroke', e.target.value);
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        }
    });

    document.getElementById('ctxImageFilter').addEventListener('change', (e) => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj && obj.type === 'image') {
            obj.filters = [];
            const val = e.target.value;
            if (val === 'grayscale') obj.filters.push(new fabric.Image.filters.Grayscale());
            else if (val === 'sepia') obj.filters.push(new fabric.Image.filters.Sepia());
            else if (val === 'contrast') obj.filters.push(new fabric.Image.filters.Contrast({ contrast: 0.3 }));
            else if (val === 'invert') obj.filters.push(new fabric.Image.filters.Invert());
            obj.applyFilters();
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        }
    });

    document.getElementById('btnCtxLayerUp').addEventListener('click', () => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj) { obj.bringToFront(); canvaStudioCanvas.renderAll(); recordCanvaHistory(); }
    });
    document.getElementById('btnCtxLayerDown').addEventListener('click', () => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj) { obj.sendToBack(); canvaStudioCanvas.renderAll(); recordCanvaHistory(); }
    });
    document.getElementById('btnCtxDuplicate').addEventListener('click', cloneActiveCanvaObject);
    document.getElementById('btnCtxDelete').addEventListener('click', () => {
        const obj = canvaStudioCanvas.getActiveObject();
        if (obj) { canvaStudioCanvas.remove(obj); recordCanvaHistory(); }
    });

    function cloneActiveCanvaObject() {
        const obj = canvaStudioCanvas.getActiveObject();
        if (!obj) return;
        obj.clone((cloned) => {
            cloned.set({ left: obj.left + 20, top: obj.top + 20 });
            canvaStudioCanvas.add(cloned);
            canvaStudioCanvas.setActiveObject(cloned);
            canvaStudioCanvas.renderAll();
            recordCanvaHistory();
        });
    }

    // Canvas Presets & Dimensions
    function setCanvaCanvasPreset(preset) {
        if (!canvaStudioCanvas) return;
        let w = 480, h = 680, desc = 'A4 Document • 210 × 297 mm';
        if (preset === 'letter') { w = 480; h = 620; desc = 'US Letter • 8.5 × 11 in'; }
        else if (preset === 'photo46') { w = 400; h = 600; desc = '4 × 6 in Photo Card'; }
        else if (preset === 'flyer') { w = 420; h = 594; desc = 'A5 Flyer / Notice'; }
        else if (preset === 'square') { w = 500; h = 500; desc = 'Square 1:1 • 200 × 200 mm'; }

        canvaStudioCanvas.setWidth(w);
        canvaStudioCanvas.setHeight(h);
        canvaStudioCanvas.renderAll();
        document.getElementById('canvaDimensionsTag').textContent = `${desc} • 300 DPI`;
    }

    document.getElementById('studioPreset').addEventListener('change', (e) => {
        setCanvaCanvasPreset(e.target.value);
    });

    // Typography additions
    document.getElementById('btnAddHeading').addEventListener('click', () => {
        addStyledText('Heading Title', 'Arial', 32, '#09090b', false, true);
    });
    document.getElementById('btnAddSubheading').addEventListener('click', () => {
        addStyledText('Subheading Text', 'Arial', 20, '#475569');
    });
    document.getElementById('btnAddBodyText').addEventListener('click', () => {
        addStyledText('Write your paragraph or body text here. Double-click to edit.', 'Arial', 14, '#18181b');
    });

    function addStyledText(text, font='Arial', size=24, color='#000000', italic=false, bold=false) {
        if (!canvaStudioCanvas) return;
        const t = new fabric.IText(text, {
            left: 60,
            top: 100,
            fontFamily: font,
            fontSize: size,
            fill: color,
            fontStyle: italic ? 'italic' : 'normal',
            fontWeight: bold ? 'bold' : 'normal'
        });
        canvaStudioCanvas.add(t);
        canvaStudioCanvas.setActiveObject(t);
        canvaStudioCanvas.renderAll();
    }

    // Elements & Shapes addition
    window.addCanvaShape = function(type) {
        if (!canvaStudioCanvas) return;
        let shape;
        if (type === 'rect') {
            shape = new fabric.Rect({ left: 80, top: 80, width: 140, height: 90, fill: '#3b82f6', stroke: '#1d4ed8', strokeWidth: 2, rx: 0 });
        } else if (type === 'roundrect') {
            shape = new fabric.Rect({ left: 80, top: 80, width: 140, height: 90, fill: '#10b981', stroke: '#047857', strokeWidth: 2, rx: 12, ry: 12 });
        } else if (type === 'circle') {
            shape = new fabric.Circle({ left: 80, top: 80, radius: 50, fill: '#f59e0b', stroke: '#b45309', strokeWidth: 2 });
        } else if (type === 'triangle') {
            shape = new fabric.Triangle({ left: 80, top: 80, width: 100, height: 90, fill: '#8b5cf6', stroke: '#6d28d9', strokeWidth: 2 });
        } else if (type === 'line') {
            shape = new fabric.Line([50, 100, 350, 100], { stroke: '#09090b', strokeWidth: 3 });
        } else if (type === 'star') {
            shape = new fabric.Polygon([
                {x: 50, y: 0}, {x: 65, y: 35}, {x: 100, y: 35}, {x: 72, y: 57},
                {x: 82, y: 91}, {x: 50, y: 70}, {x: 18, y: 91}, {x: 28, y: 57},
                {x: 0, y: 35}, {x: 35, y: 35}
            ], { left: 80, top: 80, fill: '#eab308', stroke: '#ca8a04', strokeWidth: 2 });
        }
        if (shape) {
            canvaStudioCanvas.add(shape);
            canvaStudioCanvas.setActiveObject(shape);
            canvaStudioCanvas.renderAll();
        }
    };

    // Stamps
    document.querySelectorAll('.stamp-btn[data-stamp]').forEach(btn => {
        btn.addEventListener('click', () => {
            if (!canvaStudioCanvas) return;
            const type = btn.getAttribute('data-stamp');
            let color = '#22c55e';
            if (type === 'CONFIDENTIAL') color = '#ef4444';
            if (type === 'PAID') color = '#3b82f6';
            if (type === 'URGENT') color = '#f59e0b';
            if (type === 'DRAFT') color = '#64748b';
            if (type === 'SIGNATURE') {
                const line = new fabric.Line([0, 0, 220, 0], { stroke: '#000000', strokeWidth: 2 });
                const lbl = new fabric.Text('Authorized Signature', { left: 0, top: 6, fontSize: 12, fill: '#64748b' });
                const grp = new fabric.Group([line, lbl], { left: 80, top: 200 });
                canvaStudioCanvas.add(grp);
                canvaStudioCanvas.setActiveObject(grp);
                canvaStudioCanvas.renderAll();
                return;
            }

            const stampRect = new fabric.Rect({ width: 180, height: 48, fill: 'transparent', stroke: color, strokeWidth: 4, rx: 6, ry: 6 });
            const stampText = new fabric.Text(type, { fontSize: 22, fontWeight: 'bold', fill: color, originX: 'center', originY: 'center', left: 90, top: 24 });
            const stampGroup = new fabric.Group([stampRect, stampText], { left: 100, top: 120, angle: -8 });
            canvaStudioCanvas.add(stampGroup);
            canvaStudioCanvas.setActiveObject(stampGroup);
            canvaStudioCanvas.renderAll();
        });
    });

    // Freehand Drawing Tool
    document.getElementById('chkDrawMode').addEventListener('change', (e) => {
        if (!canvaStudioCanvas) return;
        canvaStudioCanvas.isDrawingMode = e.target.checked;
        if (e.target.checked) {
            canvaStudioCanvas.freeDrawingBrush.color = document.getElementById('drawColor').value;
            canvaStudioCanvas.freeDrawingBrush.width = parseInt(document.getElementById('drawWidth').value);
        }
    });
    document.getElementById('drawColor').addEventListener('input', (e) => {
        if (canvaStudioCanvas && canvaStudioCanvas.freeDrawingBrush) canvaStudioCanvas.freeDrawingBrush.color = e.target.value;
    });
    document.getElementById('drawWidth').addEventListener('input', (e) => {
        document.getElementById('brushSizeVal').textContent = e.target.value + 'px';
        if (canvaStudioCanvas && canvaStudioCanvas.freeDrawingBrush) canvaStudioCanvas.freeDrawingBrush.width = parseInt(e.target.value);
    });

    // Image Uploads into Canva Studio
    const studioFileInput = document.getElementById('studioFileInput');
    studioFileInput.addEventListener('change', (e) => {
        if (!e.target.files.length || !canvaStudioCanvas) return;
        const file = e.target.files[0];
        const reader = new FileReader();
        reader.onload = function(ev) {
            fabric.Image.fromURL(ev.target.result, (img) => {
                img.scaleToWidth(260);
                canvaStudioCanvas.add(img);
                canvaStudioCanvas.centerObject(img);
                canvaStudioCanvas.setActiveObject(img);
                canvaStudioCanvas.renderAll();

                // Add to uploaded media gallery
                const thumb = document.createElement('img');
                thumb.src = ev.target.result;
                thumb.style.width = '100%';
                thumb.style.height = '70px';
                thumb.style.objectFit = 'cover';
                thumb.style.borderRadius = '4px';
                thumb.style.cursor = 'pointer';
                thumb.onclick = () => {
                    fabric.Image.fromURL(ev.target.result, (newImg) => {
                        newImg.scaleToWidth(240);
                        canvaStudioCanvas.add(newImg);
                        canvaStudioCanvas.centerObject(newImg);
                        canvaStudioCanvas.setActiveObject(newImg);
                    });
                };
                document.getElementById('studioMediaThumbnails').appendChild(thumb);
            });
        };
        reader.readAsDataURL(file);
    });

    // Live QR Code Generator
    document.getElementById('btnInsertQrCode').addEventListener('click', () => {
        const text = document.getElementById('qrTextInput').value.trim();
        if (!text) { showToast('Enter text or URL for the QR code', 'error'); return; }

        const container = document.getElementById('qrHiddenContainer');
        container.innerHTML = '';
        new QRCode(container, {
            text: text,
            width: 256,
            height: 256,
            correctLevel: QRCode.CorrectLevel.H
        });

        setTimeout(() => {
            const img = container.querySelector('img');
            const canvas = container.querySelector('canvas');
            const dataUrl = img ? img.src : (canvas ? canvas.toDataURL() : null);
            if (dataUrl) {
                fabric.Image.fromURL(dataUrl, (qrImg) => {
                    qrImg.scaleToWidth(130);
                    canvaStudioCanvas.add(qrImg);
                    canvaStudioCanvas.centerObject(qrImg);
                    canvaStudioCanvas.setActiveObject(qrImg);
                    canvaStudioCanvas.renderAll();
                    showToast('QR Code added to canvas!', 'success');
                });
            }
        }, 80);
    });

    // 1-Click Canva Templates
    window.loadCanvaTemplate = function(type) {
        if (!canvaStudioCanvas) return;
        canvaStudioCanvas.clear();
        setCanvaCanvasPreset('a4');

        if (type === 'sale') {
            const bg = new fabric.Rect({ left: 0, top: 0, width: 480, height: 680, fill: '#fffdfa', selectable: false });
            const banner = new fabric.Rect({ left: 0, top: 0, width: 480, height: 110, fill: '#dc2626', selectable: false });
            const title = new fabric.Text('SPECIAL PROMO', { left: 240, top: 38, originX: 'center', fontSize: 32, fontWeight: 'bold', fill: '#ffffff', fontFamily: 'Impact' });
            const bigOff = new fabric.Text('50% OFF', { left: 240, top: 220, originX: 'center', fontSize: 72, fontWeight: 'bold', fill: '#dc2626', fontFamily: 'Impact' });
            const sub = new fabric.Text('ALL ITEMS ON SALE THIS WEEKEND', { left: 240, top: 310, originX: 'center', fontSize: 16, fontWeight: 'bold', fill: '#1e293b' });
            const desc = new fabric.Text('Show this flyer in-store or use voucher code online at checkout.', { left: 240, top: 370, originX: 'center', fontSize: 13, fill: '#64748b' });
            const footer = new fabric.Rect({ left: 0, top: 620, width: 480, height: 60, fill: '#1e293b', selectable: false });
            const footText = new fabric.Text('123 Commercial Center • Open Daily 8AM - 8PM', { left: 240, top: 638, originX: 'center', fontSize: 12, fill: '#f8fafc' });

            canvaStudioCanvas.add(bg, banner, title, bigOff, sub, desc, footer, footText);
        } else if (type === 'notice') {
            const topBar = new fabric.Rect({ left: 40, top: 40, width: 400, height: 4, fill: '#2563eb' });
            const title = new fabric.Text('OFFICIAL NOTICE', { left: 40, top: 56, fontSize: 26, fontWeight: 'bold', fill: '#0f172a', fontFamily: 'Arial' });
            const date = new fabric.Text(`Date: ${new Date().toLocaleDateString()}`, { left: 40, top: 96, fontSize: 12, fill: '#64748b' });
            const sub = new fabric.Text('SUBJECT: System Upgrade & Maintenance Window', { left: 40, top: 130, fontSize: 14, fontWeight: 'bold', fill: '#1e293b' });
            const body = new fabric.IText('Please be advised that scheduled maintenance will take place this Sunday.\nAll server services will operate in read-only mode during this interval.\n\nFor questions or support, please contact your systems administrator.', { left: 40, top: 180, fontSize: 13, fill: '#334155', lineHeight: 1.5 });
            const line = new fabric.Line([40, 520, 240, 520], { stroke: '#09090b', strokeWidth: 1.5 });
            const sign = new fabric.Text('Authorized Signature', { left: 40, top: 526, fontSize: 11, fill: '#64748b' });

            canvaStudioCanvas.add(topBar, title, date, sub, body, line, sign);
        } else if (type === 'wifi') {
            const title = new fabric.Text('GUEST WI-FI', { left: 240, top: 60, originX: 'center', fontSize: 36, fontWeight: 'bold', fill: '#2563eb', fontFamily: 'Arial' });
            const desc = new fabric.Text('Connect your device to our complimentary network', { left: 240, top: 110, originX: 'center', fontSize: 13, fill: '#64748b' });
            const box = new fabric.Rect({ left: 60, top: 150, width: 360, height: 160, fill: '#f1f5f9', stroke: '#cbd5e1', strokeWidth: 2, rx: 8 });
            const ssid = new fabric.Text('Network: Free_Guest_WiFi', { left: 90, top: 190, fontSize: 18, fontWeight: 'bold', fill: '#0f172a' });
            const pass = new fabric.Text('Password: WelcomeGuest2026', { left: 90, top: 240, fontSize: 18, fontWeight: 'bold', fill: '#2563eb' });
            const scanLbl = new fabric.Text('Or point your camera to scan & connect:', { left: 240, top: 350, originX: 'center', fontSize: 13, fill: '#475569' });

            canvaStudioCanvas.add(title, desc, box, ssid, pass, scanLbl);
            // Insert QR
            document.getElementById('qrTextInput').value = 'WIFI:S:Free_Guest_WiFi;T:WPA;P:WelcomeGuest2026;;';
            document.getElementById('btnInsertQrCode').click();
        } else if (type === 'certificate') {
            const borderOut = new fabric.Rect({ left: 20, top: 20, width: 440, height: 640, fill: 'transparent', stroke: '#ca8a04', strokeWidth: 4 });
            const borderIn = new fabric.Rect({ left: 26, top: 26, width: 428, height: 628, fill: 'transparent', stroke: '#ca8a04', strokeWidth: 1 });
            const title = new fabric.Text('CERTIFICATE', { left: 240, top: 80, originX: 'center', fontSize: 34, fontWeight: 'bold', fill: '#854d0e', fontFamily: 'Georgia' });
            const sub = new fabric.Text('OF ACHIEVEMENT', { left: 240, top: 126, originX: 'center', fontSize: 16, letterSpacing: 0.1, fill: '#a16207', fontFamily: 'Georgia' });
            const pres = new fabric.Text('This certificate is proudly awarded to', { left: 240, top: 200, originX: 'center', fontSize: 14, fill: '#475569', fontStyle: 'italic', fontFamily: 'Georgia' });
            const name = new fabric.IText('[Recipient Full Name]', { left: 240, top: 240, originX: 'center', fontSize: 26, fontWeight: 'bold', fill: '#0f172a', fontFamily: 'Georgia' });
            const reason = new fabric.Text('For outstanding dedication, performance, and excellence.', { left: 240, top: 310, originX: 'center', fontSize: 13, fill: '#475569', fontFamily: 'Georgia' });
            const line = new fabric.Line([60, 520, 200, 520], { stroke: '#09090b', strokeWidth: 1.5 });
            const signLbl = new fabric.Text('Program Director', { left: 130, top: 526, originX: 'center', fontSize: 11, fill: '#64748b' });

            canvaStudioCanvas.add(borderOut, borderIn, title, sub, pres, name, reason, line, signLbl);
        }
        canvaStudioCanvas.renderAll();
        recordCanvaHistory();
    };

    // 2x2 ID Photo Grid Maker
    window.trigger2x2IdGrid = function() {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = 'image/*';
        input.onchange = (e) => {
            if (!e.target.files.length) return;
            const r = new FileReader();
            r.onload = function(ev) {
                fabric.Image.fromURL(ev.target.result, (img) => {
                    canvaStudioCanvas.clear();
                    setCanvaCanvasPreset('a4');
                    const tileW = 140;
                    const tileH = 140;
                    const cols = 2;
                    const rows = 3;
                    const startX = 60;
                    const startY = 60;
                    const gapX = 40;
                    const gapY = 40;

                    for (let row = 0; row < rows; row++) {
                        for (let col = 0; col < cols; col++) {
                            img.clone((cloned) => {
                                cloned.scaleToWidth(tileW);
                                cloned.scaleToHeight(tileH);
                                cloned.set({
                                    left: startX + col * (tileW + gapX),
                                    top: startY + row * (tileH + gapY),
                                    stroke: '#94a3b8',
                                    strokeWidth: 1
                                });
                                canvaStudioCanvas.add(cloned);
                            });
                        }
                    }
                    showToast('Generated 2×2 ID photo grid with cut guides', 'success');
                    recordCanvaHistory();
                });
            };
            r.readAsDataURL(e.target.files[0]);
        };
        input.click();
    };

    // Export & Print from Canva Studio
    document.getElementById('btnStudioDirectPrint').addEventListener('click', () => {
        if (!canvaStudioCanvas) return;
        const dpi = parseInt(document.getElementById('studioDpi').value);
        const multiplier = dpi / 72;
        showToast(`Compiling ${dpi} DPI high-res canvas in browser...`, 'info');

        const dataUrl = canvaStudioCanvas.toDataURL({ format: 'png', multiplier: multiplier });
        fetch(dataUrl)
            .then(res => res.blob())
            .then(blob => {
                const formData = new FormData();
                formData.append('file', blob, 'canva_studio_print.png');
                formData.append('printer', selectedPrinter);
                formData.append('copies', '1');
                formData.append('pagesize', document.getElementById('studioPreset').value === 'letter' ? 'Letter' : 'A4');
                formData.append('colormodel', 'RGB');
                formData.append('resolution', dpi);
                formData.append('fitpage', 'true');

                fetch('/api/print', { method: 'POST', body: formData })
                    .then(r => r.json())
                    .then(d => {
                        if (d.success) showToast('Canva design spooled directly to printer!', 'success');
                        else showToast('Error: ' + d.error, 'error');
                    });
            });
    });

    document.getElementById('btnStudioDownloadPng').addEventListener('click', () => {
        if (!canvaStudioCanvas) return;
        const dpi = parseInt(document.getElementById('studioDpi').value);
        const dataUrl = canvaStudioCanvas.toDataURL({ format: 'png', multiplier: dpi / 72 });
        const a = document.createElement('a');
        a.href = dataUrl;
        a.download = 'canva_design.png';
        a.click();
    });

    document.getElementById('btnStudioDownloadPdf').addEventListener('click', () => {
        if (!canvaStudioCanvas) return;
        const { jsPDF } = window.jspdf;
        const isLetter = document.getElementById('studioPreset').value === 'letter';
        const doc = new jsPDF('p', 'mm', isLetter ? 'letter' : 'a4');
        const imgData = canvaStudioCanvas.toDataURL({ format: 'png', multiplier: 2.5 });
        doc.addImage(imgData, 'PNG', 10, 10, isLetter ? 195 : 190, isLetter ? 260 : 277);
        doc.save('canva_design.pdf');
    });

    document.getElementById('btnStudioClearAll').addEventListener('click', () => {
        if (canvaStudioCanvas && confirm('Clear entire canvas design?')) {
            canvaStudioCanvas.clear();
            canvaStudioCanvas.setBackgroundColor('#ffffff', () => canvaStudioCanvas.renderAll());
            recordCanvaHistory();
        }
    });

    // ==========================================
    // SCAN DIGITIZER
    // ==========================================
    document.getElementById('btnTriggerScan').addEventListener('click', async () => {
        const btn = document.getElementById('btnTriggerScan');
        btn.disabled = true;
        showToast('Scanning in progress...', 'info');

        const formData = new FormData();
        formData.append('resolution', document.getElementById('scanResolution').value);
        formData.append('mode', document.getElementById('scanMode').value);
        formData.append('format', document.getElementById('scanFormat').value);
        formData.append('save_samba', document.getElementById('scanSaveSamba').checked);

        try {
            const res = await fetch('/api/scan', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                showToast('Scan complete!', 'success');
                if (data.data) {
                    const img = document.getElementById('scanPreviewImg');
                    img.src = 'data:image/png;base64,' + data.data;
                    document.getElementById('scanPreviewContainer').style.display = 'block';
                    document.getElementById('scanEmptyState').style.display = 'none';
                }
            } else {
                showToast('Scan error: ' + data.error, 'error');
            }
        } catch (e) {
            showToast('Scan failed', 'error');
        } finally {
            btn.disabled = false;
        }
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
                        <td style="font-weight: 600;">🔒 ${j.filename}</td>
                        <td>${j.pages}</td>
                        <td>${j.copies}</td>
                        <td>${j.colormodel}</td>
                        <td style="color: var(--accent); font-weight:600;">₱${j.cost.toFixed(2)}</td>
                        <td><button class="btn-primary" style="padding: 4px 10px; font-size: 11px; width:auto;" onclick="openPinModal('${j.job_id}', '${j.filename}')">Release</button></td>
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
                        <td style="font-weight: 600;">${j.title}</td>
                        <td>${j.printer}</td>
                        <td>${j.pages}</td>
                        <td>${j.copies}</td>
                        <td>${j.colormodel}</td>
                        <td style="color: var(--accent); font-weight:600;">${j.cost}</td>
                        <td><span style="font-size: 11px; padding: 2px 6px; border-radius: 4px; background: var(--surface-elevated);">${j.status}</span></td>
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
</html>
"""

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

@app.route('/api/scan', methods=['POST'])
def api_scan():
    resolution = request.form.get('resolution', '150')
    mode = request.form.get('mode', 'Color')
    fmt = request.form.get('format', 'pdf').lower()
    save_samba = request.form.get('save_samba', 'true') == 'true'

    if resolution not in ['75', '150', '300', '600']: resolution = '150'
    if mode not in ['Color', 'Gray', 'Lineart']: mode = 'Color'
    if fmt not in ['pdf', 'png', 'jpeg']: fmt = 'pdf'

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    temp_dir = tempfile.mkdtemp(prefix="scan_")
    raw_pnm = os.path.join(temp_dir, 'raw.pnm')
    final_file = os.path.join(temp_dir, f'scan_{timestamp}.{fmt}')

    try:
        scan_cmd = ['scanimage', f'--resolution={resolution}', f'--mode={mode}']
        with open(raw_pnm, 'wb') as f_out:
            p = subprocess.run(scan_cmd, stdout=f_out, stderr=subprocess.PIPE, timeout=90)
            if p.returncode != 0:
                err_msg = p.stderr.decode('utf-8', errors='ignore').strip()
                if 'no SANE devices found' in err_msg or not err_msg:
                    err_msg = 'Scanner not detected. Verify USB connection and power.'
                return jsonify({'success': False, 'error': err_msg}), 500

        subprocess.run(['convert', raw_pnm, final_file], check=True, timeout=60)

        samba_saved = False
        samba_path = ''
        if save_samba:
            samba_dir = '/srv/shares/public/Scans'
            try:
                os.makedirs(samba_dir, exist_ok=True)
                samba_dest = os.path.join(samba_dir, f'scan_{timestamp}.{fmt}')
                shutil.copy2(final_file, samba_dest)
                os.chmod(samba_dest, 0o666)
                samba_saved = True
                samba_path = f'\\\\\\\\printserver.local\\\\Public\\\\Scans\\\\scan_{timestamp}.{fmt}'
            except Exception:
                pass

        import base64
        with open(final_file, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')

        return jsonify({
            'success': True,
            'filename': f'scan_{timestamp}.{fmt}',
            'format': fmt,
            'data': b64,
            'samba_saved': samba_saved,
            'samba_path': samba_path
        })
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Scanning timed out. Check scanner connection.'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

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
