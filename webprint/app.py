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
            padding: 14px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--surface);
            transition: var(--transition-theme);
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

        /* Container Layout */
        main {
            flex: 1;
            max-width: 1080px;
            width: 100%;
            margin: 0 auto;
            padding: 32px 20px;
        }

        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }

        .grid-layout {
            display: grid;
            grid-template-columns: 1.1fr 1fr;
            gap: 24px;
            align-items: start;
        }

        @media (max-width: 860px) {
            .grid-layout { grid-template-columns: 1fr; }
        }

        /* Card Panels */
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
            transition: var(--transition-theme);
        }

        .card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
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

        /* Upload Dropzone */
        .dropzone {
            border: 1px dashed var(--border-hover);
            border-radius: var(--radius-md);
            padding: 36px 20px;
            text-align: center;
            cursor: pointer;
            background: var(--bg);
            transition: border-color 0.15s ease, background-color 0.2s ease;
        }

        .dropzone:hover, .dropzone.dragover {
            border-color: var(--text-secondary);
        }

        .dropzone-label {
            font-size: 13px;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 4px;
        }

        .dropzone-sub {
            font-size: 12px;
            color: var(--text-tertiary);
        }

        /* File Preview Container */
        .preview-box {
            display: none;
            margin-top: 16px;
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            background: var(--bg);
            overflow: hidden;
        }

        .preview-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 14px;
            border-bottom: 1px solid var(--border);
            font-size: 12px;
            color: var(--text-secondary);
        }

        .preview-info {
            display: flex;
            align-items: center;
            gap: 10px;
            overflow: hidden;
        }

        .preview-name {
            font-weight: 500;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 220px;
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

        .preview-canvas-wrap {
            padding: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 220px;
            max-height: 380px;
            overflow: auto;
        }

        .preview-canvas-wrap img {
            max-width: 100%;
            max-height: 340px;
            border-radius: 4px;
            border: 1px solid var(--border);
        }

        #pdfCanvas {
            max-width: 100%;
            height: auto;
            border-radius: 4px;
            border: 1px solid var(--border);
        }

        .preview-pagination {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            color: var(--text-tertiary);
        }

        .page-btn {
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 2px 6px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 11px;
        }

        /* Form Controls */
        .form-row {
            margin-bottom: 16px;
        }

        .label {
            display: block;
            font-size: 12px;
            font-weight: 500;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }

        .input, select {
            width: 100%;
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text-primary);
            font-family: inherit;
            font-size: 13px;
            padding: 8px 12px;
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
            gap: 12px;
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
            padding: 8px 10px;
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
            width: 34px;
            height: 34px;
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
            margin-top: 20px;
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
            grid-template-columns: 1fr 1fr;
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

        footer {
            border-top: 1px solid var(--border);
            padding: 18px 24px;
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
                <button class="nav-btn" onclick="showTab('queue')">Queue</button>
                <button class="nav-btn" onclick="showTab('system')">System</button>
            </nav>

            <button type="button" class="btn-theme" onclick="toggleTheme()" title="Toggle theme">
                <!-- Sun Icon (for Dark mode) -->
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
                <!-- Moon Icon (for Light mode) -->
                <svg class="icon-moon" viewBox="0 0 24 24">
                    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
                </svg>
            </button>
        </div>
    </header>

    <main>
        <!-- TAB: PRINT -->
        <div id="tab-print" class="tab-content active">
            <form id="printForm">
                <div class="grid-layout">
                    
                    <!-- Left: File & Preview -->
                    <div class="card">
                        <div class="card-header">
                            <span class="card-heading">Document</span>
                            <span id="fileBadge" style="font-size: 11px; color: var(--text-tertiary);">No file selected</span>
                        </div>

                        <input type="file" id="fileInput" name="file" style="display: none;" accept=".pdf,.png,.jpg,.jpeg,.txt,.doc,.docx">
                        <div class="dropzone" id="dropzone">
                            <div class="dropzone-label">Click or drop document here</div>
                            <div class="dropzone-sub">PDF, JPG, PNG, TXT, DOCX</div>
                        </div>

                        <div class="preview-box" id="previewBox">
                            <div class="preview-head">
                                <div class="preview-info">
                                    <span class="preview-name" id="previewName">file.pdf</span>
                                    <span id="previewDetails" style="font-size: 11px; color: var(--text-tertiary);"></span>
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
                            <div class="preview-canvas-wrap">
                                <img id="imgPreview" style="display: none;" alt="Preview">
                                <canvas id="pdfCanvas" style="display: none;"></canvas>
                                <div id="txtPreview" style="display: none; font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); white-space: pre-wrap; width: 100%;"></div>
                            </div>
                        </div>
                    </div>

                    <!-- Right: Settings -->
                    <div class="card">
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
                                        <input type="radio" id="colRGB" name="colormodel" value="RGB" checked>
                                        <label for="colRGB">Color</label>
                                    </div>
                                    <div class="radio-pill">
                                        <input type="radio" id="colGray" name="colormodel" value="KGray">
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
                                    <select id="advSize" name="pagesize">
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
                                    <select name="orientation">
                                        <option value="auto" selected>Auto</option>
                                        <option value="3">Portrait</option>
                                        <option value="4">Landscape</option>
                                    </select>
                                </div>
                                <div>
                                    <label class="label">Scaling</label>
                                    <select name="fitpage">
                                        <option value="true" selected>Fit to Page</option>
                                        <option value="false">100% Scale</option>
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
                    <span class="card-heading">System Information</span>
                    <span style="font-size: 12px; color: var(--text-tertiary);">Armbian / Debian 13</span>
                </div>
                <div class="info-list">
                    <div class="info-item">
                        <div class="info-label">Hostname</div>
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
        <span><a href="https://printserver.local:631" target="_blank">CUPS Admin</a> • <a href="https://github.com/Sheesh0327/armbian-print-vpn-server" target="_blank">GitHub</a></span>
    </footer>

    <script>
        pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

        let currentPdf = null;
        let pdfPage = 1;
        let pdfPagesTotal = 1;
        let mode = 'simple';

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
        const previewDetails = document.getElementById('previewDetails');
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
            fileBadge.textContent = formatBytes(f.size);
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
                        previewDetails.textContent = `${imgPreview.naturalWidth} × ${imgPreview.naturalHeight}px`;
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
                    });
                };
                reader.readAsArrayBuffer(f);
            } else {
                txtPreview.style.display = 'block';
                txtPreview.textContent = 'Ready to print.';
            }
        }

        function renderPdf(num) {
            if (!currentPdf) return;
            currentPdf.getPage(num).then(page => {
                const viewport = page.getViewport({ scale: 1.0 });
                pdfCanvas.height = viewport.height;
                pdfCanvas.width = viewport.width;
                pdfCanvas.style.display = 'block';
                page.render({
                    canvasContext: pdfCanvas.getContext('2d'),
                    viewport: viewport
                });
                pageIndicator.textContent = `${num} / ${pdfPagesTotal}`;
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

        function setMode(m) {
            mode = m;
            document.getElementById('btnSimple').classList.toggle('active', m === 'simple');
            document.getElementById('btnAdv').classList.toggle('active', m === 'advanced');
            document.getElementById('secSimple').style.display = m === 'simple' ? 'block' : 'none';
            document.getElementById('secAdv').style.display = m === 'advanced' ? 'block' : 'none';
        }

        function syncSize(val) {
            document.getElementById('advSize').value = val;
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

    suffix = os.path.splitext(file.filename)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

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
    port = int(os.environ.get('PORT', 8080))
    app.run(host='::', port=port)
