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
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ApexPrint Studio - B860H Home Print Server</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
    <style>
        :root {
            --bg-base: #080c14;
            --bg-surface: #0f1626;
            --bg-card: #151e34;
            --bg-card-hover: #1c2744;
            --border: #233152;
            --border-focus: #3b82f6;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --primary-glow: rgba(59, 130, 246, 0.35);
            --accent: #06b6d4;
            --accent-glow: rgba(6, 182, 212, 0.35);
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --text-sub: #64748b;
            --radius-lg: 18px;
            --radius-md: 12px;
            --radius-sm: 8px;
            --shadow-card: 0 16px 36px -8px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.05);
            --transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg-base);
            background-image: 
                radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(6, 182, 212, 0.08) 0px, transparent 50%);
            color: var(--text-main);
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            overflow-x: hidden;
        }

        /* Top Navigation Header */
        header {
            background: rgba(15, 22, 38, 0.75);
            backdrop-filter: blur(16px);
            border-bottom: 1px solid var(--border);
            padding: 16px 28px;
            position: sticky;
            top: 0;
            z-index: 50;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 12px;
            text-decoration: none;
            color: var(--text-main);
        }

        .brand-icon {
            width: 40px;
            height: 40px;
            background: linear-gradient(135deg, var(--primary), var(--accent));
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.3rem;
            box-shadow: 0 4px 14px var(--primary-glow);
        }

        .brand-text h1 {
            font-size: 1.15rem;
            font-weight: 800;
            letter-spacing: -0.02em;
            background: linear-gradient(to right, #fff, #93c5fd);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .brand-text span {
            font-size: 0.72rem;
            color: var(--text-muted);
            letter-spacing: 0.05em;
            text-transform: uppercase;
            font-weight: 600;
        }

        .header-controls {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .nav-tabs {
            display: flex;
            background: rgba(8, 12, 20, 0.6);
            padding: 4px;
            border-radius: var(--radius-md);
            border: 1px solid var(--border);
        }

        .nav-tab {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-family: inherit;
            font-size: 0.82rem;
            font-weight: 600;
            padding: 8px 16px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            transition: var(--transition);
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .nav-tab.active {
            background: var(--primary);
            color: #fff;
            box-shadow: 0 2px 8px var(--primary-glow);
        }

        .status-pill {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: var(--success);
            font-size: 0.78rem;
            font-weight: 600;
            padding: 6px 12px;
            border-radius: 9999px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            background: var(--success);
            border-radius: 50%;
            box-shadow: 0 0 10px var(--success);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.4; transform: scale(0.85); }
        }

        /* Main Workspace Container */
        main {
            flex: 1;
            max-width: 1320px;
            width: 100%;
            margin: 0 auto;
            padding: 28px 24px 60px;
        }

        .tab-pane {
            display: none;
        }
        .tab-pane.active {
            display: block;
            animation: fadeIn 0.3s ease;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(6px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Two-Column Print Studio Grid */
        .studio-grid {
            display: grid;
            grid-template-columns: 1.15fr 1fr;
            gap: 28px;
            align-items: start;
        }

        @media (max-width: 992px) {
            .studio-grid { grid-template-columns: 1fr; }
            .header-controls { flex-direction: column-reverse; align-items: flex-end; gap: 8px; }
        }

        /* Card Container Styles */
        .glass-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: 26px;
            box-shadow: var(--shadow-card);
            backdrop-filter: blur(8px);
        }

        .card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 14px;
        }

        .card-title {
            font-size: 1.05rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
            color: var(--text-main);
        }

        /* Dropzone / Upload Area */
        .dropzone-wrapper {
            position: relative;
        }

        .dropzone {
            border: 2px dashed var(--border);
            border-radius: var(--radius-md);
            background: rgba(8, 12, 20, 0.5);
            padding: 44px 24px;
            text-align: center;
            cursor: pointer;
            transition: var(--transition);
            position: relative;
            overflow: hidden;
        }

        .dropzone:hover, .dropzone.dragover {
            border-color: var(--primary);
            background: rgba(59, 130, 246, 0.05);
            transform: scale(1.005);
        }

        .dropzone-icon {
            width: 60px;
            height: 60px;
            background: rgba(59, 130, 246, 0.1);
            color: var(--primary);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.8rem;
            margin: 0 auto 16px;
            box-shadow: 0 4px 16px rgba(59, 130, 246, 0.15);
        }

        .dropzone h3 {
            font-size: 1.05rem;
            font-weight: 700;
            margin-bottom: 6px;
        }

        .dropzone p {
            color: var(--text-muted);
            font-size: 0.85rem;
        }

        .supported-tags {
            display: flex;
            justify-content: center;
            gap: 6px;
            margin-top: 14px;
            flex-wrap: wrap;
        }

        .tag {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.68rem;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border);
            padding: 3px 8px;
            border-radius: 6px;
            color: var(--text-sub);
        }

        /* Document Preview Component */
        .preview-container {
            display: none;
            margin-top: 20px;
            border-radius: var(--radius-md);
            border: 1px solid var(--border);
            background: #080c14;
            overflow: hidden;
        }

        .preview-toolbar {
            background: var(--bg-surface);
            padding: 10px 16px;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 0.82rem;
        }

        .preview-meta {
            display: flex;
            align-items: center;
            gap: 12px;
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
        }

        .preview-filename {
            color: var(--accent);
            font-weight: 600;
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .preview-actions {
            display: flex;
            gap: 8px;
        }

        .btn-icon {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-muted);
            width: 28px;
            height: 28px;
            border-radius: 6px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: var(--transition);
        }
        .btn-icon:hover {
            color: #fff;
            border-color: var(--text-muted);
        }

        .preview-body {
            min-height: 280px;
            max-height: 480px;
            overflow: auto;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 16px;
            background: #060910;
        }

        .preview-body img {
            max-width: 100%;
            max-height: 440px;
            border-radius: 6px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.6);
        }

        #pdfCanvas {
            max-width: 100%;
            height: auto;
            border-radius: 4px;
            box-shadow: 0 6px 24px rgba(0, 0, 0, 0.7);
        }

        .text-preview {
            width: 100%;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
            color: #93c5fd;
            white-space: pre-wrap;
            padding: 12px;
            max-height: 380px;
            overflow-y: auto;
        }

        /* Mode Switcher Toggle */
        .mode-switch-container {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: rgba(8, 12, 20, 0.6);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 6px 12px;
            margin-bottom: 22px;
        }

        .mode-label {
            font-size: 0.84rem;
            font-weight: 700;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .mode-tabs {
            display: flex;
            gap: 4px;
            background: #080c14;
            padding: 3px;
            border-radius: var(--radius-sm);
        }

        .mode-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 6px 14px;
            font-size: 0.8rem;
            font-weight: 600;
            border-radius: 6px;
            cursor: pointer;
            transition: var(--transition);
        }

        .mode-btn.active {
            background: var(--bg-card);
            color: #fff;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4);
        }

        /* Form Controls & Grids */
        .form-section {
            margin-bottom: 20px;
        }

        .section-label {
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-sub);
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .form-group {
            margin-bottom: 16px;
        }

        .form-label {
            display: block;
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 6px;
        }

        select, input[type="text"], input[type="number"] {
            width: 100%;
            background: rgba(8, 12, 20, 0.7);
            border: 1px solid var(--border);
            color: var(--text-main);
            padding: 10px 14px;
            border-radius: var(--radius-sm);
            font-family: inherit;
            font-size: 0.9rem;
            outline: none;
            transition: var(--transition);
        }

        select:focus, input:focus {
            border-color: var(--border-focus);
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
        }

        .grid-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }

        .grid-3 {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 10px;
        }

        /* Pill Selector Buttons (Color / Duplex) */
        .pill-group {
            display: flex;
            gap: 8px;
        }

        .pill-radio {
            flex: 1;
            position: relative;
        }

        .pill-radio input {
            position: absolute;
            opacity: 0;
            width: 0;
            height: 0;
        }

        .pill-label {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 9px 12px;
            background: rgba(8, 12, 20, 0.6);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--text-muted);
            cursor: pointer;
            transition: var(--transition);
            text-align: center;
        }

        .pill-radio input:checked + .pill-label {
            background: rgba(59, 130, 246, 0.15);
            border-color: var(--primary);
            color: #fff;
            box-shadow: 0 0 12px rgba(59, 130, 246, 0.2);
        }

        /* Number Stepper Control */
        .stepper-control {
            display: flex;
            align-items: center;
            background: rgba(8, 12, 20, 0.7);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            overflow: hidden;
        }

        .stepper-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            width: 38px;
            height: 38px;
            font-size: 1.1rem;
            cursor: pointer;
            transition: var(--transition);
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .stepper-btn:hover {
            color: #fff;
            background: rgba(255, 255, 255, 0.05);
        }

        .stepper-input {
            flex: 1;
            border: none !important;
            text-align: center;
            font-weight: 700;
            background: transparent !important;
            padding: 8px 0;
        }

        /* Primary Print Button */
        .btn-print-main {
            width: 100%;
            background: linear-gradient(135deg, var(--primary), #2563eb);
            color: #fff;
            border: none;
            padding: 16px 24px;
            border-radius: var(--radius-md);
            font-size: 1.05rem;
            font-weight: 700;
            letter-spacing: -0.01em;
            cursor: pointer;
            box-shadow: 0 8px 24px var(--primary-glow);
            transition: var(--transition);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            margin-top: 24px;
        }

        .btn-print-main:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 28px rgba(59, 130, 246, 0.45);
        }

        .btn-print-main:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }

        /* Floating Status Notification */
        .toast-banner {
            margin-top: 18px;
            padding: 14px 18px;
            border-radius: var(--radius-md);
            font-size: 0.88rem;
            line-height: 1.4;
            display: none;
            align-items: center;
            gap: 12px;
            animation: slideUp 0.3s ease;
        }

        @keyframes slideUp {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .toast-banner.success {
            background: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: #34d399;
        }

        .toast-banner.error {
            background: rgba(239, 68, 68, 0.12);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #f87171;
        }

        /* Queue & History Table */
        .jobs-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.88rem;
            margin-top: 12px;
        }

        .jobs-table th {
            text-align: left;
            color: var(--text-sub);
            padding: 12px 16px;
            font-weight: 700;
            text-transform: uppercase;
            font-size: 0.72rem;
            letter-spacing: 0.05em;
            border-bottom: 1px solid var(--border);
        }

        .jobs-table td {
            padding: 14px 16px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            color: var(--text-main);
        }

        .jobs-table tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }

        .job-id {
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
            color: var(--accent);
        }

        .job-state-badge {
            font-size: 0.72rem;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 9999px;
            text-transform: uppercase;
        }
        .job-state-badge.completed {
            background: rgba(16, 185, 129, 0.15);
            color: var(--success);
        }
        .job-state-badge.active {
            background: rgba(59, 130, 246, 0.2);
            color: #60a5fa;
            animation: pulse 1.5s infinite;
        }

        .btn-cancel {
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #f87171;
            padding: 4px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.75rem;
            font-weight: 600;
            transition: var(--transition);
        }
        .btn-cancel:hover {
            background: var(--danger);
            color: #fff;
        }

        /* Telemetry Cards */
        .telemetry-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 18px;
            margin-top: 14px;
        }

        .stat-card {
            background: rgba(8, 12, 20, 0.6);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 20px;
        }

        .stat-label {
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--text-sub);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 6px;
        }

        .stat-value {
            font-size: 1.4rem;
            font-weight: 800;
            color: var(--text-main);
            font-family: 'JetBrains Mono', monospace;
        }

        .stat-desc {
            font-size: 0.78rem;
            color: var(--text-muted);
            margin-top: 4px;
        }

        /* Footer */
        footer {
            border-top: 1px solid var(--border);
            padding: 20px;
            text-align: center;
            font-size: 0.8rem;
            color: var(--text-sub);
            margin-top: auto;
        }

        footer a {
            color: var(--primary);
            text-decoration: none;
        }
        footer a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>

    <!-- Header Navigation -->
    <header>
        <a href="/" class="brand">
            <div class="brand-icon">🖨️</div>
            <div class="brand-text">
                <h1>ApexPrint Studio</h1>
                <span>ZTE B860H Print Server</span>
            </div>
        </a>

        <div class="header-controls">
            <div class="status-pill" id="printerStatusBadge">
                <div class="status-dot"></div>
                <span id="printerStatusText">Checking Printer...</span>
            </div>

            <div class="nav-tabs">
                <button class="nav-tab active" onclick="switchMainTab('studio')">Print Studio</button>
                <button class="nav-tab" onclick="switchMainTab('jobs')">Queue & History</button>
                <button class="nav-tab" onclick="switchMainTab('telemetry')">Server Telemetry</button>
            </div>
        </div>
    </header>

    <main>
        <!-- TAB 1: PRINT STUDIO -->
        <div id="tab-studio" class="tab-pane active">
            <form id="printForm">
                <div class="studio-grid">
                    
                    <!-- LEFT COLUMN: File Upload & Interactive Live Preview -->
                    <div class="glass-card">
                        <div class="card-header">
                            <div class="card-title">
                                <span>📄</span> Document & Photo Source
                            </div>
                            <span class="tag" id="fileSizeBadge" style="display:none;">0 KB</span>
                        </div>

                        <div class="dropzone-wrapper">
                            <input type="file" id="fileInput" name="file" style="display: none;" accept=".pdf,.png,.jpg,.jpeg,.txt,.doc,.docx">
                            <div class="dropzone" id="dropzone">
                                <div class="dropzone-icon">📥</div>
                                <h3>Drop file here or click to browse</h3>
                                <p>Supports documents, high-res photos, receipts & labels</p>
                                <div class="supported-tags">
                                    <span class="tag">PDF</span>
                                    <span class="tag">PNG</span>
                                    <span class="tag">JPG</span>
                                    <span class="tag">TXT</span>
                                    <span class="tag">DOCX</span>
                                </div>
                            </div>
                        </div>

                        <!-- Live Preview Panel -->
                        <div class="preview-container" id="previewContainer">
                            <div class="preview-toolbar">
                                <div class="preview-meta">
                                    <span class="preview-filename" id="previewFilename">document.pdf</span>
                                    <span id="previewDimensions"></span>
                                    <span id="previewPageCount"></span>
                                </div>
                                <div class="preview-actions">
                                    <button type="button" class="btn-icon" id="btnPrevPage" title="Previous Page" style="display:none;">◀</button>
                                    <button type="button" class="btn-icon" id="btnNextPage" title="Next Page" style="display:none;">▶</button>
                                    <button type="button" class="btn-icon" onclick="clearSelectedFile()" title="Remove File">✕</button>
                                </div>
                            </div>
                            <div class="preview-body" id="previewBody">
                                <img id="imagePreview" style="display:none;" alt="Print Preview">
                                <canvas id="pdfCanvas" style="display:none;"></canvas>
                                <div id="textPreview" class="text-preview" style="display:none;"></div>
                            </div>
                        </div>
                    </div>

                    <!-- RIGHT COLUMN: Printing Options & Mode Switcher -->
                    <div class="glass-card">
                        <!-- Mode Switcher -->
                        <div class="mode-switch-container">
                            <div class="mode-label">
                                <span>⚙️</span> Configuration Mode
                            </div>
                            <div class="mode-tabs">
                                <button type="button" class="mode-btn active" id="btnModeSimple" onclick="setMode('simple')">Simple</button>
                                <button type="button" class="mode-btn" id="btnModeAdvanced" onclick="setMode('advanced')">Advanced</button>
                            </div>
                        </div>

                        <!-- Printer Selection -->
                        <div class="form-group">
                            <label class="form-label" for="printerSelect">Destination Device</label>
                            <select id="printerSelect" name="printer" required>
                                <option value="" disabled selected>Detecting printers...</option>
                            </select>
                        </div>

                        <!-- COMMON OPTIONS: Copies & Color Mode -->
                        <div class="form-section">
                            <div class="grid-2">
                                <div class="form-group">
                                    <label class="form-label">Copies</label>
                                    <div class="stepper-control">
                                        <button type="button" class="stepper-btn" onclick="adjustCopies(-1)">−</button>
                                        <input type="number" id="copiesInput" name="copies" class="stepper-input" value="1" min="1" max="99">
                                        <button type="button" class="stepper-btn" onclick="adjustCopies(1)">+</button>
                                    </div>
                                </div>

                                <div class="form-group">
                                    <label class="form-label">Color Mode</label>
                                    <div class="pill-group">
                                        <label class="pill-radio">
                                            <input type="radio" name="colormodel" value="RGB" checked>
                                            <span class="pill-label">🎨 Color</span>
                                        </label>
                                        <label class="pill-radio">
                                            <input type="radio" name="colormodel" value="KGray">
                                            <span class="pill-label">⚫ B&W</span>
                                        </label>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- SIMPLE MODE OPTIONS: Quick Paper Size -->
                        <div id="simpleOptions">
                            <div class="form-group">
                                <label class="form-label">Paper Size</label>
                                <select id="simplePageSize" name="simple_pagesize" onchange="syncPageSize(this.value)">
                                    <option value="A4" selected>A4 (Standard 210 × 297 mm)</option>
                                    <option value="Letter">Letter (8.5 × 11 in)</option>
                                    <option value="Legal">Legal (8.5 × 14 in)</option>
                                    <option value="4x6in">4 × 6 in (Photo Card)</option>
                                    <option value="5x7in">5 × 7 in (Photo Card)</option>
                                </select>
                            </div>
                        </div>

                        <!-- ADVANCED MODE OPTIONS: Professional Fine-Tuning -->
                        <div id="advancedOptions" style="display: none;">
                            
                            <!-- Media & Stock -->
                            <div class="form-section">
                                <div class="section-label">📋 Paper & Stock Selection</div>
                                <div class="grid-2">
                                    <div class="form-group">
                                        <label class="form-label">Paper Format</label>
                                        <select id="advPageSize" name="pagesize">
                                            <option value="A4" selected>A4 (210 × 297 mm)</option>
                                            <option value="Letter">Letter (8.5 × 11 in)</option>
                                            <option value="Legal">Legal (8.5 × 14 in)</option>
                                            <option value="A5">A5 (148 × 210 mm)</option>
                                            <option value="A6">A6 (105 × 148 mm)</option>
                                            <option value="B5">B5 (176 × 250 mm)</option>
                                            <option value="4x6in">4 × 6 in Photo</option>
                                            <option value="5x7in">5 × 7 in Photo</option>
                                            <option value="8x10">8 × 10 in Photo</option>
                                            <option value="EnvelopeDL">Envelope DL</option>
                                            <option value="Envelope#10">Envelope #10</option>
                                        </select>
                                    </div>
                                    <div class="form-group">
                                        <label class="form-label">Media Type</label>
                                        <select id="advMediaType" name="mediatype">
                                            <option value="Plain" selected>Standard Plain Paper</option>
                                            <option value="Glossy">HP Premium Glossy Photo</option>
                                            <option value="FastGlossy">Everyday Photo Paper</option>
                                            <option value="CoatedMatte">Matte Presentation Paper</option>
                                            <option value="CoatedBrochure">Heavyweight Brochure Paper</option>
                                            <option value="PlainPaperLight">Lightweight / Eco Paper</option>
                                        </select>
                                    </div>
                                </div>
                            </div>

                            <!-- Quality & Duplex -->
                            <div class="form-section">
                                <div class="section-label">⚡ Precision & Layout</div>
                                <div class="grid-2">
                                    <div class="form-group">
                                        <label class="form-label">Print Quality</label>
                                        <select id="advQuality" name="quality">
                                            <option value="Normal" selected>Normal (Standard 600 DPI)</option>
                                            <option value="Best">Best (High Resolution 1200 DPI)</option>
                                            <option value="Photo">Photo (Max Quality / Deep Ink)</option>
                                        </select>
                                    </div>
                                    <div class="form-group">
                                        <label class="form-label">Double-Sided (Duplex)</label>
                                        <select id="advDuplex" name="duplex">
                                            <option value="None" selected>Off (1-Sided)</option>
                                            <option value="DuplexNoTumble">2-Sided (Book Long-edge)</option>
                                            <option value="DuplexTumble">2-Sided (Calendar Short-edge)</option>
                                        </select>
                                    </div>
                                </div>
                            </div>

                            <!-- Orientation & Scaling -->
                            <div class="form-section">
                                <div class="grid-3">
                                    <div class="form-group">
                                        <label class="form-label">Orientation</label>
                                        <select id="advOrientation" name="orientation">
                                            <option value="auto" selected>Auto</option>
                                            <option value="3">Portrait</option>
                                            <option value="4">Landscape</option>
                                        </select>
                                    </div>
                                    <div class="form-group">
                                        <label class="form-label">Scaling</label>
                                        <select id="advScaling" name="fitpage">
                                            <option value="true" selected>Fit to Page</option>
                                            <option value="false">Actual Size (100%)</option>
                                        </select>
                                    </div>
                                    <div class="form-group">
                                        <label class="form-label">Pages / Sheet</label>
                                        <select id="advNumberUp" name="numberup">
                                            <option value="1" selected>1-Up (Normal)</option>
                                            <option value="2">2-Up (Side-by-side)</option>
                                            <option value="4">4-Up (Grid)</option>
                                        </select>
                                    </div>
                                </div>

                                <div class="form-group">
                                    <label class="form-label">Page Range <span style="font-weight: 400; color: var(--text-sub);">(e.g. 1-3, 5)</span></label>
                                    <input type="text" id="advPageRange" name="pagerange" placeholder="Leave empty for all pages">
                                </div>
                            </div>

                        </div>

                        <!-- Print Action Button -->
                        <button type="submit" class="btn-print-main" id="btnSubmit">
                            <span>🖨️</span> Print Document
                        </button>

                        <!-- Notification Banner -->
                        <div class="toast-banner" id="statusToast"></div>
                    </div>

                </div>
            </form>
        </div>

        <!-- TAB 2: QUEUE & HISTORY -->
        <div id="tab-jobs" class="tab-pane">
            <div class="glass-card">
                <div class="card-header">
                    <div class="card-title">
                        <span>📋</span> Print Queue & Job History
                    </div>
                    <button class="nav-tab" style="background: rgba(255,255,255,0.05);" onclick="refreshJobs()">↻ Refresh</button>
                </div>
                <div style="overflow-x: auto;">
                    <table class="jobs-table">
                        <thead>
                            <tr>
                                <th>Job ID</th>
                                <th>Printer</th>
                                <th>User</th>
                                <th>Payload Size</th>
                                <th>Date & Time</th>
                                <th>Status</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody id="jobsTableBody">
                            <tr>
                                <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 30px;">Loading print queue...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- TAB 3: SERVER TELEMETRY -->
        <div id="tab-telemetry" class="tab-pane">
            <div class="glass-card">
                <div class="card-header">
                    <div class="card-title">
                        <span>📊</span> Server Telemetry & System Vitals
                    </div>
                    <span class="tag">B860H Armbian Core</span>
                </div>

                <div class="telemetry-grid">
                    <div class="stat-card">
                        <div class="stat-label">System Memory</div>
                        <div class="stat-value" id="statRam">-- MB</div>
                        <div class="stat-desc" id="statRamDesc">Free headroom available</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">CPU Load Average</div>
                        <div class="stat-value" id="statLoad">--</div>
                        <div class="stat-desc">Ultra-low thermal footprint</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">System Uptime</div>
                        <div class="stat-value" id="statUptime">--</div>
                        <div class="stat-desc">Continuous 24/7 server state</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">mDNS Hostname</div>
                        <div class="stat-value" style="font-size: 1.1rem; color: var(--accent);">printserver.local</div>
                        <div class="stat-desc">Zero-config LAN Discovery</div>
                    </div>
                </div>

                <div style="margin-top: 24px; padding: 18px; background: rgba(8, 12, 20, 0.5); border-radius: var(--radius-md); border: 1px solid var(--border);">
                    <h4 style="font-size: 0.95rem; margin-bottom: 8px;">Hardware Connection</h4>
                    <p style="color: var(--text-muted); font-size: 0.85rem;" id="hardwareDesc">Scanning USB connection...</p>
                </div>
            </div>
        </div>
    </main>

    <!-- Footer -->
    <footer>
        <p>ApexPrint Studio • Running on ZTE B860H (Armbian Linux) • <a href="https://printserver.local:631" target="_blank">CUPS Admin (:631)</a> • <a href="https://github.com/Sheesh0327/armbian-print-vpn-server" target="_blank">GitHub Repository</a></p>
    </footer>

    <!-- Frontend Interactive Script -->
    <script>
        pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

        let currentPdf = null;
        let pdfPageNum = 1;
        let pdfTotalPages = 1;
        let currentMode = 'simple';

        // DOM elements
        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('fileInput');
        const previewContainer = document.getElementById('previewContainer');
        const previewFilename = document.getElementById('previewFilename');
        const previewDimensions = document.getElementById('previewDimensions');
        const previewPageCount = document.getElementById('previewPageCount');
        const fileSizeBadge = document.getElementById('fileSizeBadge');
        const imagePreview = document.getElementById('imagePreview');
        const pdfCanvas = document.getElementById('pdfCanvas');
        const textPreview = document.getElementById('textPreview');
        const btnPrevPage = document.getElementById('btnPrevPage');
        const btnNextPage = document.getElementById('btnNextPage');
        const statusToast = document.getElementById('statusToast');
        const btnSubmit = document.getElementById('btnSubmit');

        // Drag and drop event listeners
        dropzone.addEventListener('click', () => fileInput.click());
        dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) {
                fileInput.files = e.dataTransfer.files;
                handleFileSelect();
            }
        });
        fileInput.addEventListener('change', handleFileSelect);

        function handleFileSelect() {
            if (!fileInput.files.length) return;
            const file = fileInput.files[0];
            
            previewFilename.textContent = file.name;
            fileSizeBadge.style.display = 'inline-block';
            fileSizeBadge.textContent = formatBytes(file.size);
            previewContainer.style.display = 'block';

            // Reset preview slots
            imagePreview.style.display = 'none';
            pdfCanvas.style.display = 'none';
            textPreview.style.display = 'none';
            btnPrevPage.style.display = 'none';
            btnNextPage.style.display = 'none';
            previewDimensions.textContent = '';
            previewPageCount.textContent = '';

            const fileExt = file.name.split('.').pop().toLowerCase();

            if (['png', 'jpg', 'jpeg', 'webp', 'svg'].includes(fileExt)) {
                // Image preview
                const reader = new FileReader();
                reader.onload = (e) => {
                    imagePreview.src = e.target.result;
                    imagePreview.style.display = 'block';
                    imagePreview.onload = () => {
                        previewDimensions.textContent = `${imagePreview.naturalWidth} × ${imagePreview.naturalHeight} px`;
                    };
                };
                reader.readAsDataURL(file);
            } else if (fileExt === 'pdf') {
                // PDF preview via PDF.js
                const reader = new FileReader();
                reader.onload = function(e) {
                    const typedarray = new Uint8Array(e.target.result);
                    pdfjsLib.getDocument(typedarray).promise.then(pdf => {
                        currentPdf = pdf;
                        pdfTotalPages = pdf.numPages;
                        pdfPageNum = 1;
                        previewPageCount.textContent = `${pdfTotalPages} ${pdfTotalPages > 1 ? 'Pages' : 'Page'}`;
                        if (pdfTotalPages > 1) {
                            btnPrevPage.style.display = 'inline-flex';
                            btnNextPage.style.display = 'inline-flex';
                        }
                        renderPdfPage(pdfPageNum);
                    }).catch(err => {
                        console.warn('PDF.js render fallback:', err);
                        textPreview.style.display = 'block';
                        textPreview.textContent = 'PDF ready to print: ' + file.name;
                    });
                };
                reader.readAsArrayBuffer(file);
            } else if (['txt', 'log', 'csv', 'md'].includes(fileExt)) {
                // Text preview
                const reader = new FileReader();
                reader.onload = (e) => {
                    textPreview.style.display = 'block';
                    textPreview.textContent = e.target.result.slice(0, 3000);
                };
                reader.readAsText(file);
            } else {
                // Generic doc
                textPreview.style.display = 'block';
                textPreview.textContent = `Document (${fileExt.toUpperCase()}) queued for printing.`;
            }
        }

        function renderPdfPage(num) {
            if (!currentPdf) return;
            currentPdf.getPage(num).then(page => {
                const viewport = page.getViewport({ scale: 1.2 });
                pdfCanvas.height = viewport.height;
                pdfCanvas.width = viewport.width;
                pdfCanvas.style.display = 'block';
                const renderContext = {
                    canvasContext: pdfCanvas.getContext('2d'),
                    viewport: viewport
                };
                page.render(renderContext);
                previewPageCount.textContent = `Page ${num} of ${pdfTotalPages}`;
            });
        }

        btnPrevPage.addEventListener('click', () => {
            if (pdfPageNum > 1) {
                pdfPageNum--;
                renderPdfPage(pdfPageNum);
            }
        });

        btnNextPage.addEventListener('click', () => {
            if (pdfPageNum < pdfTotalPages) {
                pdfPageNum++;
                renderPdfPage(pdfPageNum);
            }
        });

        function clearSelectedFile() {
            fileInput.value = '';
            previewContainer.style.display = 'none';
            fileSizeBadge.style.display = 'none';
            currentPdf = null;
        }

        // Mode Switching
        function setMode(mode) {
            currentMode = mode;
            const btnSimple = document.getElementById('btnModeSimple');
            const btnAdv = document.getElementById('btnModeAdvanced');
            const simpleOpts = document.getElementById('simpleOptions');
            const advOpts = document.getElementById('advancedOptions');

            if (mode === 'simple') {
                btnSimple.classList.add('active');
                btnAdv.classList.remove('active');
                simpleOpts.style.display = 'block';
                advOpts.style.display = 'none';
            } else {
                btnAdv.classList.add('active');
                btnSimple.classList.remove('active');
                simpleOpts.style.display = 'none';
                advOpts.style.display = 'block';
            }
        }

        function syncPageSize(val) {
            document.getElementById('advPageSize').value = val;
        }

        function adjustCopies(delta) {
            const input = document.getElementById('copiesInput');
            let val = parseInt(input.value) || 1;
            val = Math.max(1, Math.min(99, val + delta));
            input.value = val;
        }

        function switchMainTab(tab) {
            document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
            
            const targetPane = document.getElementById('tab-' + tab);
            if (targetPane) targetPane.classList.add('active');

            const activeBtn = Array.from(document.querySelectorAll('.nav-tab')).find(b => b.textContent.toLowerCase().includes(tab === 'studio' ? 'studio' : (tab === 'jobs' ? 'queue' : 'telemetry')));
            if (activeBtn) activeBtn.classList.add('active');

            if (tab === 'jobs') refreshJobs();
            if (tab === 'telemetry') fetchTelemetry();
        }

        // Submit Print Job
        document.getElementById('printForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!fileInput.files.length) {
                showToast('Please select a file to print first.', 'error');
                return;
            }

            btnSubmit.disabled = true;
            btnSubmit.innerHTML = '<span>⏳</span> Spooling to CUPS...';
            showToast('', '');

            const formData = new FormData(e.target);

            // Ensure pagesize is mapped based on mode
            if (currentMode === 'simple') {
                formData.set('pagesize', document.getElementById('simplePageSize').value);
                formData.set('mediatype', 'Plain');
                formData.set('quality', 'Normal');
                formData.set('duplex', 'None');
                formData.set('orientation', 'auto');
                formData.set('fitpage', 'true');
                formData.set('numberup', '1');
            }

            try {
                const res = await fetch('/api/print', { method: 'POST', body: formData });
                const data = await res.json();
                if (res.ok && data.success) {
                    showToast(`Job submitted successfully! (ID: ${data.job_id})`, 'success');
                    clearSelectedFile();
                    setTimeout(refreshJobs, 1000);
                } else {
                    showToast('Print Error: ' + (data.error || 'Unknown error'), 'error');
                }
            } catch (err) {
                showToast('Communication error: ' + err.message, 'error');
            } finally {
                btnSubmit.disabled = false;
                btnSubmit.innerHTML = '<span>🖨️</span> Print Document';
            }
        });

        function showToast(msg, type) {
            if (!msg) { statusToast.style.display = 'none'; return; }
            statusToast.textContent = msg;
            statusToast.className = 'toast-banner ' + type;
            statusToast.style.display = 'flex';
        }

        // Fetch Printers & Status
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                const select = document.getElementById('printerSelect');
                if (data.printers && data.printers.length) {
                    const currentVal = select.value;
                    select.innerHTML = '';
                    data.printers.forEach(p => {
                        const opt = document.createElement('option');
                        opt.value = p.name;
                        opt.textContent = `${p.name} (${p.state})`;
                        if (p.is_default) opt.selected = true;
                        select.appendChild(opt);
                    });
                    if (currentVal) select.value = currentVal;

                    const defaultP = data.printers.find(p => p.is_default) || data.printers[0];
                    document.getElementById('printerStatusText').textContent = `${defaultP.name} • ${defaultP.state}`;
                }

                if (data.telemetry) {
                    document.getElementById('statRam').textContent = data.telemetry.ram_used + ' / ' + data.telemetry.ram_total;
                    document.getElementById('statLoad').textContent = data.telemetry.load;
                    document.getElementById('statUptime').textContent = data.telemetry.uptime;
                    document.getElementById('hardwareDesc').textContent = data.telemetry.usb_printer || 'Printer connected via USB';
                }
            } catch (e) {
                console.warn('Status poll failed:', e);
            }
        }

        // Fetch & Populate Jobs
        async function refreshJobs() {
            try {
                const res = await fetch('/api/jobs');
                const data = await res.json();
                const tbody = document.getElementById('jobsTableBody');

                if (!data.jobs || !data.jobs.length) {
                    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 30px;">No recent or pending print jobs found.</td></tr>';
                    return;
                }

                tbody.innerHTML = '';
                data.jobs.forEach(j => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td class="job-id">${j.id}</td>
                        <td>${j.printer}</td>
                        <td>${j.user}</td>
                        <td>${formatBytes(j.bytes)}</td>
                        <td>${j.time}</td>
                        <td><span class="job-state-badge ${j.state.toLowerCase()}">${j.state}</span></td>
                        <td>${j.state.toLowerCase() === 'active' ? `<button class="btn-cancel" onclick="cancelJob('${j.id}')">Cancel</button>` : '—'}</td>
                    `;
                    tbody.appendChild(tr);
                });
            } catch (e) {
                console.warn('Jobs fetch failed:', e);
            }
        }

        async function cancelJob(jobId) {
            if (!confirm(`Cancel print job ${jobId}?`)) return;
            try {
                const res = await fetch('/api/cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ job_id: jobId })
                });
                const data = await res.json();
                if (data.success) {
                    refreshJobs();
                } else {
                    alert('Error cancelling job: ' + (data.error || 'Unknown error'));
                }
            } catch (e) {
                alert('Connection error: ' + e.message);
            }
        }

        function formatBytes(bytes) {
            if (!bytes || bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
        }

        // Initial setup
        fetchStatus();
        setInterval(fetchStatus, 8000);
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
    except Exception as e:
        printers.append({'name': 'HP_Smart_Tank_520', 'state': 'Idle', 'is_default': True})
    return printers

def get_telemetry():
    telemetry = {
        'ram_used': '--',
        'ram_total': '--',
        'load': '--',
        'uptime': '--',
        'usb_printer': 'HP Smart Tank 520 (USB)'
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
            
        usb = subprocess.check_output(['lsusb'], text=True)
        for line in usb.split('\n'):
            if 'HP' in line or 'Smart Tank' in line or 'Printer' in line:
                telemetry['usb_printer'] = line.strip()
                break
    except Exception:
        pass
    return telemetry

def parse_jobs():
    jobs = []
    try:
        # Active jobs
        active_raw = subprocess.check_output(['lpstat', '-o'], credentials=None, text=True)
        for line in active_raw.strip().split('\n'):
            if not line.strip(): continue
            parts = line.split()
            if len(parts) >= 4:
                jobs.append({
                    'id': parts[0],
                    'printer': parts[0].rsplit('-', 1)[0],
                    'user': parts[1],
                    'bytes': int(parts[2]) if parts[2].isdigit() else 0,
                    'time': ' '.join(parts[3:]),
                    'state': 'Active'
                })

        # Completed jobs
        comp_raw = subprocess.check_output(['lpstat', '-W', 'completed', '-o'], text=True)
        for line in comp_raw.strip().split('\n')[:15]:
            if not line.strip(): continue
            parts = line.split()
            if len(parts) >= 4:
                jobs.append({
                    'id': parts[0],
                    'printer': parts[0].rsplit('-', 1)[0],
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
    numberup = request.form.get('numberup', '1')
    pagerange = request.form.get('pagerange', '').strip()

    suffix = os.path.splitext(file.filename)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        cmd = ['lp', '-d', printer, '-n', str(copies)]

        # Media & Format options
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
        if numberup in ['2', '4']:
            cmd.extend(['-o', f'number-up={numberup}'])
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
