import os
import subprocess
import tempfile
import time
import json
import re
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024  # 150MB file size limit

INDEX_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Print Server</title>
    <script>
        (function() {
            const savedTheme = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
            document.documentElement.setAttribute('data-theme', savedTheme);
        })();
    </script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
    <style>
        :root {
            --radius-sm: 6px;
            --radius-md: 8px;
            --radius-lg: 12px;
            --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --transition-theme: background-color 0.2s ease, border-color 0.2s ease, color 0.2s ease;
        }

        /* Dark Theme Palette (Default) */
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
            --margin-line: rgba(59, 130, 246, 0.45);
            --accent: #3b82f6;
            --success: #22c55e;
            --danger: #ef4444;
        }

        /* Light Theme Palette */
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
            --margin-line: rgba(37, 99, 235, 0.35);
            --accent: #2563eb;
            --success: #16a34a;
            --danger: #dc2626;
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

        /* Header */
        header {
            border-bottom: 1px solid var(--border);
            padding: 12px 28px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--surface);
            transition: var(--transition-theme);
            position: sticky;
            top: 0;
            z-index: 40;
        }

        .header-brand {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .header-title {
            font-size: 15px;
            font-weight: 600;
            color: var(--text-primary);
            letter-spacing: -0.01em;
        }

        .printer-status {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            color: var(--text-secondary);
            padding: 3px 8px;
            background: var(--badge-bg);
            border: 1px solid var(--border);
            border-radius: 9999px;
        }

        .status-dot {
            width: 6px;
            height: 6px;
            background: var(--success);
            border-radius: 50%;
        }

        .header-controls {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .header-nav {
            display: flex;
            gap: 4px;
            background: var(--surface-elevated);
            padding: 3px;
            border-radius: var(--radius-md);
            border: 1px solid var(--border);
        }

        .nav-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-size: 13px;
            font-weight: 500;
            padding: 6px 14px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            transition: all 0.15s ease;
        }

        .nav-btn:hover {
            color: var(--text-primary);
        }

        .nav-btn.active {
            background: var(--surface);
            color: var(--text-primary);
            box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }

        /* Theme Toggle Button */
        .btn-theme {
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            width: 34px;
            height: 34px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s;
        }

        .btn-theme:hover {
            color: var(--text-primary);
            border-color: var(--border-hover);
        }

        .icon-sun, .icon-moon {
            width: 16px;
            height: 16px;
            fill: none;
            stroke: currentColor;
            stroke-width: 2;
            stroke-linecap: round;
            stroke-linejoin: round;
        }

        html[data-theme="dark"] .icon-moon { display: none; }
        html[data-theme="dark"] .icon-sun { display: block; }
        html[data-theme="light"] .icon-sun { display: none; }
        html[data-theme="light"] .icon-moon { display: block; }

        /* Wide Main Container (Preview Dominant Layout) */
        main {
            flex: 1;
            max-width: 1600px;
            width: 100%;
            margin: 0 auto;
            padding: 24px 28px 40px;
        }

        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }

        /* Large Preview + Slim Settings Sidebar Grid */
        .grid-layout {
            display: grid;
            grid-template-columns: minmax(0, 1fr) 370px;
            gap: 24px;
            align-items: start;
        }

        @media (max-width: 1024px) {
            .grid-layout { grid-template-columns: 1fr; }
        }

        /* Card Panels */
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
            transition: var(--transition-theme);
        }

        .settings-sidebar {
            position: sticky;
            top: 76px;
        }

        .card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 14px;
        }

        .card-heading {
            font-size: 14px;
            font-weight: 600;
            color: var(--text-primary);
        }

        /* Segmented Mode Switch */
        .segmented-control {
            display: inline-flex;
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            padding: 2px;
            border-radius: var(--radius-sm);
        }

        .segmented-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-size: 12px;
            font-weight: 500;
            padding: 4px 12px;
            border-radius: 4px;
            cursor: pointer;
            transition: all 0.15s;
        }

        .segmented-btn.active {
            background: var(--surface);
            color: var(--text-primary);
            box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }

        /* Upload Dropzone (Compact in Preview Header) */
        .dropzone {
            border: 1px dashed var(--border-hover);
            border-radius: var(--radius-md);
            padding: 20px;
            text-align: center;
            cursor: pointer;
            background: var(--bg);
            transition: border-color 0.15s ease, background-color 0.2s ease;
            margin-bottom: 16px;
        }

        .dropzone:hover, .dropzone.dragover {
            border-color: var(--text-secondary);
        }

        .dropzone-label {
            font-size: 13px;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 2px;
        }

        .dropzone-sub {
            font-size: 12px;
            color: var(--text-tertiary);
        }

        /* High-Impact Real-Time Paper Preview */
        .preview-box {
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            background: var(--surface);
            overflow: hidden;
        }

        .preview-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 16px;
            border-bottom: 1px solid var(--border);
            font-size: 12px;
            color: var(--text-secondary);
            background: var(--surface);
        }

        .preview-info {
            display: flex;
            align-items: center;
            gap: 8px;
            overflow: hidden;
        }

        .preview-name {
            font-weight: 500;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 260px;
        }

        .preview-meta-pill {
            font-size: 11px;
            font-family: var(--font-mono);
            color: var(--text-tertiary);
            background: var(--surface-elevated);
            padding: 2px 6px;
            border-radius: 4px;
            border: 1px solid var(--border);
        }

        .btn-clear {
            background: transparent;
            border: none;
            color: var(--text-tertiary);
            cursor: pointer;
            font-size: 13px;
            padding: 2px 6px;
            border-radius: 4px;
        }
        .btn-clear:hover {
            color: var(--text-primary);
            background: var(--surface-elevated);
        }

        /* Large Virtual Workspace Viewport */
        .preview-viewport {
            background: var(--desk-bg);
            padding: 36px 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 600px;
            position: relative;
            overflow: hidden;
            transition: var(--transition-theme);
        }

        /* Simulated Physical Paper Sheet with Smooth Animation */
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

        /* Dynamic Printable Margins Box */
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

        /* Document Render Inside Margins */
        .content-render-wrap {
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            pointer-events: auto;
        }

        .content-render-wrap img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            display: block;
        }

        .content-render-wrap.scale-fill img {
            object-fit: cover;
            width: 100%;
            height: 100%;
        }

        #pdfCanvas {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            display: block;
        }

        .text-sheet-content {
            font-family: var(--font-mono);
            font-size: 11px;
            line-height: 1.4;
            color: #1e293b;
            padding: 8px;
            overflow: hidden;
            width: 100%;
            height: 100%;
            word-break: break-all;
            white-space: pre-wrap;
        }

        /* Live Preview Metadata Tag */
        .preview-dimensions-tag {
            margin-top: 18px;
            font-size: 11px;
            font-family: var(--font-mono);
            color: var(--text-tertiary);
            display: flex;
            align-items: center;
            gap: 10px;
            background: var(--surface);
            padding: 4px 12px;
            border-radius: 9999px;
            border: 1px solid var(--border);
        }

        .preview-pagination {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
            color: var(--text-tertiary);
        }

        .page-btn {
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 1px 6px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 11px;
        }

        /* Form Controls */
        .form-row {
            margin-bottom: 14px;
        }

        .label {
            display: block;
            font-size: 12px;
            font-weight: 500;
            color: var(--text-secondary);
            margin-bottom: 5px;
        }

        .input, select {
            width: 100%;
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text-primary);
            font-family: inherit;
            font-size: 13px;
            padding: 7px 10px;
            border-radius: var(--radius-sm);
            outline: none;
            transition: border-color 0.15s;
        }

        .input:focus, select:focus {
            border-color: var(--text-secondary);
        }

        .grid-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }

        .grid-4 {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr 1fr;
            gap: 6px;
        }

        .margin-input-col {
            text-align: center;
        }

        .margin-input-col span {
            display: block;
            font-size: 10px;
            color: var(--text-tertiary);
            margin-bottom: 2px;
            font-family: var(--font-mono);
        }

        .margin-input-col input {
            text-align: center;
            padding: 5px 2px;
            font-size: 12px;
            font-family: var(--font-mono);
        }

        /* Segmented Radios */
        .radio-pills {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px;
        }

        .radio-pill {
            position: relative;
        }

        .radio-pill input {
            position: absolute;
            opacity: 0;
            width: 0;
            height: 0;
        }

        .radio-pill label {
            display: block;
            text-align: center;
            padding: 7px 8px;
            font-size: 12px;
            font-weight: 500;
            color: var(--text-secondary);
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            cursor: pointer;
            transition: all 0.15s;
        }

        .radio-pill input:checked + label {
            background: var(--surface-elevated);
            border-color: var(--text-secondary);
            color: var(--text-primary);
        }

        /* Copies Stepper */
        .stepper {
            display: flex;
            align-items: center;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
        }

        .stepper-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            width: 30px;
            height: 32px;
            font-size: 14px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .stepper-btn:hover {
            color: var(--text-primary);
        }

        .stepper-val {
            flex: 1;
            border: none !important;
            text-align: center;
            font-size: 13px;
            font-weight: 500;
            padding: 0;
            background: transparent !important;
        }

        /* Primary Print Button */
        .btn-submit {
            width: 100%;
            background: var(--btn-bg);
            color: var(--btn-text);
            border: none;
            font-family: inherit;
            font-size: 13px;
            font-weight: 600;
            padding: 10px 16px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            transition: opacity 0.15s;
            margin-top: 18px;
        }

        .btn-submit:hover {
            opacity: 0.9;
        }

        .btn-submit:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }

        /* Simple Feedback Toast */
        .toast {
            margin-top: 14px;
            padding: 10px 14px;
            border-radius: var(--radius-sm);
            font-size: 12px;
            display: none;
        }

        .toast.success {
            background: rgba(34, 197, 94, 0.1);
            border: 1px solid rgba(34, 197, 94, 0.25);
            color: var(--success);
        }

        .toast.error {
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.25);
            color: var(--danger);
        }

        /* Jobs Queue Table */
        .table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }

        .table th {
            text-align: left;
            padding: 10px 12px;
            font-size: 12px;
            font-weight: 500;
            color: var(--text-tertiary);
            border-bottom: 1px solid var(--border);
        }

        .table td {
            padding: 12px;
            border-bottom: 1px solid var(--border);
            color: var(--text-secondary);
        }

        .table tr:last-child td {
            border-bottom: none;
        }

        .badge-status {
            display: inline-block;
            font-size: 11px;
            font-weight: 500;
            padding: 2px 6px;
            border-radius: 4px;
            background: var(--surface-elevated);
            border: 1px solid var(--border);
        }

        .badge-status.active {
            color: var(--accent);
            border-color: rgba(37, 99, 235, 0.4);
        }

        .badge-status.completed {
            color: var(--text-tertiary);
        }

        .btn-action {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 4px;
            cursor: pointer;
        }

        .btn-action:hover {
            color: var(--danger);
            border-color: var(--danger);
        }

        /* System Telemetry List */
        .info-list {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
        }

        .info-item {
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 14px;
        }

        .info-label {
            font-size: 11px;
            color: var(--text-tertiary);
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 4px;
        }

        .info-value {
            font-family: var(--font-mono);
            font-size: 13px;
            color: var(--text-primary);
        }

        .info-value a {
            color: var(--accent);
            text-decoration: none;
        }
        .info-value a:hover {
            text-decoration: underline;
        }

        footer {
            border-top: 1px solid var(--border);
            padding: 18px 28px;
            font-size: 12px;
            color: var(--text-tertiary);
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--surface);
            transition: var(--transition-theme);
        }

        footer a {
            color: var(--text-secondary);
            text-decoration: none;
        }

        footer a:hover {
            text-decoration: underline;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>

    <header>
        <div class="header-brand">
            <span class="header-title">Print Server</span>
            <div class="printer-status">
                <span class="status-dot"></span>
                <span id="headerStatusText">HP Smart Tank 520</span>
            </div>
        </div>

        <div class="header-controls">
            <nav class="header-nav">
                <button class="nav-btn active" onclick="showTab('print')">Print</button>
                <button class="nav-btn" onclick="showTab('scan')">Scan</button>
                <button class="nav-btn" onclick="showTab('queue')">Queue</button>
                <button class="nav-btn" onclick="showTab('system')">System</button>
            </nav>

            <button type="button" class="btn-theme" onclick="toggleTheme()" title="Toggle theme">
                <!-- Sun Icon -->
                <svg class="icon-sun" viewBox="0 0 24 24">
                    <circle cx="12" cy="12" r="5"></circle>
                    <line x1="12" y1="1" x2="12" y2="3"></line>
                    <line x1="12" y1="21" x2="12" y2="23"></line>
                    <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
                    <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
                    <line x1="1" y1="12" x2="3" y2="12"></line>
                    <line x1="21" y1="12" x2="23" y2="12"></line>
                    <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
                    <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
                </svg>
                <!-- Moon Icon -->
                <svg class="icon-moon" viewBox="0 0 24 24">
                    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
                </svg>
            </button>
        </div>
    </header>

    <main>
        <!-- TAB: PRINT (Preview Dominant Split) -->
        <div id="tab-print" class="tab-content active">
            <form id="printForm">
                <div class="grid-layout">
                    
                    <!-- Left Column: Large Dynamic Preview (Dominant Share) -->
                    <div class="card">
                        <div class="card-header">
                            <span class="card-heading">Document Preview</span>
                            <span id="fileBadge" style="font-size: 11px; color: var(--text-tertiary);">No file selected</span>
                        </div>

                        <input type="file" id="fileInput" name="file" style="display: none;" accept=".pdf,.png,.jpg,.jpeg,.txt,.doc,.docx,.odt,.rtf">
                        <div class="dropzone" id="dropzone">
                            <div class="dropzone-label">Click or drop document to preview & print</div>
                            <div class="dropzone-sub">Supports PDF, DOCX, Word, Images, TXT</div>
                        </div>

                        <div class="preview-box" id="previewBox" style="display: none;">
                            <div class="preview-head">
                                <div class="preview-info">
                                    <span class="preview-name" id="previewName">file.pdf</span>
                                    <span class="preview-meta-pill" id="previewSizePill">0 KB</span>
                                </div>
                                <div style="display: flex; align-items: center; gap: 8px;">
                                    <div class="preview-pagination" id="previewPagination" style="display: none;">
                                        <button type="button" class="page-btn" id="btnPrev">‹</button>
                                        <span id="pageIndicator">1 / 1</span>
                                        <button type="button" class="page-btn" id="btnNext">›</button>
                                    </div>
                                    <button type="button" class="btn-clear" onclick="clearFile()">✕</button>
                                </div>
                            </div>

                            <!-- Large Virtual Desk with Realistic Paper Sheet -->
                            <div class="preview-viewport">
                                <div class="paper-sheet" id="paperSheet">
                                    <div class="printable-area" id="printableArea">
                                        <div class="content-render-wrap" id="renderWrap">
                                            <img id="imgPreview" style="display: none;" alt="Preview">
                                            <canvas id="pdfCanvas" style="display: none;"></canvas>
                                            <div id="txtPreview" class="text-sheet-content" style="display: none;"></div>
                                        </div>
                                    </div>
                                </div>
                                <div class="preview-dimensions-tag">
                                    <span id="sheetFormatTag">A4 • 210 × 297 mm</span>
                                    <span>•</span>
                                    <span id="sheetOrientationTag">Portrait</span>
                                    <span>•</span>
                                    <span id="sheetMarginTag">Printable Area</span>
                                    <span>•</span>
                                    <span id="sheetColorTag">Color</span>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Right Column: Settings Panel (Slim Sidebar) -->
                    <div class="card settings-sidebar">
                        <div class="card-header">
                            <span class="card-heading">Settings</span>
                            <div class="segmented-control">
                                <button type="button" class="segmented-btn active" id="btnSimple" onclick="setMode('simple')">Simple</button>
                                <button type="button" class="segmented-btn" id="btnAdv" onclick="setMode('advanced')">Advanced</button>
                            </div>
                        </div>

                        <!-- Destination Printer -->
                        <div class="form-row">
                            <label class="label" for="printerSelect">Printer</label>
                            <select id="printerSelect" name="printer" required>
                                <option value="HP_Smart_Tank_520">HP Smart Tank 520</option>
                            </select>
                        </div>

                        <!-- Copies & Color Mode -->
                        <div class="grid-2 form-row">
                            <div>
                                <label class="label">Copies</label>
                                <div class="stepper">
                                    <button type="button" class="stepper-btn" onclick="stepCopies(-1)">−</button>
                                    <input type="number" id="copiesInput" name="copies" class="input stepper-val" value="1" min="1" max="99">
                                    <button type="button" class="stepper-btn" onclick="stepCopies(1)">+</button>
                                </div>
                            </div>
                            <div>
                                <label class="label">Color</label>
                                <div class="radio-pills">
                                    <div class="radio-pill">
                                        <input type="radio" id="colRGB" name="colormodel" value="RGB" checked onchange="updatePreviewLayout()">
                                        <label for="colRGB">Color</label>
                                    </div>
                                    <div class="radio-pill">
                                        <input type="radio" id="colGray" name="colormodel" value="KGray" onchange="updatePreviewLayout()">
                                        <label for="colGray">B&W</label>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Simple Mode: Basic Paper Size -->
                        <div id="secSimple">
                            <div class="form-row">
                                <label class="label">Paper Size</label>
                                <select id="simpleSize" onchange="syncSize(this.value)">
                                    <option value="A4" selected>A4 (210 × 297 mm)</option>
                                    <option value="Letter">Letter (8.5 × 11 in)</option>
                                    <option value="Legal">Legal (8.5 × 14 in)</option>
                                    <option value="4x6in">4 × 6 in Photo</option>
                                    <option value="5x7in">5 × 7 in Photo</option>
                                </select>
                            </div>
                        </div>

                        <!-- Advanced Mode: Detailed Controls -->
                        <div id="secAdv" style="display: none;">
                            <div class="grid-2 form-row">
                                <div>
                                    <label class="label">Paper Size</label>
                                    <select id="advSize" name="pagesize" onchange="updatePreviewLayout()">
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
                                <div>
                                    <label class="label">Media Type</label>
                                    <select name="mediatype">
                                        <option value="Plain" selected>Plain Paper</option>
                                        <option value="Glossy">Glossy Photo</option>
                                        <option value="FastGlossy">Everyday Photo</option>
                                        <option value="CoatedMatte">Matte Paper</option>
                                        <option value="CoatedBrochure">Brochure</option>
                                    </select>
                                </div>
                            </div>

                            <!-- Margin Controls with Custom Inputs -->
                            <div class="form-row">
                                <label class="label">Margins</label>
                                <select id="marginSelect" name="margin_mode" onchange="handleMarginChange(this.value)">
                                    <option value="printable" selected>Full Printable Area (Standard)</option>
                                    <option value="none">Zero / Borderless (0 mm)</option>
                                    <option value="normal">Normal (12.7 mm / 0.5 in)</option>
                                    <option value="custom">Custom Margins...</option>
                                </select>
                            </div>

                            <!-- Custom Margin mm Inputs (Hidden unless Custom is selected) -->
                            <div id="customMarginBox" class="form-row" style="display: none;">
                                <label class="label" style="font-size: 11px;">Custom Margins (mm)</label>
                                <div class="grid-4">
                                    <div class="margin-input-col">
                                        <span>Top</span>
                                        <input type="number" id="mTop" name="m_top" class="input" value="10" min="0" max="60" oninput="updatePreviewLayout()">
                                    </div>
                                    <div class="margin-input-col">
                                        <span>Bottom</span>
                                        <input type="number" id="mBottom" name="m_bottom" class="input" value="10" min="0" max="60" oninput="updatePreviewLayout()">
                                    </div>
                                    <div class="margin-input-col">
                                        <span>Left</span>
                                        <input type="number" id="mLeft" name="m_left" class="input" value="10" min="0" max="60" oninput="updatePreviewLayout()">
                                    </div>
                                    <div class="margin-input-col">
                                        <span>Right</span>
                                        <input type="number" id="mRight" name="m_right" class="input" value="10" min="0" max="60" oninput="updatePreviewLayout()">
                                    </div>
                                </div>
                            </div>

                            <div class="grid-2 form-row">
                                <div>
                                    <label class="label">Quality</label>
                                    <select name="quality">
                                        <option value="Normal" selected>Normal</option>
                                        <option value="Best">Best</option>
                                        <option value="Photo">High Resolution</option>
                                    </select>
                                </div>
                                <div>
                                    <label class="label">Two-Sided</label>
                                    <select name="duplex">
                                        <option value="None" selected>Off (1-Sided)</option>
                                        <option value="DuplexNoTumble">Long Edge</option>
                                        <option value="DuplexTumble">Short Edge</option>
                                    </select>
                                </div>
                            </div>

                            <div class="grid-2 form-row">
                                <div>
                                    <label class="label">Orientation</label>
                                    <select id="orientationSelect" name="orientation" onchange="updatePreviewLayout()">
                                        <option value="auto" selected>Auto</option>
                                        <option value="3">Portrait</option>
                                        <option value="4">Landscape</option>
                                    </select>
                                </div>
                                <div>
                                    <label class="label">Scaling</label>
                                    <select id="scalingSelect" name="fitpage" onchange="updatePreviewLayout()">
                                        <option value="true" selected>Fit to Page</option>
                                        <option value="false">Fill Sheet</option>
                                    </select>
                                </div>
                            </div>

                            <div class="form-row">
                                <label class="label">Page Range</label>
                                <input type="text" name="pagerange" class="input" placeholder="e.g. 1-4, 7">
                            </div>
                        </div>

                        <button type="submit" class="btn-submit" id="btnSubmit">Print</button>
                        <div class="toast" id="toast"></div>
                    </div>

                </div>
            </form>
        </div>

        <!-- TAB: SCAN (Flatbed Scanner) -->
        <div id="tab-scan" class="tab-content">
            <div class="grid-layout">
                <!-- Left: Scan Preview & Output -->
                <div class="card">
                    <div class="card-header">
                        <span class="card-heading">Flatbed Scan Output</span>
                        <div id="scanStatusBadge" class="preview-badge" style="display: none;">Ready</div>
                    </div>
                    
                    <div id="scanDisplayArea" style="min-height: 480px; display: flex; flex-direction: column; align-items: center; justify-content: center; background: var(--desk-bg); border-radius: var(--radius-md); padding: 24px; position: relative;">
                        <!-- Placeholder state -->
                        <div id="scanPlaceholder" style="text-align: center; color: var(--text-tertiary); max-width: 320px;">
                            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom: 12px; opacity: 0.6;">
                                <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
                                <line x1="3" y1="9" x2="21" y2="9"></line>
                                <line x1="9" y1="21" x2="9" y2="9"></line>
                            </svg>
                            <p style="font-size: 13px; font-weight: 500; color: var(--text-secondary); margin-bottom: 4px;">Flatbed Scanner Ready</p>
                            <p style="font-size: 12px;">Place your document face-down on the HP Smart Tank 520 glass and click "Scan Flatbed".</p>
                        </div>

                        <!-- Progress / Scanning state -->
                        <div id="scanLoading" style="display: none; text-align: center; color: var(--text-secondary);">
                            <div style="width: 36px; height: 36px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 16px;"></div>
                            <p style="font-size: 14px; font-weight: 500; color: var(--text-primary); margin-bottom: 4px;">Scanning document...</p>
                            <p style="font-size: 12px; color: var(--text-tertiary);">Reading optical sensor on HP Smart Tank 520</p>
                        </div>

                        <!-- Scanned Image Result -->
                        <img id="scanResultImg" style="display: none; max-width: 100%; max-height: 520px; border-radius: var(--radius-sm); box-shadow: var(--paper-shadow); object-fit: contain;">

                        <!-- Scanned PDF Result -->
                        <div id="scanResultPdf" style="display: none; width: 100%; height: 520px;">
                            <canvas id="scanPdfCanvas" style="display: block; margin: 0 auto; max-width: 100%; max-height: 520px; border-radius: var(--radius-sm); box-shadow: var(--paper-shadow);"></canvas>
                        </div>
                    </div>

                    <!-- Scanned Action Bar -->
                    <div id="scanActionBar" style="display: none; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
                        <span id="scanFileInfo" style="font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary);"></span>
                        <div style="display: flex; gap: 8px;">
                            <a id="btnScanDownload" class="btn-action" style="text-decoration: none; padding: 6px 14px; background: var(--surface-elevated);" download>Download</a>
                            <button type="button" class="btn-action" onclick="loadScanIntoPrint()" style="background: var(--text-primary); color: var(--bg);">Print Document</button>
                        </div>
                    </div>
                </div>

                <!-- Right: Scan Settings Sidebar -->
                <div class="card settings-sidebar">
                    <div class="card-header">
                        <span class="card-heading">Scanner Settings</span>
                    </div>

                    <form id="scanForm">
                        <div class="form-row">
                            <label class="label">Resolution (DPI)</label>
                            <select id="scanResolution" name="resolution">
                                <option value="75">75 DPI (Draft / Fast)</option>
                                <option value="150" selected>150 DPI (Document / Standard)</option>
                                <option value="300">300 DPI (Photo / Crisp)</option>
                                <option value="600">600 DPI (High Resolution)</option>
                            </select>
                        </div>

                        <div class="form-row">
                            <label class="label">Color Mode</label>
                            <select id="scanMode" name="mode">
                                <option value="Color" selected>Full Color</option>
                                <option value="Gray">Grayscale</option>
                                <option value="Lineart">Monochrome (Black & White)</option>
                            </select>
                        </div>

                        <div class="form-row">
                            <label class="label">Output Format</label>
                            <select id="scanFormat" name="format">
                                <option value="pdf" selected>PDF Document (.pdf)</option>
                                <option value="png">PNG Image (.png)</option>
                                <option value="jpeg">JPEG Photo (.jpg)</option>
                            </select>
                        </div>

                        <div class="form-row" style="margin-top: 16px;">
                            <label style="display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-secondary); cursor: pointer;">
                                <input type="checkbox" id="scanSaveSamba" name="save_samba" value="true" checked style="accent-color: var(--accent);">
                                Save copy to Samba (\\\\printserver.local\\\\Public\\\\Scans)
                            </label>
                        </div>

                        <button type="submit" class="btn-submit" id="btnScanSubmit" style="margin-top: 16px;">Scan Flatbed</button>
                    </form>

                    <div id="scanToast" class="toast"></div>
                </div>
            </div>
        </div>

        <!-- TAB: QUEUE -->
        <div id="tab-queue" class="tab-content">
            <div class="card">
                <div class="card-header">
                    <span class="card-heading">Print Queue</span>
                    <button type="button" class="btn-action" onclick="fetchJobs()">Refresh</button>
                </div>
                <div style="overflow-x: auto;">
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Job</th>
                                <th>User</th>
                                <th>Size</th>
                                <th>Submitted</th>
                                <th>Status</th>
                                <th></th>
                            </tr>
                        </thead>
                        <tbody id="queueBody">
                            <tr>
                                <td colspan="6" style="text-align: center; color: var(--text-tertiary); padding: 24px;">No recent jobs.</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- TAB: SYSTEM -->
        <div id="tab-system" class="tab-content">
            <div class="card">
                <div class="card-header">
                    <span class="card-heading">Home Server Services & Information</span>
                    <span style="font-size: 12px; color: var(--text-tertiary);">Armbian / Debian 13</span>
                </div>
                <div class="info-list">
                    <div class="info-item">
                        <div class="info-label">Web Print Server</div>
                        <div class="info-value">Port 80 (Standard) & 8080</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">AdGuard Home (DNS)</div>
                        <div class="info-value"><a href="http://10.0.0.95:3000" target="_blank">:3000 (Setup / Dashboard)</a></div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Samba File Share</div>
                        <div class="info-value">\\\\printserver.local\\Public</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">CUPS Admin</div>
                        <div class="info-value"><a href="https://printserver.local:631" target="_blank">:631</a></div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">mDNS Hostname</div>
                        <div class="info-value">printserver.local</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">LAN Address</div>
                        <div class="info-value">10.0.0.95</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Tailscale Address</div>
                        <div class="info-value">100.104.185.40</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Memory</div>
                        <div class="info-value" id="sysRam">--</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">System Load</div>
                        <div class="info-value" id="sysLoad">--</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Uptime</div>
                        <div class="info-value" id="sysUptime">--</div>
                    </div>
                </div>
            </div>
        </div>
    </main>

    <footer>
        <span>Print Server • ZTE B860H</span>
        <span>
            <a href="http://10.0.0.95:3000" target="_blank">AdGuard Home</a> • 
            <a href="https://printserver.local:631" target="_blank">CUPS Admin</a> • 
            <a href="https://github.com/Sheesh0327/armbian-print-vpn-server" target="_blank">GitHub</a>
        </span>
    </footer>

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

        let currentPdf = null;
        let pdfPage = 1;
        let pdfPagesTotal = 1;
        let mode = 'simple';
        let docNaturalAspect = 1.414;

        // Theme switching
        function toggleTheme() {
            const current = document.documentElement.getAttribute('data-theme') || 'dark';
            const next = current === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('theme', next);
        }

        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('fileInput');
        const previewBox = document.getElementById('previewBox');
        const previewName = document.getElementById('previewName');
        const previewSizePill = document.getElementById('previewSizePill');
        const fileBadge = document.getElementById('fileBadge');
        const imgPreview = document.getElementById('imgPreview');
        const pdfCanvas = document.getElementById('pdfCanvas');
        const txtPreview = document.getElementById('txtPreview');
        const previewPagination = document.getElementById('previewPagination');
        const pageIndicator = document.getElementById('pageIndicator');
        const btnPrev = document.getElementById('btnPrev');
        const btnNext = document.getElementById('btnNext');
        const toast = document.getElementById('toast');
        const btnSubmit = document.getElementById('btnSubmit');

        // Paper & Margin Preview DOM elements
        const paperSheet = document.getElementById('paperSheet');
        const printableArea = document.getElementById('printableArea');
        const renderWrap = document.getElementById('renderWrap');
        const sheetFormatTag = document.getElementById('sheetFormatTag');
        const sheetOrientationTag = document.getElementById('sheetOrientationTag');
        const sheetMarginTag = document.getElementById('sheetMarginTag');
        const sheetColorTag = document.getElementById('sheetColorTag');
        const customMarginBox = document.getElementById('customMarginBox');

        dropzone.addEventListener('click', () => fileInput.click());
        dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            if (e.dataTransfer.files.length) {
                fileInput.files = e.dataTransfer.files;
                loadFile();
            }
        });
        fileInput.addEventListener('change', loadFile);

        function loadFile() {
            if (!fileInput.files.length) return;
            const f = fileInput.files[0];
            previewName.textContent = f.name;
            const sizeStr = formatBytes(f.size);
            fileBadge.textContent = sizeStr;
            previewSizePill.textContent = sizeStr;
            previewBox.style.display = 'block';

            imgPreview.style.display = 'none';
            pdfCanvas.style.display = 'none';
            txtPreview.style.display = 'none';
            previewPagination.style.display = 'none';

            const ext = f.name.split('.').pop().toLowerCase();
            if (['png', 'jpg', 'jpeg', 'webp'].includes(ext)) {
                const reader = new FileReader();
                reader.onload = (e) => {
                    imgPreview.src = e.target.result;
                    imgPreview.style.display = 'block';
                    imgPreview.onload = () => {
                        docNaturalAspect = imgPreview.naturalHeight / imgPreview.naturalWidth;
                        updatePreviewLayout();
                    };
                };
                reader.readAsDataURL(f);
            } else if (ext === 'pdf') {
                const reader = new FileReader();
                reader.onload = function(e) {
                    pdfjsLib.getDocument(new Uint8Array(e.target.result)).promise.then(pdf => {
                        currentPdf = pdf;
                        pdfPagesTotal = pdf.numPages;
                        pdfPage = 1;
                        if (pdfPagesTotal > 1) previewPagination.style.display = 'flex';
                        renderPdf(pdfPage);
                    }).catch(() => {
                        txtPreview.style.display = 'block';
                        txtPreview.textContent = 'PDF ready to print.';
                        updatePreviewLayout();
                    });
                };
                reader.readAsArrayBuffer(f);
            } else {
                txtPreview.style.display = 'block';
                txtPreview.textContent = `Document (${ext.toUpperCase()}) loaded and ready to print.`;
                updatePreviewLayout();
            }
        }

        function renderPdf(num) {
            if (!currentPdf) return;
            currentPdf.getPage(num).then(page => {
                const viewport = page.getViewport({ scale: 1.5 });
                docNaturalAspect = viewport.height / viewport.width;
                pdfCanvas.height = viewport.height;
                pdfCanvas.width = viewport.width;
                pdfCanvas.style.display = 'block';
                page.render({
                    canvasContext: pdfCanvas.getContext('2d'),
                    viewport: viewport
                });
                pageIndicator.textContent = `${num} / ${pdfPagesTotal}`;
                updatePreviewLayout();
            });
        }

        btnPrev.addEventListener('click', () => { if (pdfPage > 1) { pdfPage--; renderPdf(pdfPage); } });
        btnNext.addEventListener('click', () => { if (pdfPage < pdfPagesTotal) { pdfPage++; renderPdf(pdfPage); } });

        function clearFile() {
            fileInput.value = '';
            previewBox.style.display = 'none';
            fileBadge.textContent = 'No file selected';
            currentPdf = null;
        }

        function handleMarginChange(val) {
            customMarginBox.style.display = (val === 'custom') ? 'block' : 'none';
            updatePreviewLayout();
        }

        // Real-Time Layout, Paper Size & Margins Geometry Engine
        function updatePreviewLayout() {
            const sizeKey = mode === 'simple' 
                ? document.getElementById('simpleSize').value 
                : document.getElementById('advSize').value;
            const spec = PAPER_SPECS[sizeKey] || PAPER_SPECS['A4'];

            let orientation = '3';
            if (mode === 'advanced') {
                orientation = document.getElementById('orientationSelect').value;
            }
            let isLandscape = false;
            if (orientation === '4') {
                isLandscape = true;
            } else if (orientation === 'auto') {
                isLandscape = docNaturalAspect < 0.95;
            }

            const maxDeskWidth = 460;
            const maxDeskHeight = 540;
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

            paperSheet.style.width = sheetW + 'px';
            paperSheet.style.height = sheetH + 'px';

            let mTopMm = 4.2, mBottomMm = 4.2, mLeftMm = 4.2, mRightMm = 4.2;
            let marginLabel = 'Printable Area';

            if (mode === 'advanced') {
                const mMode = document.getElementById('marginSelect').value;
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
            }

            const scaleX = sheetW / paperPhysW;
            const scaleY = sheetH / paperPhysH;

            const topPx = Math.round(mTopMm * scaleY);
            const bottomPx = Math.round(mBottomMm * scaleY);
            const leftPx = Math.round(mLeftMm * scaleX);
            const rightPx = Math.round(mRightMm * scaleX);

            printableArea.style.top = topPx + 'px';
            printableArea.style.bottom = bottomPx + 'px';
            printableArea.style.left = leftPx + 'px';
            printableArea.style.right = rightPx + 'px';

            const isGray = document.getElementById('colGray').checked;
            paperSheet.style.filter = isGray ? 'grayscale(100%)' : 'none';
            sheetColorTag.textContent = isGray ? 'B&W' : 'Color';

            if (mode === 'advanced') {
                const fitPage = document.getElementById('scalingSelect').value === 'true';
                renderWrap.classList.toggle('scale-fill', !fitPage);
            } else {
                renderWrap.classList.remove('scale-fill');
            }

            sheetFormatTag.textContent = `${spec.name} • ${spec.desc}`;
            sheetOrientationTag.textContent = isLandscape ? 'Landscape' : 'Portrait';
            sheetMarginTag.textContent = marginLabel;
        }

        function setMode(m) {
            mode = m;
            document.getElementById('btnSimple').classList.toggle('active', m === 'simple');
            document.getElementById('btnAdv').classList.toggle('active', m === 'advanced');
            document.getElementById('secSimple').style.display = m === 'simple' ? 'block' : 'none';
            document.getElementById('secAdv').style.display = m === 'advanced' ? 'block' : 'none';
            updatePreviewLayout();
        }

        function syncSize(val) {
            document.getElementById('advSize').value = val;
            updatePreviewLayout();
        }

        function stepCopies(d) {
            const el = document.getElementById('copiesInput');
            el.value = Math.max(1, Math.min(99, (parseInt(el.value) || 1) + d));
        }

        function showTab(t) {
            document.querySelectorAll('.tab-content').forEach(e => e.classList.remove('active'));
            document.querySelectorAll('.nav-btn').forEach(e => e.classList.remove('active'));
            document.getElementById('tab-' + t).classList.add('active');
            
            const btn = Array.from(document.querySelectorAll('.nav-btn')).find(b => b.textContent.toLowerCase() === t);
            if (btn) btn.classList.add('active');

            if (t === 'queue') fetchJobs();
            if (t === 'system') fetchStatus();
        }

        document.getElementById('printForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!fileInput.files.length) {
                showToast('Select a file to print.', 'error');
                return;
            }

            btnSubmit.disabled = true;
            btnSubmit.textContent = 'Sending...';
            showToast('', '');

            const formData = new FormData(e.target);
            if (mode === 'simple') {
                formData.set('pagesize', document.getElementById('simpleSize').value);
                formData.set('mediatype', 'Plain');
                formData.set('quality', 'Normal');
                formData.set('duplex', 'None');
                formData.set('orientation', 'auto');
                formData.set('fitpage', 'true');
                formData.set('margin_mode', 'printable');
            }

            try {
                const res = await fetch('/api/print', { method: 'POST', body: formData });
                const d = await res.json();
                if (res.ok && d.success) {
                    showToast(`Print job submitted (${d.job_id})`, 'success');
                    clearFile();
                } else {
                    showToast(d.error || 'Print failed.', 'error');
                }
            } catch (err) {
                showToast('Network error: ' + err.message, 'error');
            } finally {
                btnSubmit.disabled = false;
                btnSubmit.textContent = 'Print';
            }
        });

        function showToast(msg, type) {
            if (!msg) { toast.style.display = 'none'; return; }
            toast.textContent = msg;
            toast.className = 'toast ' + type;
            toast.style.display = 'block';
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const d = await res.json();
                if (d.printers && d.printers.length) {
                    document.getElementById('headerStatusText').textContent = d.printers[0].name;
                }
                if (d.telemetry) {
                    document.getElementById('sysRam').textContent = d.telemetry.ram_used + ' / ' + d.telemetry.ram_total;
                    document.getElementById('sysLoad').textContent = d.telemetry.load;
                    document.getElementById('sysUptime').textContent = d.telemetry.uptime;
                }
            } catch (e) {}
        }

        async function fetchJobs() {
            try {
                const res = await fetch('/api/jobs');
                const d = await res.json();
                const tbody = document.getElementById('queueBody');
                if (!d.jobs || !d.jobs.length) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color: var(--text-tertiary); padding: 24px;">No recent jobs.</td></tr>';
                    return;
                }
                tbody.innerHTML = d.jobs.map(j => `
                    <tr>
                        <td style="font-family: var(--font-mono); color: var(--text-primary);">${j.id}</td>
                        <td>${j.user}</td>
                        <td>${formatBytes(j.bytes)}</td>
                        <td>${j.time}</td>
                        <td><span class="badge-status ${j.state.toLowerCase()}">${j.state}</span></td>
                        <td>${j.state === 'Active' ? `<button type="button" class="btn-action" onclick="cancelJob('${j.id}')">Cancel</button>` : ''}</td>
                    </tr>
                `).join('');
            } catch (e) {}
        }

        async function cancelJob(id) {
            if (!confirm(`Cancel job ${id}?`)) return;
            try {
                await fetch('/api/cancel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ job_id: id })
                });
                fetchJobs();
            } catch (e) {}
        }

        function formatBytes(bytes) {
            if (!bytes) return '0 B';
            const k = 1024, sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
        }

        // Scanner Logic
        let currentScanBlob = null;
        let currentScanFilename = '';

        document.getElementById('scanForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('btnScanSubmit');
            const toast = document.getElementById('scanToast');
            const placeholder = document.getElementById('scanPlaceholder');
            const loading = document.getElementById('scanLoading');
            const imgEl = document.getElementById('scanResultImg');
            const pdfEl = document.getElementById('scanResultPdf');
            const actionBar = document.getElementById('scanActionBar');

            btn.disabled = true;
            btn.textContent = 'Scanning...';
            toast.style.display = 'none';
            placeholder.style.display = 'none';
            imgEl.style.display = 'none';
            pdfEl.style.display = 'none';
            actionBar.style.display = 'none';
            loading.style.display = 'block';

            const formData = new FormData(e.target);

            try {
                const res = await fetch('/api/scan', { method: 'POST', body: formData });
                const d = await res.json();
                if (res.ok && d.success) {
                    loading.style.display = 'none';
                    currentScanFilename = d.filename;

                    const byteCharacters = atob(d.data);
                    const byteNumbers = new Array(byteCharacters.length);
                    for (let i = 0; i < byteCharacters.length; i++) {
                        byteNumbers[i] = byteCharacters.charCodeAt(i);
                    }
                    const byteArray = new Uint8Array(byteNumbers);
                    const mime = d.format === 'pdf' ? 'application/pdf' : (d.format === 'png' ? 'image/png' : 'image/jpeg');
                    currentScanBlob = new Blob([byteArray], { type: mime });

                    const blobUrl = URL.createObjectURL(currentScanBlob);
                    const dlBtn = document.getElementById('btnScanDownload');
                    dlBtn.href = blobUrl;
                    dlBtn.download = d.filename;

                    document.getElementById('scanFileInfo').textContent = `${d.filename} (${formatBytes(currentScanBlob.size)})` + (d.samba_saved ? ' • Saved to Samba' : '');
                    actionBar.style.display = 'flex';

                    if (d.format === 'pdf') {
                        pdfEl.style.display = 'block';
                        const loadingTask = pdfjsLib.getDocument({ data: byteArray });
                        const pdf = await loadingTask.promise;
                        const page = await pdf.getPage(1);
                        const viewport = page.getViewport({ scale: 1.5 });
                        const canvas = document.getElementById('scanPdfCanvas');
                        const ctx = canvas.getContext('2d');
                        canvas.height = viewport.height;
                        canvas.width = viewport.width;
                        await page.render({ canvasContext: ctx, viewport: viewport }).promise;
                    } else {
                        imgEl.src = blobUrl;
                        imgEl.style.display = 'block';
                    }
                } else {
                    loading.style.display = 'none';
                    placeholder.style.display = 'block';
                    toast.textContent = d.error || 'Scan failed.';
                    toast.className = 'toast error';
                    toast.style.display = 'block';
                }
            } catch (err) {
                loading.style.display = 'none';
                placeholder.style.display = 'block';
                toast.textContent = 'Scan error: ' + err.message;
                toast.className = 'toast error';
                toast.style.display = 'block';
            } finally {
                btn.disabled = false;
                btn.textContent = 'Scan Flatbed';
            }
        });

        function loadScanIntoPrint() {
            if (!currentScanBlob) return;
            const file = new File([currentScanBlob], currentScanFilename, { type: currentScanBlob.type });
            const dataTransfer = new DataTransfer();
            dataTransfer.items.add(file);
            fileInput.files = dataTransfer.files;
            loadFile();
            showTab('print');
            showToast('Scanned document loaded into print preview.', 'success');
        }

        fetchStatus();
    </script>
