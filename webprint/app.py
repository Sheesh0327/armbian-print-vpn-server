import os
import subprocess
import tempfile
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Home Print Server</title>
    <style>
        :root {
            --bg: #0b0f19;
            --card: #151d30;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --text: #f1f5f9;
            --text-muted: #94a3b8;
            --border: #23304a;
            --success: #10b981;
            --error: #ef4444;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
        body { background: var(--bg); color: var(--text); display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
        .card { background: var(--card); border: 1px solid var(--border); border-radius: 20px; padding: 36px; width: 100%; max-width: 540px; box-shadow: 0 20px 40px rgba(0,0,0,0.5); }
        .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
        .badge { background: rgba(59, 130, 246, 0.2); color: #60a5fa; font-size: 0.75rem; font-weight: 600; padding: 4px 8px; border-radius: 6px; }
        h1 { font-size: 1.5rem; font-weight: 700; }
        p.subtitle { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 24px; line-height: 1.4; }
        .form-group { margin-bottom: 20px; }
        label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 8px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        select, input[type="number"] { width: 100%; background: #0c111d; border: 1px solid var(--border); color: var(--text); border-radius: 10px; padding: 12px 14px; font-size: 0.95rem; outline: none; transition: border-color 0.2s; }
        select:focus, input[type="number"]:focus { border-color: var(--primary); }
        .dropzone { border: 2px dashed var(--border); border-radius: 14px; padding: 40px 20px; text-align: center; cursor: pointer; transition: all 0.2s; background: #0c111d; margin-bottom: 20px; }
        .dropzone.dragover { border-color: var(--primary); background: rgba(59, 130, 246, 0.08); }
        .dropzone-icon { font-size: 2.4rem; margin-bottom: 8px; }
        .dropzone p { color: var(--text-muted); font-size: 0.9rem; }
        .dropzone strong { color: var(--primary); }
        .file-info { font-size: 0.9rem; color: #38bdf8; font-weight: 600; margin-top: 12px; display: none; word-break: break-all; }
        .options-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 24px; }
        button.btn-print { width: 100%; background: var(--primary); color: #fff; border: none; padding: 15px; font-size: 1.05rem; font-weight: 600; border-radius: 10px; cursor: pointer; transition: background 0.2s; display: flex; align-items: center; justify-content: center; gap: 8px; }
        button.btn-print:hover { background: var(--primary-hover); }
        button.btn-print:disabled { opacity: 0.5; cursor: not-allowed; }
        .status { margin-top: 18px; padding: 14px; border-radius: 10px; font-size: 0.9rem; display: none; text-align: center; line-height: 1.4; }
        .status.success { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
        .status.error { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
        .footer { margin-top: 24px; text-align: center; font-size: 0.8rem; color: var(--text-muted); }
        .footer a { color: var(--primary); text-decoration: none; }
        .footer a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <h1>Home Print Server</h1>
            <span class="badge">Armbian</span>
        </div>
        <p class="subtitle">Upload documents or photos to print instantly from any device on your local or VPN network.</p>

        <form id="printForm">
            <div class="form-group">
                <label for="printer">Select Destination Printer</label>
                <select id="printer" name="printer" required>
                    {% for p in printers %}
                        <option value="{{ p }}">{{ p }}</option>
                    {% endfor %}
                </select>
            </div>

            <div class="dropzone" id="dropzone">
                <div class="dropzone-icon">📄</div>
                <input type="file" id="fileInput" name="file" style="display: none;" accept=".pdf,.txt,.png,.jpg,.jpeg,.doc,.docx">
                <p><strong>Click to browse</strong> or drag & drop files here</p>
                <p style="font-size: 0.75rem; margin-top: 4px;">Supports PDF, JPG, PNG, TXT, DOCX</p>
                <div class="file-info" id="fileInfo"></div>
            </div>

            <div class="options-grid">
                <div>
                    <label for="copies">Copies</label>
                    <input type="number" id="copies" name="copies" value="1" min="1" max="99">
                </div>
                <div>
                    <label for="duplex">Sides (Duplex)</label>
                    <select id="duplex" name="duplex">
                        <option value="one-sided">1-Sided</option>
                        <option value="two-sided-long-edge">2-Sided (Long edge)</option>
                        <option value="two-sided-short-edge">2-Sided (Short edge)</option>
                    </select>
                </div>
            </div>

            <button type="submit" class="btn-print" id="submitBtn">
                <span>🖨️</span> Send to Printer
            </button>
            <div class="status" id="statusBox"></div>
        </form>

        <div class="footer">
            Administered via <a href="/admin-link" onclick="window.open(location.protocol + '//' + location.hostname + ':631', '_blank'); return false;">CUPS Web Interface (:631)</a>
        </div>
    </div>

    <script>
        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('fileInput');
        const fileInfo = document.getElementById('fileInfo');
        const form = document.getElementById('printForm');
        const submitBtn = document.getElementById('submitBtn');
        const statusBox = document.getElementById('statusBox');

        dropzone.addEventListener('click', () => fileInput.click());
        dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) {
                fileInput.files = e.dataTransfer.files;
                showSelectedFile();
            }
        });
        fileInput.addEventListener('change', showSelectedFile);

        function showSelectedFile() {
            if (fileInput.files.length > 0) {
                fileInfo.style.display = 'block';
                fileInfo.textContent = 'Selected: ' + fileInput.files[0].name + ' (' + (fileInput.files[0].size / 1024).toFixed(1) + ' KB)';
            }
        }

        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!fileInput.files.length) {
                showStatus('Please select a file to print.', 'error');
                return;
            }

            submitBtn.disabled = true;
            submitBtn.textContent = 'Spooling job to CUPS...';
            showStatus('', '');

            const formData = new FormData(form);

            try {
                const res = await fetch('/print', { method: 'POST', body: formData });
                const data = await res.json();
                if (res.ok && data.success) {
                    showStatus('Job successfully queued! ' + (data.job_id || ''), 'success');
                    form.reset();
                    fileInfo.style.display = 'none';
                } else {
                    showStatus('Print error: ' + (data.error || 'Unknown error'), 'error');
                }
            } catch (err) {
                showStatus('Network error connecting to print server: ' + err.message, 'error');
            } finally {
                submitBtn.disabled = false;
                submitBtn.innerHTML = '<span>🖨️</span> Send to Printer';
            }
        });

        function showStatus(msg, type) {
            if (!msg) { statusBox.style.display = 'none'; return; }
            statusBox.textContent = msg;
            statusBox.className = 'status ' + type;
            statusBox.style.display = 'block';
        }
    </script>
</body>
</html>"""

def get_printers():
    try:
        output = subprocess.check_output(['lpstat', '-e'], text=True)
        printers = [line.strip() for line in output.strip().split('\n') if line.strip()]
        return printers if printers else ['No printers configured in CUPS']
    except Exception:
        return ['CUPS service not responding']

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, printers=get_printers())

@app.route('/print', methods=['POST'])
def handle_print():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    printer = request.form.get('printer')
    copies = request.form.get('copies', '1')
    duplex = request.form.get('duplex', 'one-sided')

    suffix = os.path.splitext(file.filename)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        cmd = ['lp', '-d', printer, '-n', str(copies)]
        if duplex != 'one-sided':
            cmd.extend(['-o', f'sides={duplex}'])
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