</body>
</html>
"""

def parse_printers():
    printers = []
    try:
        raw = subprocess.check_output(['lpstat', '-p', '-d'], text=True)
        default_name = ''
        for line in raw.split('\n'):
            if 'system default destination:' in line:
                default_name = line.split('system default destination:')[-1].strip()

        for line in raw.split('\n'):
            if line.startswith('printer '):
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[1]
                    state = 'Idle'
                    if 'is printing' in line:
                        state = 'Printing'
                    elif 'disabled' in line:
                        state = 'Disabled'
                    printers.append({
                        'name': name,
                        'state': state,
                        'is_default': (name == default_name)
                    })
    except Exception:
        printers.append({'name': 'HP_Smart_Tank_520', 'state': 'Idle', 'is_default': True})
    return printers

def get_telemetry():
    telemetry = {
        'ram_used': '--',
        'ram_total': '--',
        'load': '--',
        'uptime': '--'
    }
    try:
        mem = subprocess.check_output(['free', '-m'], text=True).split('\n')[1].split()
        telemetry['ram_total'] = f"{mem[1]} MB"
        telemetry['ram_used'] = f"{mem[2]} MB"
        
        load = subprocess.check_output(['uptime'], text=True)
        telemetry['load'] = load.split('load average:')[-1].strip()
        up_match = re.search(r'up\s+(.*?),\s+\d+\s+user', load)
        if up_match:
            telemetry['uptime'] = up_match.group(1).strip()
    except Exception:
        pass
    return telemetry

def parse_jobs():
    jobs = []
    try:
        active_raw = subprocess.check_output(['lpstat', '-o'], text=True)
        for line in active_raw.strip().split('\n'):
            if not line.strip(): continue
            parts = line.split()
            if len(parts) >= 4:
                jobs.append({
                    'id': parts[0],
                    'user': parts[1],
                    'bytes': int(parts[2]) if parts[2].isdigit() else 0,
                    'time': ' '.join(parts[3:]),
                    'state': 'Active'
                })

        comp_raw = subprocess.check_output(['lpstat', '-W', 'completed', '-o'], text=True)
        for line in comp_raw.strip().split('\n')[:10]:
            if not line.strip(): continue
            parts = line.split()
            if len(parts) >= 4:
                jobs.append({
                    'id': parts[0],
                    'user': parts[1],
                    'bytes': int(parts[2]) if parts[2].isdigit() else 0,
                    'time': ' '.join(parts[3:]),
                    'state': 'Completed'
                })
    except Exception:
        pass
    return jobs

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/api/status')
def api_status():
    return jsonify({
        'printers': parse_printers(),
        'telemetry': get_telemetry()
    })

@app.route('/api/jobs')
def api_jobs():
    return jsonify({
        'jobs': parse_jobs()
    })

@app.route('/api/cancel', methods=['POST'])
def api_cancel():
    data = request.get_json() or {}
    job_id = data.get('job_id')
    if not job_id:
        return jsonify({'success': False, 'error': 'Missing job_id'}), 400
    try:
        subprocess.run(['cancel', job_id], check=True, text=True)
        return jsonify({'success': True})
    except subprocess.CalledProcessError as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/scan', methods=['POST'])
def api_scan():
    resolution = request.form.get('resolution', '150')
    mode = request.form.get('mode', 'Color')
    fmt = request.form.get('format', 'pdf').lower()
    save_samba = request.form.get('save_samba', 'true') == 'true'

    if resolution not in ['75', '150', '300', '600']:
        resolution = '150'
    if mode not in ['Color', 'Gray', 'Lineart']:
        mode = 'Color'
    if fmt not in ['pdf', 'png', 'jpeg']:
        fmt = 'pdf'

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    temp_dir = tempfile.mkdtemp()
    raw_pnm = os.path.join(temp_dir, 'raw.pnm')
    final_file = os.path.join(temp_dir, f'scan_{timestamp}.{fmt}')

    try:
        scan_cmd = ['scanimage', f'--resolution={resolution}', f'--mode={mode}']
        with open(raw_pnm, 'wb') as f_out:
            p = subprocess.run(scan_cmd, stdout=f_out, stderr=subprocess.PIPE, timeout=90)
            if p.returncode != 0:
                err_msg = p.stderr.decode('utf-8', errors='ignore').strip()
                if 'no SANE devices found' in err_msg or not err_msg:
                    err_msg = 'HP Smart Tank 520 scanner not detected. Please verify the printer is turned on and the USB cable is connected.'
                return jsonify({'success': False, 'error': err_msg}), 500

        # Convert to requested format
        subprocess.run(['convert', raw_pnm, final_file], check=True, timeout=60)

        samba_saved = False
        samba_path = ''
        if save_samba:
            samba_dir = '/srv/shares/public/Scans'
            try:
                os.makedirs(samba_dir, exist_ok=True)
                samba_dest = os.path.join(samba_dir, f'scan_{timestamp}.{fmt}')
                subprocess.run(['cp', final_file, samba_dest], check=True)
                subprocess.run(['chmod', '666', samba_dest], check=True)
                samba_saved = True
                samba_path = f'\\\\printserver.local\\Public\\Scans\\scan_{timestamp}.{fmt}'
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
        return jsonify({'success': False, 'error': 'Scanning timed out. Please check printer power and USB connection.'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if os.path.exists(raw_pnm):
            try: os.remove(raw_pnm)
            except Exception: pass

@app.route('/api/print', methods=['POST'])
def api_print():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Empty filename'}), 400

    printer = request.form.get('printer', 'HP_Smart_Tank_520')
    copies = request.form.get('copies', '1')
    pagesize = request.form.get('pagesize', 'A4')
    mediatype = request.form.get('mediatype', 'Plain')
    quality = request.form.get('quality', 'Normal')
    colormodel = request.form.get('colormodel', 'RGB')
    duplex = request.form.get('duplex', 'None')
    orientation = request.form.get('orientation', 'auto')
    fitpage = request.form.get('fitpage', 'true')
    pagerange = request.form.get('pagerange', '').strip()
    margin_mode = request.form.get('margin_mode', 'printable')

    suffix = os.path.splitext(file.filename)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    # Handle Office Document formats (.docx, .doc, .odt, .rtf) via headless LibreOffice
    if suffix in ['.docx', '.doc', '.odt', '.rtf']:
        try:
            conv_dir = tempfile.mkdtemp()
            subprocess.run([
                'libreoffice', '--headless', '--convert-to', 'pdf',
                tmp_path, '--outdir', conv_dir
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            base_name = os.path.splitext(os.path.basename(tmp_path))[0]
            converted_pdf = os.path.join(conv_dir, f"{base_name}.pdf")
            if os.path.exists(converted_pdf):
                tmp_path = converted_pdf
        except Exception as e:
            pass

    try:
        cmd = ['lp', '-d', printer, '-n', str(copies)]

        if pagesize:
            cmd.extend(['-o', f'PageSize={pagesize}'])
        if mediatype:
            cmd.extend(['-o', f'MediaType={mediatype}'])
        if quality:
            cmd.extend(['-o', f'OutputMode={quality}'])
        if colormodel:
            cmd.extend(['-o', f'ColorModel={colormodel}'])
        if duplex and duplex != 'None':
            cmd.extend(['-o', f'Duplex={duplex}'])
        if orientation in ['3', '4']:
            cmd.extend(['-o', f'orientation-requested={orientation}'])
        if fitpage == 'true':
            cmd.extend(['-o', 'fit-to-page'])
        if pagerange:
            cmd.extend(['-o', f'page-ranges={pagerange}'])

        # Margin adjustments (convert mm to PostScript points: 1 mm = 2.83465 points)
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

        cmd.append(tmp_path)

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        job_id = result.stdout.strip()
        return jsonify({'success': True, 'job_id': job_id})
    except subprocess.CalledProcessError as e:
        return jsonify({'success': False, 'error': e.stderr.strip() or str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 80))
    app.run(host='::', port=port)
