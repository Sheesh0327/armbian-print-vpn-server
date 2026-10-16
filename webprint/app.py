import os
import subprocess
import tempfile
import time
import json
import re
import sqlite3
import hashlib
import hmac
import secrets
import shutil
import glob
import ipaddress
import threading
from urllib.parse import quote, unquote
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, Response, send_file
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB file size limit

if os.name == 'nt' and 'WEBPRINT_DB' not in os.environ:
    base_dir = os.path.join(tempfile.gettempdir(), 'webprint_dev')
    DB_PATH = os.path.join(base_dir, 'webprint.db')
    SECRET_KEY_PATH = os.path.join(base_dir, 'secret.key')
    SPOOL_PINNED_DIR = os.path.join(base_dir, 'pinned')
    SPOOL_INBOX_DIR = os.path.join(base_dir, 'inbox')
    SPOOL_PREVIEWS_DIR = os.path.join(base_dir, 'previews')
    SEND_KEY_PATH = os.path.join(base_dir, 'send.key')
else:
    DB_PATH = os.environ.get('WEBPRINT_DB', '/var/lib/webprint/webprint.db')
    SECRET_KEY_PATH = os.environ.get('WEBPRINT_SECRET_KEY', '/var/lib/webprint/secret.key')
    SPOOL_PINNED_DIR = os.environ.get('WEBPRINT_SPOOL_PINNED', '/var/spool/webprint/pinned')
    SPOOL_INBOX_DIR = os.environ.get('WEBPRINT_SPOOL_INBOX', '/var/spool/webprint/inbox')
    SPOOL_PREVIEWS_DIR = os.environ.get('WEBPRINT_SPOOL_PREVIEWS', '/var/spool/webprint/previews')
    SEND_KEY_PATH = os.environ.get('WEBPRINT_SEND_KEY', '/etc/webprint/send.key')

CONVERSION_SEMAPHORE = threading.Semaphore(1)
MAX_PAGES_PER_JOB = int(os.environ.get('MAX_PAGES_PER_JOB', 50))

# Inbox: files that Wi-Fi guests send from the captive portal. The router relays them (guests never reach this server);
# nothing is printed automatically. The owner sees them in the admin console and decides.
ROUTER_IP = os.environ.get('ROUTER_IP', '10.0.0.1')
INBOX_MAX_BYTES = int(os.environ.get('INBOX_MAX_MB', 10)) * 1024 * 1024
INBOX_MAX_FILES = int(os.environ.get('INBOX_MAX_FILES', 50))
INBOX_TOTAL_BYTES = int(os.environ.get('INBOX_TOTAL_MB', 300)) * 1024 * 1024
INBOX_TTL_SECONDS = int(os.environ.get('INBOX_TTL_HOURS', 24)) * 3600

# ------------------------------------------------------------
# Trusted home devices only
# ------------------------------------------------------------
# Only these networks / single addresses may use this server at all (portal, API and admin). Set TRUSTED_NETWORKS in
# /etc/webprint/webprint.env, comma separated, e.g. "10.0.0.0/24,192.168.1.20,192.168.1.21". Loopback is always allowed.
# Guest Wi-Fi (10.0.30.0/24) is not in the default list. Anything that cannot be parsed is ignored, never widened.
DEFAULT_TRUSTED_NETWORKS = '10.0.0.0/24,192.168.1.0/24'

def parse_trusted_networks(raw):
    nets = []
    for part in (raw or '').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets

TRUSTED_NETWORKS = parse_trusted_networks(os.environ.get('TRUSTED_NETWORKS', DEFAULT_TRUSTED_NETWORKS))
LOOPBACK_NETWORKS = [ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128')]

def is_trusted_address(addr, networks=None):
    if not addr:
        return False
    if addr.startswith('::ffff:'):
        addr = addr[7:]
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for net in LOOPBACK_NETWORKS + (TRUSTED_NETWORKS if networks is None else networks):
        if ip.version == net.version and ip in net:
            return True
    return False

@app.before_request
def only_trusted_devices():
    if request.path == '/api/inbox/upload':
        return None  # the router relays guest files; that endpoint checks the router address and key itself
    if not is_trusted_address(request.remote_addr):
        return Response('403 Forbidden: this print server is only for trusted home devices.\n', status=403, mimetype='text/plain')

ALLOWED_EXTENSIONS = {
    '.pdf', '.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif',
    '.avif', '.tiff', '.tif', '.bmp', '.docx', '.doc', '.odt',
    '.rtf', '.txt', '.csv', '.xlsx', '.xls', '.pptx', '.ppt'
}

ALLOWED_PAGESIZES = {
    'A4', 'Letter', 'Legal', 'A5', 'A6', 'B5', '4x6', '4x6in', '5x7', '5x7in', '8x10', 'Envelope_DL', 'EnvelopeDL',
    'Custom.4x6in', 'EnvDL'
}
CANONICAL_PAGESIZES = {
    '4x6in': '4x6',
    '5x7in': '5x7',
    'EnvelopeDL': 'Envelope_DL',
    'EnvDL': 'Envelope_DL'
}

ALLOWED_MEDIATYPES = {'Plain', 'Glossy', 'FastGlossy', 'CoatedMatte', 'Matte', 'CoatedBrochure', 'Brochure'}
CANONICAL_MEDIATYPES = {
    'FastGlossy': 'Glossy',
    'CoatedMatte': 'Matte',
    'CoatedBrochure': 'Brochure'
}

ALLOWED_QUALITIES = {'Draft', 'Normal', 'Best', 'Photo'}
ALLOWED_COLORMODELS = {'RGB', 'CMYK', 'K', 'Gray', 'KGray', 'Color'}
ALLOWED_DUPLEX = {'None', 'DuplexNoTumble', 'DuplexTumble'}
ALLOWED_ORIENTATIONS = {'auto', '3', '4', 'portrait', 'landscape'}
ALLOWED_RESOLUTIONS = {'150', '300', '600', '1200'}
ALLOWED_MARGIN_MODES = {'printable', 'none', 'normal', 'custom'}

class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.history = {}

    def is_allowed(self, client_ip: str) -> bool:
        if not client_ip:
            client_ip = 'unknown'
        now = time.time()
        cutoff = now - self.window_seconds
        records = [t for t in self.history.get(client_ip, []) if t > cutoff]
        if len(records) >= self.max_requests:
            self.history[client_ip] = records
            return False
        records.append(now)
        self.history[client_ip] = records
        return True

    def reset(self, client_ip: str = None):
        if client_ip:
            self.history.pop(client_ip, None)
        else:
            self.history.clear()

admin_login_limiter = RateLimiter(max_requests=5, window_seconds=60)
pin_release_limiter = RateLimiter(max_requests=10, window_seconds=60)
public_print_limiter = RateLimiter(max_requests=10, window_seconds=60)

ACTIVE_TOKENS = {}

def verify_admin_token(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    expired = [t for t, exp in ACTIVE_TOKENS.items() if exp <= now]
    for t in expired:
        ACTIVE_TOKENS.pop(t, None)

    for active_token, exp in list(ACTIVE_TOKENS.items()):
        if hmac.compare_digest(token, active_token) and exp > now:
            return True
    return False

def get_or_create_secret_key() -> bytes:
    if os.path.exists(SECRET_KEY_PATH):
        try:
            with open(SECRET_KEY_PATH, 'rb') as f:
                key = f.read()
                if len(key) == 32:
                    return key
        except Exception:
            pass
    key = secrets.token_bytes(32)
    try:
        os.makedirs(os.path.dirname(SECRET_KEY_PATH), exist_ok=True)
        fd = os.open(SECRET_KEY_PATH, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with open(fd, 'wb') as f:
            f.write(key)
    except Exception:
        pass
    return key

def hash_pin(pin: str) -> str:
    key = get_or_create_secret_key()
    return hmac.new(key, pin.encode('utf-8'), hashlib.sha256).hexdigest()

def encrypt_held_file(input_path: str, output_enc_path: str):
    key = get_or_create_secret_key()
    with open(input_path, 'rb') as f:
        data = f.read()
    if HAS_CRYPTOGRAPHY:
        nonce = secrets.token_bytes(12)
        aesgcm = AESGCM(key)
        ct = aesgcm.encrypt(nonce, data, None)
        with open(output_enc_path, 'wb') as f:
            f.write(b"AESGCM1:" + nonce + ct)
    else:
        with open(output_enc_path, 'wb') as f:
            f.write(b"RAW0600:" + data)
    try:
        os.chmod(output_enc_path, 0o600)
    except Exception:
        pass

def decrypt_held_file(enc_path: str, output_path: str):
    key = get_or_create_secret_key()
    with open(enc_path, 'rb') as f:
        payload = f.read()
    if payload.startswith(b"AESGCM1:"):
        if not HAS_CRYPTOGRAPHY:
            raise RuntimeError("cryptography package required to decrypt held file")
        nonce = payload[8:20]
        ct = payload[20:]
        aesgcm = AESGCM(key)
        data = aesgcm.decrypt(nonce, ct, None)
        with open(output_path, 'wb') as f:
            f.write(data)
    elif payload.startswith(b"RAW0600:"):
        with open(output_path, 'wb') as f:
            f.write(payload[8:])
    else:
        with open(output_path, 'wb') as f:
            f.write(payload)
    try:
        os.chmod(output_path, 0o600)
    except Exception:
        pass

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        os.makedirs(SPOOL_PINNED_DIR, exist_ok=True)
        os.makedirs(SPOOL_INBOX_DIR, exist_ok=True)
        os.makedirs(SPOOL_PREVIEWS_DIR, exist_ok=True)
    except Exception:
        pass
    get_or_create_secret_key()
    conn = get_db()
    with conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=20000;")
        except Exception:
            pass
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
                expires_at INTEGER,
                failed_attempts INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY,
                file_path TEXT,
                filename TEXT,
                size INTEGER,
                device TEXT,
                received_at INTEGER
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
                status TEXT,
                source TEXT DEFAULT 'portal'
            )
        """)

        try:
            conn.execute("ALTER TABLE pinned_jobs ADD COLUMN failed_attempts INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE print_ledger ADD COLUMN source TEXT DEFAULT 'portal'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE print_ledger ADD COLUMN cups_job_id INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_cups_job_id ON print_ledger(cups_job_id)")
        except sqlite3.OperationalError:
            pass
        defaults = {
            'currency_symbol': '₱',
            'rate_bw': '2.00',
            'rate_color': '5.00',
            'pin_expire_hours': '24',
            'max_pages_per_job': '50',
            'public_print': 'on',
            'auto_anti_clog': 'on',
            'last_anti_clog_check': '0'
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'admin_hash'")
        row = cur.fetchone()
        if not row:
            initial_pass = secrets.token_urlsafe(12)
            admin_hash = generate_password_hash(initial_pass, method='scrypt')
            conn.execute("INSERT INTO settings (key, value) VALUES ('admin_hash', ?)", (admin_hash,))
            init_pass_file = os.path.join(os.path.dirname(DB_PATH), 'initial_admin_password.txt')
            try:
                fd = os.open(init_pass_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with open(fd, 'w') as f:
                    f.write(initial_pass + "\n")
            except Exception:
                pass

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

def is_public_print_enabled() -> bool:
    env_val = os.environ.get('PUBLIC_PRINT')
    if env_val is not None:
        return env_val.lower() in ['1', 'true', 'on', 'yes']
    return get_setting('public_print', 'on').lower() in ['1', 'true', 'on', 'yes']

def record_ledger(title, printer, pages, copies, total_pages, colormodel, cost, client_ip, status, source='portal', cups_job_id=None):
    try:
        conn = get_db()
        with conn:
            conn.execute("""
                INSERT INTO print_ledger 
                (timestamp, job_title, printer, pages, copies, total_pages, colormodel, cost, client_ip, status, source, cups_job_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (int(time.time()), title, printer, pages, copies, total_pages, colormodel, cost, client_ip, status, source, cups_job_id))
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

def validate_extension(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"File extension '{ext}' is not supported")
    return ext

def validate_copies(copies_raw) -> int:
    try:
        val = int(copies_raw)
        if val < 1 or val > 99:
            raise ValueError("Copies must be between 1 and 99")
        return val
    except (ValueError, TypeError):
        raise ValueError("Copies must be an integer between 1 and 99")

def validate_page_range(range_str: str) -> str:
    range_str = (range_str or '').strip()
    if range_str and not re.match(r'^[0-9,\- ]+$', range_str):
        raise ValueError("Invalid page range format")
    return range_str

def validate_pin(pin_str: str) -> str:
    pin_str = (pin_str or '').strip()
    if not re.match(r'^[0-9]{6}$', pin_str):
        raise ValueError("PIN must be exactly 6 digits")
    return pin_str

def parse_page_ranges(range_str: str, total_pages: int) -> int:
    """Calculates the exact count of pages selected by range_str clamped to [1, total_pages]."""
    if not range_str or not str(range_str).strip():
        return max(1, total_pages)
    selected = set()
    parts = str(range_str).split(',')
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            subparts = part.split('-', 1)
            try:
                raw_s = int(subparts[0].strip())
                raw_e = int(subparts[1].strip())
                if (raw_s > total_pages and raw_e > total_pages) or (raw_s < 1 and raw_e < 1):
                    continue
                s = max(1, min(total_pages, raw_s))
                e = max(1, min(total_pages, raw_e))
                start = min(s, e)
                end = max(s, e)
                selected.update(range(start, end + 1))
            except ValueError:
                pass
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    selected.add(p)
            except ValueError:
                pass
    return len(selected) if selected else max(1, total_pages)

def build_lp_command(printer: str, pdf_path: str, options: dict) -> list:
    """Builds standard, robust lp command flags mapping UI options to CUPS PPD attributes."""
    cmd = ['lp', '-d', printer]
    copies = options.get('copies', 1)
    try:
        copies = int(copies)
        if copies > 1:
            cmd.extend(['-n', str(copies)])
    except Exception:
        pass

    pagesize = options.get('pagesize', 'A4')
    cups_pagesize = CANONICAL_PAGESIZES.get(pagesize, pagesize)
    if cups_pagesize:
        cmd.extend(['-o', f'PageSize={cups_pagesize}'])

    mediatype = options.get('mediatype', 'Plain')
    cups_mediatype = CANONICAL_MEDIATYPES.get(mediatype, mediatype)
    if cups_mediatype:
        cmd.extend(['-o', f'MediaType={cups_mediatype}'])

    quality = options.get('quality', 'Normal')
    if quality:
        cmd.extend(['-o', f'OutputMode={quality}'])

    colormodel = options.get('colormodel', 'RGB')
    if colormodel in ('KGray', 'Gray', 'K'):
        cmd.extend(['-o', 'ColorModel=KGray', '-o', 'print-color-mode=monochrome'])
    elif colormodel:
        cmd.extend(['-o', f'ColorModel={colormodel}', '-o', 'print-color-mode=color'])

    duplex = options.get('duplex', 'None')
    if duplex and duplex != 'None':
        cmd.extend(['-o', f'Duplex={duplex}'])

    orientation = str(options.get('orientation', 'auto'))
    if orientation in ['3', '4']:
        cmd.extend(['-o', f'orientation-requested={orientation}'])

    fitpage = options.get('fitpage')
    if fitpage is True or str(fitpage).lower() == 'true':
        cmd.extend(['-o', 'fit-to-page', '-o', 'print-scaling=fit'])
    else:
        cmd.extend(['-o', 'print-scaling=none'])

    pagerange = str(options.get('pagerange', '')).strip()
    if pagerange:
        cmd.extend(['-o', f'page-ranges={pagerange}'])

    resolution = options.get('resolution')
    if resolution:
        cmd.extend(['-o', f'resolution={resolution}dpi'])

    margin_mode = options.get('margin_mode', 'printable')
    if margin_mode == 'none':
        cmd.extend(['-o', 'page-top=0', '-o', 'page-bottom=0', '-o', 'page-left=0', '-o', 'page-right=0'])
    elif margin_mode == 'normal':
        cmd.extend(['-o', 'page-top=36', '-o', 'page-bottom=36', '-o', 'page-left=36', '-o', 'page-right=36'])
    elif margin_mode == 'custom':
        try:
            m_top = max(0, min(100, float(options.get('m_top', '10'))))
            m_bottom = max(0, min(100, float(options.get('m_bottom', '10'))))
            m_left = max(0, min(100, float(options.get('m_left', '10'))))
            m_right = max(0, min(100, float(options.get('m_right', '10'))))
            cmd.extend([
                '-o', f'page-top={int(m_top * 2.83465)}',
                '-o', f'page-bottom={int(m_bottom * 2.83465)}',
                '-o', f'page-left={int(m_left * 2.83465)}',
                '-o', f'page-right={int(m_right * 2.83465)}'
            ])
        except Exception:
            pass

    number_up = options.get('number_up')
    if number_up and str(number_up) in ['2', '4', '6', '9', '16']:
        cmd.extend(['-o', f'number-up={number_up}'])
        page_border = options.get('page_border')
        if page_border in ['single', 'single-thick', 'double', 'double-thick']:
            cmd.extend(['-o', f'page-border={page_border}'])
        elif str(page_border).lower() in ['true', '1', 'yes', 'on']:
            cmd.extend(['-o', 'page-border=single'])

    outputorder = options.get('outputorder')
    if not outputorder:
        outputorder = get_setting('cups_default_outputorder', 'reverse')
    if outputorder in ('reverse', 'normal'):
        cmd.extend(['-o', f'outputorder={outputorder}'])

    cmd.append(pdf_path)
    return cmd

def convert_to_pdf(input_path, original_filename):
    ext = validate_extension(original_filename)
    temp_dir = tempfile.mkdtemp(prefix="conv_")
    output_pdf = os.path.join(temp_dir, "converted.pdf")
    lo_profile = os.path.join(temp_dir, "lo_profile")

    try:
        if ext == '.pdf':
            safe_pdf = os.path.join(temp_dir, "safe_input.pdf")
            shutil.copy2(input_path, safe_pdf)
            return safe_pdf, temp_dir

        with CONVERSION_SEMAPHORE:
            if ext in ['.heic', '.heif', '.avif', '.webp', '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif']:
                subprocess.run(
                    ['convert', input_path, '-auto-orient', '-density', '300', '-units', 'PixelsPerInch', output_pdf],
                    check=True,
                    capture_output=True,
                    timeout=30
                )
                return output_pdf, temp_dir

            if ext in ['.docx', '.doc', '.odt', '.rtf', '.txt', '.csv', '.xlsx', '.xls', '.pptx', '.ppt']:
                subprocess.run([
                    'libreoffice',
                    '--headless',
                    f'-env:UserInstallation=file://{lo_profile}',
                    '--convert-to', 'pdf',
                    input_path,
                    '--outdir', temp_dir
                ], check=True, capture_output=True, timeout=45)
                base = os.path.splitext(os.path.basename(input_path))[0]
                conv_file = os.path.join(temp_dir, f"{base}.pdf")
                if os.path.exists(conv_file):
                    return conv_file, temp_dir

        raise ValueError(f"Conversion unsupported for format '{ext}'")
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

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
                } else if (tabName === 'cups') {
                    if (typeof loadCupsOverview === 'function') loadCupsOverview();
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

        /* 2D Vector Icons */
        .icon {
            display: inline-block;
            vertical-align: -2px;
            width: 13px;
            height: 13px;
            stroke: currentColor;
            fill: none;
            stroke-width: 2;
            stroke-linecap: round;
            stroke-linejoin: round;
            flex-shrink: 0;
        }
        .icon-lg {
            width: 44px;
            height: 44px;
            stroke-width: 1.5;
            stroke: #5f6368;
            fill: none;
            stroke-linecap: round;
            stroke-linejoin: round;
            margin-bottom: 6px;
            display: block;
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
                height: auto;
            }
            .cr-desk {
                width: 100%;
                min-height: 260px;
                height: 42vh;
                max-height: 380px;
                padding: 12px 16px;
            }
            .cr-sidebar {
                width: 100% !important;
                max-width: 100% !important;
                min-width: 0 !important;
                border-left: none !important;
                border-top: 1px solid var(--border);
                height: auto !important;
            }
        }

        @media (max-width: 768px) {
            body {
                height: auto;
                min-height: 100dvh;
                overflow-x: hidden;
                overflow-y: auto;
                -webkit-overflow-scrolling: touch;
            }
            main {
                height: auto;
                min-height: calc(100dvh - 84px);
                overflow: visible;
                padding-bottom: 74px;
            }
            .tab-content {
                height: auto;
                min-height: 100%;
            }
            .tab-content.active {
                display: block;
            }

            /* Responsive 2-Row Header Reflow via CSS Flex Order */
            header {
                height: auto;
                min-height: 80px;
                padding: 8px 12px 6px;
                flex-wrap: wrap;
                gap: 8px 10px;
            }
            .header-brand {
                order: 1;
                flex: 1;
                min-width: 0;
                gap: 6px;
            }
            .header-title {
                font-size: 12px;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .header-title .tag {
                display: none;
            }
            .printer-select-wrapper {
                padding: 2px 6px;
                max-width: 140px;
            }
            #headerPrinterSelect {
                max-width: 110px;
                font-size: 11px !important;
            }
            .header-controls {
                order: 2;
                gap: 6px;
                flex-shrink: 0;
            }
            .btn-lock, .btn-theme {
                padding: 5px 8px;
                font-size: 11px;
            }
            .btn-lock span {
                display: none;
            }
            .header-nav {
                order: 3;
                width: 100%;
                margin-top: 2px;
                padding-bottom: 2px;
                gap: 5px;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                scrollbar-width: none;
            }
            .header-nav::-webkit-scrollbar {
                display: none;
            }
            .nav-btn {
                padding: 6px 12px;
                font-size: 12px;
                border-radius: 9999px;
                flex-shrink: 0;
            }

            /* Desk & Preview Canvas */
            .cr-desk {
                width: 100%;
                height: 36vh;
                min-height: 220px;
                max-height: 320px;
                padding: 8px 10px;
            }
            .cr-doc-title {
                max-width: 170px;
                font-size: 12px;
            }
            .cr-empty-desk {
                padding: 14px;
                gap: 4px;
            }
            .cr-empty-desk .icon-lg {
                width: 32px;
                height: 32px;
                margin-bottom: 2px;
            }
            .cr-empty-desk .empty-title {
                font-size: 13px;
            }
            .cr-empty-desk .empty-subtitle {
                font-size: 11px;
                margin-bottom: 4px;
            }
            .cr-empty-desk .empty-btn {
                padding: 4px 14px;
                font-size: 11px;
            }
            .cr-desk-footer-pill {
                font-size: 10px;
                padding: 3px 10px;
                max-width: 95%;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            /* Settings Sidebar */
            .cr-sidebar {
                padding-bottom: 75px;
            }
            .cr-sidebar-header {
                padding: 12px 16px 10px;
            }
            .cr-row {
                padding: 9px 14px;
                min-height: 44px;
            }
            .cr-row-control {
                width: 55%;
                max-width: 200px;
            }
            select, .cr-select, .cr-input {
                font-size: 14px;
                padding: 6px 22px 6px 8px;
                min-height: 34px;
            }
            .cr-checkbox-row {
                padding: 4px 0;
            }
            .cr-checkbox-row input[type="checkbox"] {
                width: 18px;
                height: 18px;
            }

            /* Fixed Floating Bottom Print Bar */
            .cr-bottom-bar {
                position: fixed;
                bottom: 0;
                left: 0;
                right: 0;
                width: 100%;
                z-index: 100;
                margin-top: 0;
                padding: 10px 16px;
                background: var(--surface);
                border-top: 1px solid var(--border);
                box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.25);
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 12px;
            }
            .cr-btn-pill {
                padding: 8px 18px;
                font-size: 13px;
                min-height: 38px;
            }
            .cr-btn-print {
                flex: 1;
                max-width: 200px;
                font-size: 14px;
                font-weight: 600;
                padding: 8px 24px;
            }

            /* ID Studio Mobile */
            #idPreviewDesk {
                height: 36vh;
                min-height: 220px;
                max-height: 320px;
                padding: 8px 10px;
            }
            #idSheetTitleTag {
                max-width: 180px;
                font-size: 11px;
            }
            #idEditControls .cr-row-control {
                width: 60%;
                max-width: 220px;
            }
            .id-bg-btn, .id-crop-btn {
                padding: 4px 8px !important;
                min-height: 28px;
            }

            /* General Tabs on Mobile */
            .cr-page-container {
                padding: 12px 12px 80px;
                margin: 6px auto;
                width: 100%;
            }
            .cr-card {
                padding: 14px;
                margin-bottom: 12px;
            }
            .cr-table-wrapper {
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                width: 100%;
                margin-top: 8px;
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
            }
            .cr-table-wrapper table {
                min-width: 580px;
            }
            #tab-settings div[style*="grid-template-columns"] {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 8px !important;
            }
            #adminLockedState div {
                flex-direction: column;
                max-width: 100% !important;
            }
            #adminUnlockedState div {
                max-width: 100% !important;
            }
            #adminRateBw, #adminRateColor {
                width: 100%;
            }
            .modal-card {
                width: 92%;
                max-width: 360px;
                padding: 18px;
            }
            .toast {
                bottom: 74px;
                max-width: 90%;
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
            font-weight: 600;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 420px;
        }

        .cr-desk-top-toolbar .cr-btn-pill.cr-btn-cancel {
            background: var(--surface-elevated);
            border: 1px solid var(--border);
            color: var(--text-primary);
        }
        .cr-desk-top-toolbar .cr-btn-pill.cr-btn-cancel:hover {
            background: var(--surface-hover);
            border-color: var(--border-hover);
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
            max-width: 100%;
            max-height: 100%;
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
            position: absolute;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
            cursor: pointer;
            padding: 24px;
            border-radius: var(--radius-md);
            transition: background 0.15s;
            z-index: 5;
        }
        .cr-empty-desk:hover {
            background: rgba(0, 0, 0, 0.03);
        }
        .cr-empty-desk .empty-title {
            font-weight: 600;
            font-size: 15px;
            color: #202124 !important;
        }
        .cr-empty-desk .empty-subtitle {
            font-size: 12px;
            color: #5f6368 !important;
            margin-bottom: 8px;
        }
        .cr-empty-desk .empty-btn {
            font-size: 12px;
            pointer-events: none;
            background: #ffffff !important;
            border: 1px solid #dadce0 !important;
            color: #1a73e8 !important;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
            border-radius: 9999px;
            padding: 6px 18px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 500;
        }
        .cr-empty-desk:hover .empty-btn {
            background: #f8fafd !important;
            border-color: #aecbfa !important;
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

        select, .cr-select {
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
        select option, .cr-select option {
            background-color: var(--surface);
            color: var(--text-primary);
        }
        .cr-select:focus, .cr-input:focus, select:focus {
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
        ::placeholder {
            color: var(--text-tertiary);
            opacity: 1;
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
            background: var(--surface-hover);
        }

        .id-bg-btn[data-bg="blue"] {
            color: var(--accent) !important;
        }
        .id-bg-btn[data-bg="red"] {
            color: var(--danger) !important;
        }
        .id-crop-btn.active, .id-bg-btn.active {
            background: var(--surface-hover);
            border-color: var(--accent);
            color: var(--accent);
            font-weight: 600;
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
            color: var(--text-primary);
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
            color: var(--text-primary);
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
        <button class="nav-btn" data-tab="cups" onclick="switchTab('cups')">CUPS Control</button>
        <button class="nav-btn" data-tab="pin" onclick="switchTab('pin')">PIN Release</button>
        <button class="nav-btn" data-tab="history" onclick="switchTab('history')">History</button>
        <button class="nav-btn" data-tab="settings" onclick="switchTab('settings')">Settings</button>
    </nav>

    <div class="header-controls">
        <button class="btn-lock" id="btnAdminLock"><svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg> <span>Locked</span></button>
        <button class="btn-theme" id="btnThemeToggle" title="Toggle theme" aria-label="Toggle theme"><svg class="icon" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm0 18a8 8 0 0 1 0-16v16z"/></svg></button>
    </div>
</header>

<main>
    <!-- TAB 1: EXACT CHROMIUM PRINT PREVIEW DIALOG -->
    <div class="tab-content active" id="tab-print">
        <div id="inboxBar" style="display: none; margin: 0 0 10px 0; padding: 10px 14px; border: 1px solid var(--border); border-radius: 10px; font-size: 13px;">
            <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap;">
                <div><strong>Inbox</strong> <span id="inboxSummary" style="color: var(--text-secondary);"></span></div>
                <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnInboxToggle" style="padding: 3px 12px; font-size: 12px;">Show</button>
            </div>
            <div id="inboxList" style="display: none; flex-direction: column; gap: 6px; margin-top: 10px;"></div>
        </div>
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
                        <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnClearPreview" style="padding: 2px 10px; font-size: 11px;"><svg class="icon" viewBox="0 0 24 24" style="width: 11px; height: 11px;"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg> Clear</button>
                    </div>
                </div>

                <div class="cr-desk-viewport" id="printDeskViewport">
                    <div class="paper-sheet" id="paperSheet">
                        <div class="printable-area" id="printableArea">
                            <div class="content-render-wrap" id="renderWrap">
                                <canvas id="pdfCanvas" style="display: none;"></canvas>
                                <img id="imgPreview" style="display: none;" alt="Preview">
                                <div id="txtPreview" style="display: none; padding: 20px; font-size: 12px; color: #333; overflow: hidden; width:100%; height:100%; word-break: break-all;"></div>
                            </div>
                        </div>
                    </div>

                    <div id="previewEmptyState" class="cr-empty-desk" onclick="document.getElementById('printFileInput').click()">
                        <svg class="icon-lg" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>
                        <div class="empty-title">No document loaded</div>
                        <div class="empty-subtitle">Drop PDF, Word, Excel, or Image to inspect</div>
                        <button type="button" class="empty-btn">Choose File...</button>
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
                    <svg id="crMoreArrow" class="icon" viewBox="0 0 24 24" style="width: 12px; height: 12px; transition: transform 0.15s ease;"><polyline points="6 9 12 15 18 9"></polyline></svg>
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
                    <div id="customMarginBox" class="cr-row" style="display: none; flex-direction: column; align-items: stretch; gap: 8px; background: var(--surface-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); margin: 4px 20px 8px; padding: 8px 12px;">
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

                    <!-- Pages per Sheet (N-Up) & Border -->
                    <div class="cr-row">
                        <span class="cr-row-label">Pages / sheet</span>
                        <div class="cr-row-control">
                            <select id="printNumberUp" class="cr-select">
                                <option value="1" selected>1 (Standard)</option>
                                <option value="2">2 (2-Up Handout)</option>
                                <option value="4">4 (4-Up Grid)</option>
                                <option value="6">6 (6-Up Handout)</option>
                                <option value="9">9 (9-Up Slides)</option>
                            </select>
                        </div>
                    </div>
                    <div class="cr-row" id="pageBorderRow" style="display: none; padding-top: 0;">
                        <span class="cr-row-label" style="font-size: 11px; color: var(--text-tertiary);">Slide Border</span>
                        <div class="cr-row-control" style="justify-content: flex-start;">
                            <label class="cr-checkbox-row">
                                <input type="checkbox" id="chkPageBorder">
                                <span style="font-size: 13px;">Draw border around slides</span>
                            </label>
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
                        <span class="cr-row-label" style="font-size: 11px; color: var(--text-tertiary);">6-Digit PIN</span>
                        <div class="cr-row-control">
                            <input type="password" id="printPin" class="cr-input" maxlength="6" placeholder="••••••" style="text-align: center; font-size: 16px; letter-spacing: 0.25em; font-family: var(--font-mono); width: 110px;">
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
                        <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnIdDownload" style="padding: 3px 10px; font-size: 11px;"><svg class="icon" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg> Save PNG</button>
                        <button type="button" class="cr-btn-pill cr-btn-print" id="btnIdDirectPrint" style="padding: 3px 14px; font-size: 11px;"><svg class="icon" viewBox="0 0 24 24"><polyline points="6 9 6 2 18 2 18 9"></polyline><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path><rect x="6" y="14" width="12" height="8"></rect></svg> Print ID Sheet</button>
                    </div>
                </div>

                <div class="cr-desk-viewport">
                    <div class="paper-sheet" id="idPaperSheet" style="display: flex; align-items: center; justify-content: center; background: #ffffff; border-radius: 2px; overflow: hidden; position: relative;">
                        <canvas id="idSheetCanvas" style="width: 100%; height: 100%; object-fit: contain; display: none;"></canvas>
                    </div>
                    <div id="idEmptyState" class="cr-empty-desk" onclick="document.getElementById('idFileInput').click()">
                        <svg class="icon-lg" viewBox="0 0 24 24"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>
                        <div class="empty-title">Print Shop ID Studio Ready</div>
                        <div class="empty-subtitle">Upload portrait photo on right to auto-generate packages</div>
                        <button type="button" class="empty-btn">Choose Portrait...</button>
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
                            <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnIdRotate" style="padding: 3px 8px; font-size: 11px;" title="Rotate 90°"><svg class="icon" viewBox="0 0 24 24" style="width: 12px; height: 12px;"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"></path></svg></button>
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
                            <button type="button" class="cr-btn-pill cr-btn-cancel id-bg-btn" data-bg="blue" style="padding: 2px 6px; font-size: 10px;">Blue</button>
                            <button type="button" class="cr-btn-pill cr-btn-cancel id-bg-btn" data-bg="red" style="padding: 2px 6px; font-size: 10px;">Red</button>
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

    <!-- TAB: CUPS HARDWARE COMMAND CENTER -->
    <div class="tab-content" id="tab-cups">
        <div class="cr-page-container">
            <!-- Admin Lock Banner if locked -->
            <div id="cupsAdminBanner" style="display: none; background: rgba(234, 134, 0, 0.12); border: 1px solid rgba(234, 134, 0, 0.35); border-radius: var(--radius-md); padding: 12px 16px; margin-bottom: 16px; align-items: center; justify-content: space-between; gap: 12px;">
                <div style="display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--warning);">
                    <svg class="icon" viewBox="0 0 24 24" style="stroke: var(--warning); flex-shrink: 0;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                    <span><strong>Read-Only Mode:</strong> Hardware settings and spooler actions require administrator authentication.</span>
                </div>
                <button type="button" class="cr-btn-pill cr-btn-print" style="padding: 4px 14px; font-size: 12px;" onclick="window.switchTab('settings')">Unlock Admin</button>
            </div>

            <!-- Top Header & Printer Selector -->
            <div class="cr-card" style="margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 14px;">
                    <div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <h2 style="font-size: 18px; font-weight: 600; color: var(--text-primary); margin: 0;">CUPS Hardware Command Center</h2>
                            <span class="badge" id="cupsStatusBadge" style="background: rgba(30, 142, 62, 0.2); color: var(--success); font-weight: 600; padding: 2px 8px; border-radius: 12px; font-size: 11px;">Ready</span>
                        </div>
                        <p style="font-size: 12px; color: var(--text-secondary); margin: 4px 0 0 0;">
                            Configure hardware defaults for all devices (Direct Windows PCs, Android Mopria, iOS AirPrint & Web Portal).
                        </p>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <select id="cupsPrinterSelect" class="cr-select" style="min-width: 200px; font-weight: 500;">
                            <option value="">Loading printers...</option>
                        </select>
                        <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnRefreshCups" title="Refresh CUPS Status" style="padding: 6px 12px;">
                            <svg class="icon" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg> Refresh
                        </button>
                    </div>
                </div>

                <!-- Hardware info details grid -->
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border);">
                    <div style="font-size: 12px;">
                        <span style="color: var(--text-secondary); display: block; font-size: 10px; text-transform: uppercase;">Device URI</span>
                        <code id="cupsInfoUri" style="color: var(--text-primary); font-size: 11px; word-break: break-all;">--</code>
                    </div>
                    <div style="font-size: 12px;">
                        <span style="color: var(--text-secondary); display: block; font-size: 10px; text-transform: uppercase;">Make & Driver</span>
                        <span id="cupsInfoDriver" style="color: var(--text-primary); font-weight: 500;">--</span>
                    </div>
                    <div style="font-size: 12px;">
                        <span style="color: var(--text-secondary); display: block; font-size: 10px; text-transform: uppercase;">Spooler Queue State</span>
                        <span id="cupsInfoAccepting" style="color: var(--success); font-weight: 500;">Accepting Jobs</span>
                    </div>
                    <div style="font-size: 12px;">
                        <span style="color: var(--text-secondary); display: block; font-size: 10px; text-transform: uppercase;">Network Sharing</span>
                        <span id="cupsInfoShared" style="color: var(--accent); font-weight: 500;">Shared Network Queue</span>
                    </div>
                </div>

                <!-- Quick Printer Hardware Actions -->
                <div style="display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border);">
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" id="btnCupsPauseResume">
                        <svg class="icon" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg> Pause Printer
                    </button>
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" id="btnCupsAcceptReject">
                        <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg> Reject Jobs
                    </button>
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" id="btnCupsTestPage">
                        <svg class="icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg> Print Official Test Page
                    </button>
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" id="btnCupsRestartDaemon">
                        <svg class="icon" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg> Restart CUPS Spooler
                    </button>
                </div>
            </div>

            <!-- 1-Click Shop Hardware Profiles Bar -->
            <div class="cr-card" style="margin-bottom: 16px; background: var(--surface-elevated);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <svg class="icon" viewBox="0 0 24 24" style="stroke: var(--accent);"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>
                        <strong style="font-size: 13px; color: var(--text-primary);">1-Click Print Shop Profiles</strong>
                    </div>
                    <span style="font-size: 11px; color: var(--text-secondary);">Click a profile to preset hardware defaults for all connected devices.</span>
                </div>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px;">
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" onclick="applyCupsProfile('standard_docs')" style="justify-content: flex-start; padding: 8px 12px; height: auto;">
                        <div>
                            <div style="font-weight: 600; color: var(--text-primary); font-size: 12px;">📄 Standard Documents</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">A4 • Plain • Normal • B&W • Reverse</div>
                        </div>
                    </button>
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" onclick="applyCupsProfile('eco_draft')" style="justify-content: flex-start; padding: 8px 12px; height: auto;">
                        <div>
                            <div style="font-weight: 600; color: var(--text-primary); font-size: 12px;">⚡ Eco Draft (Fast)</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">A4 • Plain • Draft • B&W • 300 DPI</div>
                        </div>
                    </button>
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" onclick="applyCupsProfile('glossy_photo')" style="justify-content: flex-start; padding: 8px 12px; height: auto;">
                        <div>
                            <div style="font-weight: 600; color: var(--text-primary); font-size: 12px;">🖼️ Glossy Photo (4×6")</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">4×6 • Glossy • Photo • RGB • 1200 DPI</div>
                        </div>
                    </button>
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" onclick="applyCupsProfile('certificate')" style="justify-content: flex-start; padding: 8px 12px; height: auto;">
                        <div>
                            <div style="font-weight: 600; color: var(--text-primary); font-size: 12px;">📜 Certificate / Heavy</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">A4 • CoatedMatte • Best • RGB • 600 DPI</div>
                        </div>
                    </button>
                </div>
            </div>

            <!-- Global Hardware Defaults Card -->
            <div class="cr-card" style="margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h3 style="font-size: 15px; font-weight: 600; color: var(--text-primary); margin: 0;">Global Hardware Defaults</h3>
                        <p style="font-size: 12px; color: var(--text-secondary); margin: 3px 0 0 0;">
                            Changes update CUPS PPD and <code>lpoptions</code>. All connected computers and mobile devices inherit these defaults.
                        </p>
                    </div>
                    <button type="button" class="cr-btn-pill cr-btn-print cups-admin-req" id="btnSaveCupsDefaults">
                        <svg class="icon" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg> Save CUPS Defaults
                    </button>
                </div>

                <form id="cupsDefaultsForm" onsubmit="return false;">
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px;">
                        <!-- Page Stack Order -->
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">
                                Page Stack Order (Reverse / Normal)
                            </label>
                            <select id="cupsDefOutputOrder" class="cr-select" style="width: 100%;">
                                <option value="reverse">Reverse (Page 1 on Top - Face-Up Tray) [Default]</option>
                                <option value="normal">Normal (Page 1 on Bottom - Face-Down Tray)</option>
                            </select>
                            <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
                                Prints last page first so sheets stack face-up in 1 to N order without rearranging.
                            </div>
                        </div>

                        <!-- Default Paper Size -->
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">
                                Default Paper Size (PageSize)
                            </label>
                            <select id="cupsDefPageSize" class="cr-select" style="width: 100%;">
                                <option value="A4">A4 (210 × 297 mm)</option>
                                <option value="Letter">Letter (8.5 × 11 in)</option>
                                <option value="Legal">Legal (8.5 × 14 in)</option>
                                <option value="4x6in">4 × 6 in (Photo Card)</option>
                                <option value="5x7in">5 × 7 in (Photo Card)</option>
                                <option value="8x10">8 × 10 in (Photo Print)</option>
                                <option value="EnvelopeDL">Envelope DL (110 × 220 mm)</option>
                            </select>
                            <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
                                Standard sheet size assumed when clients do not specify paper dimensions.
                            </div>
                        </div>

                        <!-- Default Media Type -->
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">
                                Default Media Type (MediaType)
                            </label>
                            <select id="cupsDefMediaType" class="cr-select" style="width: 100%;">
                                <option value="Plain">Plain Paper (Standard Copy & Bond)</option>
                                <option value="Glossy">HP Photo Paper / Glossy</option>
                                <option value="FastGlossy">Everyday Glossy Photo</option>
                                <option value="CoatedMatte">Matte Presentation Paper</option>
                                <option value="Brochure">Brochure Paper</option>
                            </select>
                            <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
                                Calibrates ink delivery and drying time for media surface.
                            </div>
                        </div>

                        <!-- Default Print Quality -->
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">
                                Default Print Quality (OutputMode)
                            </label>
                            <select id="cupsDefQuality" class="cr-select" style="width: 100%;">
                                <option value="Normal">Normal (Standard 600 DPI, Fast & Balanced)</option>
                                <option value="Best">Best (Enhanced Quality, High Contrast)</option>
                                <option value="Photo">Photo (Ultra-Fine Photo Definition)</option>
                                <option value="Draft">Draft (High Speed Ink-Saver)</option>
                            </select>
                            <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
                                Determines printhead passes and detail density.
                            </div>
                        </div>

                        <!-- Default Color Mode -->
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">
                                Default Color Mode (ColorModel)
                            </label>
                            <select id="cupsDefColorModel" class="cr-select" style="width: 100%;">
                                <option value="RGB">Color (RGB - Full Color Printing)</option>
                                <option value="KGray">Monochrome (Black & White Only)</option>
                            </select>
                            <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
                                Restrict to Black ink cartridge only or allow color printing by default.
                            </div>
                        </div>

                        <!-- Hardware Scaling Mode -->
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">
                                Hardware Scaling (print-scaling)
                            </label>
                            <select id="cupsDefScaling" class="cr-select" style="width: 100%;">
                                <option value="none">100% Exact Scale (None / 1:1 Size)</option>
                                <option value="fit">Fit to Page (Shrink / Expand to Margins)</option>
                                <option value="fill">Fill Entire Page (Full Bleed)</option>
                            </select>
                            <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
                                Automatically adapts documents that slightly exceed printable bounds.
                            </div>
                        </div>

                        <!-- Two-Sided Printing -->
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">
                                Duplex Mode (Two-Sided)
                            </label>
                            <select id="cupsDefDuplex" class="cr-select" style="width: 100%;">
                                <option value="None">None (Single-Sided)</option>
                                <option value="DuplexNoTumble">Duplex Long-Edge (Book Binding)</option>
                                <option value="DuplexTumble">Duplex Short-Edge (Flip-Top Binding)</option>
                            </select>
                            <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
                                Manual duplex prompt or hardware duplexer configuration.
                            </div>
                        </div>

                        <!-- Resolution -->
                        <div>
                            <label style="font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">
                                Hardware Resolution (DPI)
                            </label>
                            <select id="cupsDefResolution" class="cr-select" style="width: 100%;">
                                <option value="600">600 DPI (Standard Crisp Text & Images)</option>
                                <option value="300">300 DPI (Draft / Fast)</option>
                                <option value="1200">1200 DPI (High Precision Photo)</option>
                            </select>
                            <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
                                Hardware rasterizer rendering density.
                            </div>
                        </div>
                    </div>
                </form>
            </div>

            <!-- Live Spool Queue Card -->
            <div class="cr-card" style="margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h3 style="font-size: 15px; font-weight: 600; color: var(--text-primary); margin: 0;">Live CUPS Spool Queue</h3>
                        <p style="font-size: 12px; color: var(--text-secondary); margin: 3px 0 0 0;">
                            Real-time monitor of active and pending jobs in the hardware spooler.
                        </p>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <label style="font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; cursor: pointer;">
                            <input type="checkbox" id="chkAutoRefreshQueue" checked> Auto-refresh (5s)
                        </label>
                        <button type="button" class="cr-btn-pill cr-btn-cancel" id="btnRefreshQueueManual" style="padding: 4px 10px; font-size: 12px;">Refresh</button>
                        <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" id="btnPurgeQueue" style="color: var(--danger); padding: 4px 10px; font-size: 12px;">
                            <svg class="icon" viewBox="0 0 24 24" style="stroke: var(--danger);"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg> Purge Queue
                        </button>
                    </div>
                </div>

                <div style="overflow-x: auto;">
                    <table class="table-compact" style="width: 100%; border-collapse: collapse; font-size: 12px;">
                        <thead>
                            <tr style="border-bottom: 1px solid var(--border); text-align: left; color: var(--text-secondary);">
                                <th style="padding: 8px 10px;">Job ID</th>
                                <th style="padding: 8px 10px;">User / Client</th>
                                <th style="padding: 8px 10px;">Size</th>
                                <th style="padding: 8px 10px;">Submitted</th>
                                <th style="padding: 8px 10px;">Status</th>
                                <th style="padding: 8px 10px; text-align: right;">Action</th>
                            </tr>
                        </thead>
                        <tbody id="cupsQueueBody">
                            <tr>
                                <td colspan="6" style="text-align: center; padding: 20px; color: var(--text-secondary);">
                                    Loading spool queue...
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Recent Completed Jobs (24-Hour Spool Retention) -->
            <div class="cr-card" style="margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <h3 style="font-size: 15px; font-weight: 600; color: var(--text-primary); margin: 0;">Completed Jobs (24-Hour Spool Retention)</h3>
                            <span class="badge" style="background: rgba(30, 142, 62, 0.2); color: var(--success); font-size: 11px;">Spool Preserved</span>
                        </div>
                        <p style="font-size: 12px; color: var(--text-secondary); margin: 3px 0 0 0;">
                            Preserved jobs can be reprinted with 1 click without asking the customer to re-send or re-upload.
                        </p>
                    </div>
                </div>

                <div style="overflow-x: auto;">
                    <table class="table-compact" style="width: 100%; border-collapse: collapse; font-size: 12px;">
                        <thead>
                            <tr style="border-bottom: 1px solid var(--border); text-align: left; color: var(--text-secondary);">
                                <th style="padding: 8px 10px;">Job ID</th>
                                <th style="padding: 8px 10px;">User / Client</th>
                                <th style="padding: 8px 10px;">Size</th>
                                <th style="padding: 8px 10px;">Finished Date</th>
                                <th style="padding: 8px 10px;">Status</th>
                                <th style="padding: 8px 10px; text-align: right;">Action</th>
                            </tr>
                        </thead>
                        <tbody id="cupsCompletedJobsBody">
                            <tr>
                                <td colspan="6" style="text-align: center; padding: 20px; color: var(--text-secondary);">
                                    No completed jobs retained in spool cache.
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Printhead Health & Diagnostics -->
            <div class="cr-card" style="margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h3 style="font-size: 15px; font-weight: 600; color: var(--text-primary); margin: 0;">Printhead Health & Maintenance</h3>
                        <p style="font-size: 12px; color: var(--text-secondary); margin: 3px 0 0 0;">
                            Prevent HP Smart Tank nozzle clogs, test CMYK ink density, and resolve USB communication locks.
                        </p>
                    </div>
                </div>

                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px;">
                    <!-- CMYK Nozzle Check -->
                    <div style="background: var(--surface-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 14px;">
                        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                            <svg class="icon" viewBox="0 0 24 24" style="stroke: #00e5ff;"><circle cx="12" cy="12" r="10"></circle><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"></path><path d="M2 12h20"></path></svg>
                            <strong style="font-size: 13px; color: var(--text-primary);">Vector CMYK Nozzle Check</strong>
                        </div>
                        <p style="font-size: 11px; color: var(--text-secondary); margin-bottom: 12px;">Prints crisp Cyan, Magenta, Yellow, and Black alignment bars to check for missing jets or micro-banding.</p>
                        <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" onclick="runNozzleCheck()" style="padding: 6px 14px; font-size: 12px;">
                            <svg class="icon" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> Print CMYK Test Page
                        </button>
                    </div>

                    <!-- USB Hardware Reset -->
                    <div style="background: var(--surface-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 14px;">
                        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                            <svg class="icon" viewBox="0 0 24 24" style="stroke: var(--warning);"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"></path></svg>
                            <strong style="font-size: 13px; color: var(--text-primary);">USB Port Unfreeze (usbreset)</strong>
                        </div>
                        <p style="font-size: 11px; color: var(--text-secondary); margin-bottom: 12px;">Resets USB device <code>03f0:4554</code> at kernel level without power cycling the ZTE B860H appliance.</p>
                        <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" onclick="runUsbReset()" style="padding: 6px 14px; font-size: 12px; color: var(--warning);">
                            <svg class="icon" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg> Reset USB Port
                        </button>
                    </div>

                    <!-- Anti-Clog Scheduler -->
                    <div style="background: var(--surface-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 14px;">
                        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                            <svg class="icon" viewBox="0 0 24 24" style="stroke: var(--accent);"><path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"></path></svg>
                            <strong style="font-size: 13px; color: var(--text-primary);">Anti-Clog Keep-Wet Routine</strong>
                        </div>
                        <p style="font-size: 11px; color: var(--text-secondary); margin-bottom: 10px;">Automatically exercises thermal printhead every 7 days if the printer has been idle to keep nozzles wet.</p>
                        <div style="display: flex; align-items: center; justify-content: space-between;">
                            <label style="font-size: 12px; color: var(--text-primary); display: flex; align-items: center; gap: 6px; cursor: pointer;">
                                <input type="checkbox" id="chkAutoAntiClog" class="cups-admin-req" onchange="toggleAntiClog(this.checked)"> Auto Keep-Wet (7-Day)
                            </label>
                            <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" onclick="runNozzleCheck()" style="padding: 4px 10px; font-size: 11px;">Keep-Wet Now</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Network Companion Daemons & Printable Counter Sign -->
            <div class="cr-card" style="margin-bottom: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h3 style="font-size: 15px; font-weight: 600; color: var(--text-primary); margin: 0;">Network Companion Daemons & Counter Sign</h3>
                        <p style="font-size: 12px; color: var(--text-secondary); margin: 3px 0 0 0;">
                            Monitor background services powering Windows file shares, AirPrint discovery, and remote mesh access.
                        </p>
                    </div>
                    <a href="/api/reports/counter-sign" target="_blank" class="cr-btn-pill cr-btn-print" style="padding: 6px 14px; text-decoration: none; font-size: 12px;">
                        <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><line x1="9" y1="3" x2="9" y2="21"></line></svg> 🪧 Print Shop Counter QR Sign (A4)
                    </a>
                </div>

                <div id="networkServicesGrid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px;">
                    <!-- Populated dynamically via loadNetworkServices() -->
                    <div style="padding: 12px; text-align: center; color: var(--text-secondary); font-size: 12px;">Loading service status...</div>
                </div>
            </div>

            <!-- Advanced Driver PPD Options Accordion -->
            <div class="cr-card">
                <div id="cupsPpdToggle" style="display: flex; justify-content: space-between; align-items: center; cursor: pointer; user-select: none;">
                    <div>
                        <h3 style="font-size: 14px; font-weight: 600; color: var(--text-primary); margin: 0; display: flex; align-items: center; gap: 8px;">
                            <span>Advanced PPD Driver Inspector</span>
                            <span class="badge" id="cupsPpdCountBadge" style="background: var(--surface-elevated); font-size: 11px; padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border);">0 options</span>
                        </h3>
                        <p style="font-size: 11px; color: var(--text-secondary); margin: 2px 0 0 0;">
                            Inspect and fine-tune all low-level hardware options defined in the printer PPD driver.
                        </p>
                    </div>
                    <svg class="icon" id="cupsPpdArrow" viewBox="0 0 24 24" style="transition: transform 0.2s;"><polyline points="6 9 12 15 18 9"></polyline></svg>
                </div>
                <div id="cupsPpdContent" style="display: none; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border);">
                    <div id="cupsPpdGrid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-bottom: 14px;">
                        <!-- Dynamically filled with PPD options -->
                    </div>
                    <div style="display: flex; justify-content: flex-end;">
                        <button type="button" class="cr-btn-pill cr-btn-print cups-admin-req" id="btnApplyRawPpdOptions">
                            Apply Advanced PPD Settings
                        </button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 3: CONFIDENTIAL PIN RELEASE -->
    <div class="tab-content" id="tab-pin">
        <div class="cr-page-container">
            <div class="cr-card" style="margin-bottom: 16px;">
                <h3 style="font-size: 15px; font-weight: 600; color: var(--text-primary); margin-bottom: 6px;">Direct Job Release</h3>
                <p style="font-size: 12px; color: var(--text-secondary); margin-bottom: 12px;">Walk-up release: enter your Job ID and 6-digit release PIN to send your held document directly to the printer.</p>
                <div style="display: flex; gap: 10px; flex-wrap: wrap; align-items: center;">
                    <input type="text" id="directJobId" class="cr-input" placeholder="Job ID (e.g. pin_...)" style="flex: 1; min-width: 180px;">
                    <input type="password" id="directJobPin" class="cr-input" maxlength="6" placeholder="••••••" style="width: 110px; text-align: center; font-family: var(--font-mono); letter-spacing: 0.25em;">
                    <button type="button" class="cr-btn-pill cr-btn-print" id="btnDirectPinRelease" style="padding: 6px 16px;">Release to Printer</button>
                </div>
            </div>

            <div class="cr-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                    <div>
                        <h2 style="font-size: 16px; font-weight: 600; color: var(--text-primary);">Confidential Print Queue</h2>
                        <p style="font-size: 12px; color: var(--text-secondary);">Encrypted queue • Administrator authenticated view</p>
                    </div>
                    <button class="cr-btn-pill cr-btn-cancel" id="btnRefreshPinList" style="font-size: 12px;"><svg class="icon" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg> Refresh</button>
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
                            <tr><td colspan="7" style="text-align: center; color: var(--text-tertiary);">Loading queue...</td></tr>
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
                        <p style="font-size: 12px; color: var(--text-secondary);">Administrator view • Complete log of all print jobs</p>
                    </div>
                    <button type="button" id="btnExportCsv" class="cr-btn-pill cr-btn-cancel" style="font-size: 12px;"><svg class="icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> Export CSV</button>
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
                                <th>Source</th>
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
                            <tr><td colspan="9" style="text-align: center; color: var(--text-tertiary);">No audit history recorded</td></tr>
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
                        <input type="password" id="adminPassInput" class="cr-input" placeholder="Administrator Password">
                        <button class="cr-btn-pill cr-btn-print" id="btnUnlockAdmin">Unlock</button>
                    </div>
                </div>

                <div id="adminUnlockedState" style="display: none;">
                    <div style="font-size: 12px; color: var(--success); font-weight: 600; margin-bottom: 12px; display: flex; align-items: center; gap: 6px;">
                        <svg class="icon" viewBox="0 0 24 24" style="stroke: var(--success);"><polyline points="20 6 9 17 4 12"></polyline></svg>
                        <span>Administrator Mode Active</span>
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
                            <button class="cr-btn-pill cr-btn-cancel" id="btnAdminRestartCups"><svg class="icon" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg> Restart CUPS Spooler</button>
                            <button class="cr-btn-pill cr-btn-cancel" id="btnAdminPurgeCache"><svg class="icon" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg> Purge Temporary Cache</button>
                            <button class="cr-btn-pill cr-btn-cancel" id="btnAdminReboot" style="color: var(--warning);"><svg class="icon" viewBox="0 0 24 24" style="stroke: var(--warning);"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"></path><line x1="12" y1="2" x2="12" y2="12"></line></svg> Reboot Appliance</button>
                            <button class="cr-btn-pill cr-btn-cancel" id="btnAdminShutdown" style="color: var(--danger);"><svg class="icon" viewBox="0 0 24 24" style="stroke: var(--danger);"><circle cx="12" cy="12" r="10"></circle><rect x="9" y="9" width="6" height="6"></rect></svg> Shut Down</button>
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
            <label style="font-size: 11px; color: var(--text-secondary); text-transform: uppercase; display: block; margin-bottom: 6px;">Enter 6-Digit Release PIN</label>
            <input type="password" id="pinModalCode" class="cr-input" maxlength="6" placeholder="••••••" style="text-align: center; font-size: 22px; letter-spacing: 0.35em; font-family: var(--font-mono);">
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
            crMoreArrow.style.transform = isOpen ? 'rotate(180deg)' : 'rotate(0deg)';
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
        const isMobile = window.innerWidth <= 768;
        const padX = isMobile ? 12 : 36;
        const padY = isMobile ? 12 : 36;
        const rawW = deskViewport && deskViewport.clientWidth > 0 ? deskViewport.clientWidth - padX : (isMobile ? 280 : 600);
        const rawH = deskViewport && deskViewport.clientHeight > 0 ? deskViewport.clientHeight - padY : (isMobile ? 220 : 750);
        const availW = Math.max(rawW, 80);
        const availH = Math.max(rawH, 120);

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

    let currentPreviewId = null;

    function parseClientPageRanges(rangeStr, totalPages) {
        if (!rangeStr || !String(rangeStr).trim()) return Math.max(1, totalPages || 1);
        const selected = new Set();
        const parts = String(rangeStr).split(',');
        for (let part of parts) {
            part = part.trim();
            if (!part) continue;
            if (part.includes('-')) {
                const sub = part.split('-');
                try {
                    const rawS = parseInt(sub[0]) || 1;
                    const rawE = parseInt(sub[1]) || totalPages;
                    if ((rawS > totalPages && rawE > totalPages) || (rawS < 1 && rawE < 1)) continue;
                    const s = Math.max(1, Math.min(totalPages, rawS));
                    const e = Math.max(1, Math.min(totalPages, rawE));
                    const start = Math.min(s, e);
                    const end = Math.max(s, e);
                    for (let i = start; i <= end; i++) selected.add(i);
                } catch (err) {}
            } else {
                try {
                    const p = parseInt(part);
                    if (!isNaN(p) && p >= 1 && p <= totalPages) selected.add(p);
                } catch (err) {}
            }
        }
        return selected.size > 0 ? selected.size : Math.max(1, totalPages || 1);
    }

    function updateEstimatedCost() {
        const copies = parseInt(document.getElementById('printCopies').value) || 1;
        const colorModel = document.getElementById('printColorModel').value;
        const isColor = colorModel === 'RGB' || colorModel === 'Color';
        const rate = isColor ? 5.00 : 2.00;
        
        const totalDocPages = inspectedPageCount || 1;
        const isCustomPages = document.getElementById('printPagesMode') && document.getElementById('printPagesMode').value === 'custom';
        const rangeVal = isCustomPages ? document.getElementById('printPageRange').value : '';
        const activePages = parseClientPageRanges(rangeVal, totalDocPages);

        const isDuplex = document.getElementById('chkDuplex') && document.getElementById('chkDuplex').checked;
        const sheetsPerCopy = isDuplex ? Math.ceil(activePages / 2) : activePages;
        const totalSheets = sheetsPerCopy * copies;
        const totalCost = rate * activePages * copies;

        document.getElementById('costDetails').textContent = `${activePages} page(s) × ${copies} copy(ies) @ ₱${rate.toFixed(2)}/pg`;
        document.getElementById('costTotal').textContent = `₱${totalCost.toFixed(2)}`;
        document.getElementById('costSheets').textContent = `${totalSheets} sheet${totalSheets !== 1 ? 's' : ''} of paper${isDuplex ? ' (Duplex 2-sided)' : ''}`;
    }

    ['printPageSize', 'printOrientation', 'printMarginMode', 'printColorModel', 'printScaling', 'printCopies'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', updatePreviewLayout);
    });
    ['mTop', 'mBottom', 'mLeft', 'mRight'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', updatePreviewLayout);
    });

    ['printPageRange', 'printCopies', 'printColorModel'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('input', updateEstimatedCost);
            el.addEventListener('change', updateEstimatedCost);
        }
    });

    const chkDup = document.getElementById('chkDuplex');
    if (chkDup) chkDup.addEventListener('change', updateEstimatedCost);

    const selDup = document.getElementById('printDuplex');
    if (selDup) selDup.addEventListener('change', updateEstimatedCost);

    const selPagesMode = document.getElementById('printPagesMode');
    if (selPagesMode) selPagesMode.addEventListener('change', updateEstimatedCost);

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

    // File Ingestion & Live Rendering with Preview Caching
    const fileInput = document.getElementById('printFileInput');
    fileInput.addEventListener('change', async () => {
        if (!fileInput.files.length) return;
        const file = fileInput.files[0];
        currentPreviewId = null;

        document.getElementById('printFileName').textContent = file.name;
        document.getElementById('previewDocTitle').textContent = file.name;
        document.getElementById('previewTopToolbar').style.display = 'flex';
        document.getElementById('previewEmptyState').style.display = 'none';
        document.getElementById('previewDimensionsTag').style.display = 'flex';

        const ext = file.name.split('.').pop().toLowerCase();
        const pdfCanvas = document.getElementById('pdfCanvas');
        const imgPreview = document.getElementById('imgPreview');
        const txtPreview = document.getElementById('txtPreview');

        pdfCanvas.style.display = 'none';
        imgPreview.style.display = 'none';
        txtPreview.style.display = 'none';
        document.getElementById('previewPagination').style.display = 'none';

        // Fast client-side render for native web images and local PDF
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
                    inspectedPageCount = pdf.numPages;
                    pdfPage = 1;
                    if (pdfPagesTotal > 1) document.getElementById('previewPagination').style.display = 'flex';
                    renderPdfPage(pdfPage);
                    updateEstimatedCost();
                }).catch(() => {
                    txtPreview.style.display = 'block';
                    txtPreview.textContent = 'PDF ready to print.';
                    updatePreviewLayout();
                });
            };
            reader.readAsArrayBuffer(file);
        } else {
            // Office / HEIC / TIFF files
            txtPreview.style.display = 'block';
            txtPreview.textContent = `Analyzing & converting ${ext.toUpperCase()} document preview...`;
            updatePreviewLayout();
        }

        // Call /api/inspect in background to cache converted PDF and obtain accurate page & cost metrics
        const formData = new FormData();
        formData.append('file', file);
        formData.append('copies', document.getElementById('printCopies').value);
        formData.append('colormodel', document.getElementById('printColorModel').value);
        const isDuplex = document.getElementById('chkDuplex') && document.getElementById('chkDuplex').checked;
        formData.append('duplex', isDuplex ? document.getElementById('printDuplex').value : 'None');
        formData.append('pagerange', document.getElementById('printPageRange').value);

        try {
            const res = await fetch('/api/inspect', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                inspectedPageCount = data.pages;
                currentPreviewId = data.preview_id;
                updateEstimatedCost();

                // If this is an Office document or HEIC, load converted PDF directly into PDF.js!
                if (!['jpg', 'jpeg', 'png', 'webp', 'bmp', 'pdf'].includes(ext) || ext === 'heic' || ext === 'heif' || ext === 'tiff' || ext === 'tif') {
                    if (data.preview_id) {
                        pdfjsLib.getDocument(`/api/preview/${data.preview_id}.pdf`).promise.then(pdf => {
                            currentPdf = pdf;
                            pdfPagesTotal = pdf.numPages;
                            pdfPage = 1;
                            txtPreview.style.display = 'none';
                            if (pdfPagesTotal > 1) document.getElementById('previewPagination').style.display = 'flex';
                            renderPdfPage(pdfPage);
                        }).catch(err => {
                            console.error('PDF.js preview error:', err);
                            txtPreview.style.display = 'block';
                            txtPreview.textContent = `Document (${ext.toUpperCase()}) converted and ready to print.`;
                            updatePreviewLayout();
                        });
                    }
                }
            } else {
                showToast(data.error || 'Failed to inspect document', 'error');
            }
        } catch (e) {
            console.error(e);
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
        currentPreviewId = null;
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

    async function pollJobStatus(jobId, btn) {
        let attempts = 0;
        const maxAttempts = 20; // 30 seconds
        const interval = setInterval(async () => {
            attempts++;
            try {
                const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/status`);
                const data = await res.json();
                if (data.success) {
                    if (data.status === 'printing') {
                        showToast('Printing on ' + selectedPrinter + '...', 'info');
                    } else if (data.status === 'completed') {
                        clearInterval(interval);
                        showToast('Print completed successfully!', 'success');
                        btn.disabled = false;
                        btn.textContent = 'Print';
                        return;
                    }
                }
            } catch (e) {
                // ignore transient network errors
            }
            if (attempts >= maxAttempts) {
                clearInterval(interval);
                showToast('Job submitted to printer queue.', 'success');
                btn.disabled = false;
                btn.textContent = 'Print';
            }
        }, 1500);
    }

    document.getElementById('btnSubmitPrint').addEventListener('click', async () => {
        if (!fileInput.files.length) {
            showToast('Please select a file to print', 'error');
            return;
        }

        const isPin = document.getElementById('chkPinHold').checked;
        const pin = document.getElementById('printPin').value.trim();
        if (isPin && (!pin || pin.length !== 6 || !/^[0-9]{6}$/.test(pin))) {
            showToast('Please enter a 6-digit PIN', 'error');
            return;
        }

        const btn = document.getElementById('btnSubmitPrint');
        btn.disabled = true;
        btn.textContent = isPin ? 'Holding...' : 'Sending...';

        const isDuplex = document.getElementById('chkDuplex') && document.getElementById('chkDuplex').checked;
        const duplexVal = isDuplex ? document.getElementById('printDuplex').value : 'None';

        const formData = new FormData();
        formData.append('file', fileInput.files[0]);
        if (currentPreviewId) {
            formData.append('preview_id', currentPreviewId);
        }
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
        formData.append('duplex', duplexVal);
        formData.append('fitpage', document.getElementById('printScaling').value);
        formData.append('pagerange', document.getElementById('printPageRange').value);
        if (document.getElementById('printNumberUp')) {
            formData.append('number_up', document.getElementById('printNumberUp').value);
        }
        if (document.getElementById('printPageBorder')) {
            formData.append('page_border', document.getElementById('printPageBorder').value);
        }

        const endpoint = isPin ? '/api/print/pin-hold' : '/api/print';
        if (isPin) formData.append('pin', pin);

        showToast(isPin ? 'Encrypting & holding document...' : 'Submitting print job...', 'info');
        try {
            const res = await fetch(endpoint, { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                if (isPin) {
                    showToast('Document securely held in PIN queue! Job ID: ' + (data.job_id || ''), 'success');
                    btn.disabled = false;
                    btn.textContent = 'Hold in PIN';
                } else {
                    const jobId = data.job_id;
                    showToast('Print job queued (ID: ' + jobId + ')', 'info');
                    pollJobStatus(jobId, btn);
                }
            } else {
                showToast('Error: ' + data.error, 'error');
                btn.disabled = false;
                btn.textContent = isPin ? 'Hold in PIN' : 'Print';
            }
        } catch (e) {
            showToast('Failed to reach print server', 'error');
            btn.disabled = false;
            btn.textContent = isPin ? 'Hold in PIN' : 'Print';
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
        const isMobileId = window.innerWidth <= 768;
        const padXId = isMobileId ? 12 : 36;
        const padYId = isMobileId ? 12 : 36;
        const rawWId = idViewport && idViewport.clientWidth > 0 ? idViewport.clientWidth - padXId : (isMobileId ? 280 : 560);
        const rawHId = idViewport && idViewport.clientHeight > 0 ? idViewport.clientHeight - padYId : (isMobileId ? 220 : 750);
        const availW = Math.max(rawWId, 80);
        const availH = Math.max(rawHId, 120);

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

        // Exact 300 DPI Physical Standard Measurements:
        // 2x2 in = 600x600 px (50.8 x 50.8 mm)
        // 1x1 in = 300x300 px (25.4 x 25.4 mm)
        // Passport (Philippine standard 35x45 mm) = 413x531 px
        // Wallet size (2.5x3.5 in) = 750x1050 px
        const s2x2 = 600;
        const s1x1 = 300;
        const passW = 413;
        const passH = 531;
        const wallW = 750;
        const wallH = 1050;

        if (pkg === 'rush_combo_a') {
            if (isA4) {
                // A4: 3 pcs 2x2" on top + 20 pcs 1x1" below
                const startX2 = Math.round((sheetW - (3 * s2x2 + 2 * 60)) / 2);
                const y1 = 140;
                for (let i = 0; i < 3; i++) {
                    drawPhotoItem(startX2 + i * (s2x2 + 60), y1, s2x2, s2x2);
                }

                const startX1 = Math.round((sheetW - (5 * s1x1 + 4 * 50)) / 2);
                const y2 = y1 + s2x2 + 90;
                for (let r = 0; r < 4; r++) {
                    for (let c = 0; c < 5; c++) {
                        drawPhotoItem(startX1 + c * (s1x1 + 50), y2 + r * (s1x1 + 50), s1x1, s1x1);
                    }
                }
            } else {
                // 4x6" Sheet: 2 pcs 2x2" stacked + 7 pcs 1x1" (strictly within 1200x1800)
                const x2 = 80;
                drawPhotoItem(x2, 80, s2x2, s2x2);
                drawPhotoItem(x2, 740, s2x2, s2x2);

                const x1 = 760;
                drawPhotoItem(x1, 80, s1x1, s1x1);
                drawPhotoItem(x1, 410, s1x1, s1x1);
                drawPhotoItem(x1, 740, s1x1, s1x1);
                drawPhotoItem(x1, 1070, s1x1, s1x1);

                const yBottom = 1400;
                drawPhotoItem(80, yBottom, s1x1, s1x1);
                drawPhotoItem(420, yBottom, s1x1, s1x1);
                drawPhotoItem(760, yBottom, s1x1, s1x1);
            }
        } else if (pkg === 'rush_combo_b') {
            if (isA4) {
                // A4: 3 pcs 2x2" + 6 pcs Passport + 10 pcs 1x1"
                const startX2 = Math.round((sheetW - (3 * s2x2 + 2 * 60)) / 2);
                drawPhotoItem(startX2, 120, s2x2, s2x2);
                drawPhotoItem(startX2 + s2x2 + 60, 120, s2x2, s2x2);
                drawPhotoItem(startX2 + 2 * (s2x2 + 60), 120, s2x2, s2x2);

                const pStartX = Math.round((sheetW - (3 * passW + 2 * 60)) / 2);
                const pY = 120 + s2x2 + 80;
                for (let r = 0; r < 2; r++) {
                    for (let c = 0; c < 3; c++) {
                        drawPhotoItem(pStartX + c * (passW + 60), pY + r * (passH + 60), passW, passH);
                    }
                }

                const sStartX1 = Math.round((sheetW - (5 * s1x1 + 4 * 50)) / 2);
                const sY = pY + 2 * (passH + 60) + 40;
                for (let r = 0; r < 2; r++) {
                    for (let c = 0; c < 5; c++) {
                        drawPhotoItem(sStartX1 + c * (s1x1 + 50), sY + r * (s1x1 + 50), s1x1, s1x1);
                    }
                }
            } else {
                // 4x6" Sheet: 2 pcs 2x2" + 2 pcs Passport + 3 pcs 1x1"
                drawPhotoItem(80, 80, s2x2, s2x2);
                drawPhotoItem(80, 720, s2x2, s2x2);

                drawPhotoItem(720, 80, passW, passH);
                drawPhotoItem(720, 680, passW, passH);

                const yBottom = 1380;
                drawPhotoItem(80, yBottom, s1x1, s1x1);
                drawPhotoItem(420, yBottom, s1x1, s1x1);
                drawPhotoItem(760, yBottom, s1x1, s1x1);
            }
        } else if (pkg === 'all_2x2') {
            if (isA4) {
                // A4: 3 cols x 4 rows = 12 pcs of exact 2x2"
                const startX = Math.round((sheetW - (3 * s2x2 + 2 * 60)) / 2);
                const startY = Math.round((sheetH - (4 * s2x2 + 3 * 60)) / 2);
                for (let r = 0; r < 4; r++) {
                    for (let c = 0; c < 3; c++) {
                        drawPhotoItem(startX + c * (s2x2 + 60), startY + r * (s2x2 + 60), s2x2, s2x2);
                    }
                }
            } else {
                // 4x6": 2 pcs 2x2" centered with ample margin
                const startX = Math.round((sheetW - s2x2) / 2);
                drawPhotoItem(startX, 180, s2x2, s2x2);
                drawPhotoItem(startX, 880, s2x2, s2x2);
            }
        } else if (pkg === 'all_1x1') {
            if (isA4) {
                // A4: 6 cols x 8 rows = 48 pcs of exact 1x1"
                const startX = Math.round((sheetW - (6 * s1x1 + 5 * 40)) / 2);
                const startY = Math.round((sheetH - (8 * s1x1 + 7 * 40)) / 2);
                for (let r = 0; r < 8; r++) {
                    for (let c = 0; c < 6; c++) {
                        drawPhotoItem(startX + c * (s1x1 + 40), startY + r * (s1x1 + 40), s1x1, s1x1);
                    }
                }
            } else {
                // 4x6": 3 cols x 4 rows = 12 pcs of exact 1x1"
                const startX = Math.round((sheetW - (3 * s1x1 + 2 * 50)) / 2);
                const startY = Math.round((sheetH - (4 * s1x1 + 3 * 50)) / 2);
                for (let r = 0; r < 4; r++) {
                    for (let c = 0; c < 3; c++) {
                        drawPhotoItem(startX + c * (s1x1 + 50), startY + r * (s1x1 + 50), s1x1, s1x1);
                    }
                }
            }
        } else if (pkg === 'all_passport') {
            if (isA4) {
                // A4: 4 cols x 5 rows = 20 pcs of exact Passport
                const startX = Math.round((sheetW - (4 * passW + 3 * 60)) / 2);
                const startY = Math.round((sheetH - (5 * passH + 4 * 60)) / 2);
                for (let r = 0; r < 5; r++) {
                    for (let c = 0; c < 4; c++) {
                        drawPhotoItem(startX + c * (passW + 60), startY + r * (passH + 60), passW, passH);
                    }
                }
            } else {
                // 4x6": 2 cols x 2 rows = 4 pcs of exact Passport
                const startX = Math.round((sheetW - (2 * passW + 60)) / 2);
                const startY = Math.round((sheetH - (2 * passH + 80)) / 2);
                for (let r = 0; r < 2; r++) {
                    for (let c = 0; c < 2; c++) {
                        drawPhotoItem(startX + c * (passW + 60), startY + r * (passH + 80), passW, passH);
                    }
                }
            }
        } else if (pkg === 'wallet') {
            if (isA4) {
                // A4: 2 cols x 2 rows = 4 pcs of exact Wallet
                const startX = Math.round((sheetW - (2 * wallW + 120)) / 2);
                const startY = Math.round((sheetH - (2 * wallH + 120)) / 2);
                for (let r = 0; r < 2; r++) {
                    for (let c = 0; c < 2; c++) {
                        drawPhotoItem(startX + c * (wallW + 120), startY + r * (wallH + 120), wallW, wallH);
                    }
                }
            } else {
                // 4x6": 1 pc exact Wallet centered
                const startX = Math.round((sheetW - wallW) / 2);
                const startY = Math.round((sheetH - wallH) / 2);
                drawPhotoItem(startX, startY, wallW, wallH);
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
                formData.append('fitpage', 'false');

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
                btn.disabled = false; btn.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><polyline points="6 9 6 2 18 2 18 9"></polyline><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path><rect x="6" y="14" width="12" height="8"></rect></svg> Print ID Sheet';
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
        const tbody = document.getElementById('pinTableBody');
        try {
            const headers = {};
            if (currentAdminToken) headers['X-Admin-Token'] = currentAdminToken;
            const res = await fetch('/api/pin/list', { headers });
            if (res.status === 401) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; padding: 24px; color: var(--text-secondary);"><svg class="icon" viewBox="0 0 24 24" style="width: 20px; height: 20px; margin: 0 auto 8px auto; display: block;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>Administrator login required to inspect held queue. <button class="cr-btn-pill cr-btn-print" style="margin-left: 8px; padding: 3px 10px; font-size: 11px;" onclick="window.switchTab(&quot;settings&quot;)">Log In</button></td></tr>';
                return;
            }
            const data = await res.json();
            tbody.innerHTML = '';
            if (data.jobs && data.jobs.length > 0) {
                data.jobs.forEach(j => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${j.created_at}</td>
                        <td style="font-weight: 500;"><svg class="icon" viewBox="0 0 24 24" style="width: 12px; height: 12px; margin-right: 4px;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>${j.filename}</td>
                        <td>${j.pages}</td>
                        <td>${j.copies}</td>
                        <td>${j.colormodel}</td>
                        <td style="color: var(--accent); font-weight: 500;">₱${Number(j.cost).toFixed(2)}</td>
                        <td><button class="cr-btn-pill cr-btn-print" style="padding: 2px 10px; font-size: 11px;" onclick="openPinModal('${j.job_id}', '${j.filename}')">Release</button></td>
                    `;
                    tbody.appendChild(tr);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-tertiary);">No confidential jobs held in queue</td></tr>';
            }
        } catch (e) {
            console.error(e);
            tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--danger);">Failed to load queue</td></tr>';
        }
    }

    // Direct walk-up release handler
    document.getElementById('btnDirectPinRelease').addEventListener('click', async () => {
        const jobId = document.getElementById('directJobId').value.trim();
        const pin = document.getElementById('directJobPin').value.trim();
        if (!jobId) {
            showToast('Please enter your Job ID', 'error');
            return;
        }
        if (!pin || pin.length !== 6 || !/^[0-9]{6}$/.test(pin)) {
            showToast('PIN must be exactly 6 digits', 'error');
            return;
        }

        const formData = new FormData();
        formData.append('job_id', jobId);
        formData.append('pin', pin);

        showToast('Verifying PIN and unlocking job...', 'info');
        try {
            const res = await fetch('/api/pin/release', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                showToast('Job unlocked and sent to printer!', 'success');
                document.getElementById('directJobId').value = '';
                document.getElementById('directJobPin').value = '';
                if (currentAdminToken) loadPinList();
            } else {
                showToast('Error: ' + data.error, 'error');
            }
        } catch (e) {
            showToast('Direct release request failed', 'error');
        }
    });

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
        if (!pin || pin.length !== 6 || !/^[0-9]{6}$/.test(pin)) {
            showToast('PIN must be exactly 6 digits', 'error');
            return;
        }

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
        const tbody = document.getElementById('historyTableBody');
        try {
            const headers = {};
            if (currentAdminToken) headers['X-Admin-Token'] = currentAdminToken;
            const res = await fetch('/api/reports/ledger', { headers });
            if (res.status === 401) {
                document.getElementById('statTotalJobs').textContent = '--';
                document.getElementById('statTotalPages').textContent = '--';
                document.getElementById('statTotalRevenue').textContent = '--';
                tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; padding: 24px; color: var(--text-secondary);"><svg class="icon" viewBox="0 0 24 24" style="width: 20px; height: 20px; margin: 0 auto 8px auto; display: block;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>Administrator login required to inspect audit ledger. <button class="cr-btn-pill cr-btn-print" style="margin-left: 8px; padding: 3px 10px; font-size: 11px;" onclick="window.switchTab(&quot;settings&quot;)">Log In</button></td></tr>';
                return;
            }
            const data = await res.json();
            if (data.stats) {
                document.getElementById('statTotalJobs').textContent = data.stats.total_jobs;
                document.getElementById('statTotalPages').textContent = data.stats.total_pages;
                document.getElementById('statTotalRevenue').textContent = data.stats.total_revenue;
            }
            tbody.innerHTML = '';
            if (data.jobs && data.jobs.length > 0) {
                const srcBadges = {
                    'portal': '<span style="font-size: 10px; padding: 2px 5px; border-radius: 3px; background: rgba(26, 115, 232, 0.15); color: var(--accent);">Web Portal</span>',
                    'pin_release': '<span style="font-size: 10px; padding: 2px 5px; border-radius: 3px; background: rgba(234, 134, 0, 0.15); color: var(--warning);">PIN Release</span>',
                    'windows_ipp': '<span style="font-size: 10px; padding: 2px 5px; border-radius: 3px; background: rgba(30, 142, 62, 0.15); color: var(--success);">Windows IPP</span>',
                    'mobile_airprint': '<span style="font-size: 10px; padding: 2px 5px; border-radius: 3px; background: rgba(161, 66, 244, 0.15); color: #c084fc;">Mobile AirPrint</span>',
                    'maintenance': '<span style="font-size: 10px; padding: 2px 5px; border-radius: 3px; background: rgba(255, 255, 255, 0.1); color: var(--text-secondary);">Maintenance</span>'
                };
                data.jobs.forEach(j => {
                    const srcHtml = srcBadges[j.source] || `<span style="font-size: 10px; color: var(--text-secondary);">${j.source || 'portal'}</span>`;
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${j.timestamp}</td>
                        <td>${srcHtml}</td>
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
                tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--text-tertiary);">No audit history recorded</td></tr>';
            }
        } catch (e) {
            console.error(e);
            tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--danger);">Failed to load history</td></tr>';
        }
    }

    document.getElementById('btnExportCsv').addEventListener('click', (e) => {
        e.preventDefault();
        if (!currentAdminToken) {
            showToast('Administrator login required to export audit ledger', 'error');
            window.switchTab('settings');
            return;
        }
        window.location.href = '/api/reports/export.csv?token=' + encodeURIComponent(currentAdminToken);
    });

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

        try {
            const res = await fetch('/api/admin/auth', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                currentAdminToken = data.token;
                sessionStorage.setItem('admin_token', currentAdminToken);
                document.getElementById('adminLockedState').style.display = 'none';
                document.getElementById('adminUnlockedState').style.display = 'block';
                document.getElementById('btnAdminLock').innerHTML = '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 9.9-1"></path></svg> <span>Admin</span>';
                if (typeof updateCupsAdminState === 'function') updateCupsAdminState(true);
                showToast('Administrator privileges unlocked', 'success');
            } else {
                showToast(data.error || 'Invalid password', 'error');
            }
        } catch (e) {
            showToast('Authentication request failed', 'error');
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

    // ==========================================
    // INBOX: files sent by Wi-Fi guests through the captive portal
    // ==========================================
    let inboxTimer = null;
    let inboxOpen = false;
    let inboxLastCount = -1;

    function formatBytes(n) {
        if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
        return Math.max(1, Math.round(n / 1024)) + ' KB';
    }

    async function loadInbox() {
        const bar = document.getElementById('inboxBar');
        try {
            if (!currentAdminToken) {
                const c = await (await fetch('/api/inbox/count')).json();
                if (c.success && c.count > 0) {
                    bar.style.display = 'block';
                    document.getElementById('inboxSummary').textContent = c.count + ' file(s) waiting. Unlock Admin (Settings tab) to open them.';
                    document.getElementById('btnInboxToggle').style.display = 'none';
                } else {
                    bar.style.display = 'none';
                }
                return;
            }
            const res = await fetch('/api/inbox/list', { headers: { 'X-Admin-Token': currentAdminToken } });
            if (res.status === 401) { bar.style.display = 'none'; return; }
            const data = await res.json();
            if (!data.success) return;
            bar.style.display = 'block';
            document.getElementById('btnInboxToggle').style.display = '';
            document.getElementById('inboxSummary').textContent = data.files.length
                ? data.files.length + ' file(s) from Wi-Fi guests (kept ' + data.ttl_hours + ' h)'
                : 'Empty. Guests can send files from the Wi-Fi portal.';
            if (data.files.length > inboxLastCount && inboxLastCount >= 0) showToast('New file in the inbox: ' + data.files[0].filename, 'success');
            inboxLastCount = data.files.length;
            renderInbox(data.files);
        } catch (e) {}
    }

    function renderInbox(files) {
        const list = document.getElementById('inboxList');
        list.style.display = (inboxOpen && files.length) ? 'flex' : 'none';
        document.getElementById('btnInboxToggle').textContent = inboxOpen ? 'Hide' : 'Show';
        list.textContent = '';
        files.forEach(f => {
            const row = document.createElement('div');
            row.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;align-items:center;';
            const info = document.createElement('div');
            info.style.cssText = 'flex:1;min-width:180px;';
            const name = document.createElement('div'); name.style.fontWeight = '600'; name.textContent = f.filename;
            const meta = document.createElement('div'); meta.style.cssText = 'font-size:11px;color:var(--text-secondary);';
            meta.textContent = formatBytes(f.size) + ' - received ' + f.received + (f.device ? ' - device ' + f.device : '');
            info.appendChild(name); info.appendChild(meta);
            const bUse = document.createElement('button'); bUse.type = 'button'; bUse.className = 'cr-btn-pill cr-btn-print'; bUse.textContent = 'Open for printing';
            const bDel = document.createElement('button'); bDel.type = 'button'; bDel.className = 'cr-btn-pill cr-btn-cancel'; bDel.style.color = 'var(--danger)'; bDel.textContent = 'Delete';
            bUse.addEventListener('click', async () => {
                bUse.disabled = true;
                try {
                    const res = await fetch('/api/inbox/file?id=' + encodeURIComponent(f.id), { headers: { 'X-Admin-Token': currentAdminToken } });
                    if (!res.ok) { showToast('File no longer available', 'error'); return; }
                    const blob = await res.blob();
                    const dt = new DataTransfer();
                    dt.items.add(new File([blob], f.filename));
                    fileInput.files = dt.files;
                    fileInput.dispatchEvent(new Event('change'));
                    showToast('Loaded ' + f.filename + '. Choose options, then Print.', 'success');
                } catch (e) { showToast('Could not load the file', 'error'); }
                bUse.disabled = false;
            });
            bDel.addEventListener('click', async () => {
                if (!confirm('Delete ' + f.filename + ' from the inbox?')) return;
                const fd = new FormData(); fd.append('id', f.id);
                await fetch('/api/inbox/delete', { method: 'POST', headers: { 'X-Admin-Token': currentAdminToken }, body: fd });
                loadInbox();
            });
            row.appendChild(info); row.appendChild(bUse); row.appendChild(bDel);
            list.appendChild(row);
        });
    }

    document.getElementById('btnInboxToggle').addEventListener('click', () => { inboxOpen = !inboxOpen; loadInbox(); });
    loadInbox();
    inboxTimer = setInterval(loadInbox, 10000);

    // ==========================================
    // CUPS HARDWARE COMMAND CENTER
    // ==========================================
    let cupsQueueTimer = null;
    let cachedCupsData = null;

    function updateCupsAdminState(isUnlocked) {
        const banner = document.getElementById('cupsAdminBanner');
        if (banner) banner.style.display = isUnlocked ? 'none' : 'flex';
        
        document.querySelectorAll('.cups-admin-req').forEach(el => {
            el.disabled = !isUnlocked;
            el.style.opacity = isUnlocked ? '1' : '0.55';
            el.style.cursor = isUnlocked ? 'pointer' : 'not-allowed';
        });

        const formInputs = document.querySelectorAll('#cupsDefaultsForm select, #cupsDefaultsForm input');
        formInputs.forEach(el => {
            el.disabled = !isUnlocked;
        });
    }

    async function loadCupsOverview(printerName) {
        const pSelect = document.getElementById('cupsPrinterSelect');
        const selected = printerName || (pSelect ? pSelect.value : '');
        const url = '/api/cups/overview' + (selected ? '?printer=' + encodeURIComponent(selected) : '');
        
        try {
            const res = await fetch(url);
            const data = await res.json();
            if (!data.success) {
                showToast('Failed to query CUPS status: ' + (data.error || 'Unknown error'), 'error');
                return;
            }
            cachedCupsData = data;
            renderCupsOverview(data);
        } catch (e) {
            console.error("CUPS overview error:", e);
        }
    }

    function renderCupsOverview(data) {
        // Populate printer select
        const pSelect = document.getElementById('cupsPrinterSelect');
        if (pSelect && data.printers) {
            const curVal = data.selected_printer;
            pSelect.innerHTML = '';
            data.printers.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.name;
                opt.textContent = p.name + (p.is_default ? ' (Default)' : '');
                if (p.name === curVal) opt.selected = true;
                pSelect.appendChild(opt);
            });
        }

        // Status badge
        const badge = document.getElementById('cupsStatusBadge');
        if (badge) {
            const st = (data.info.status || 'idle').toLowerCase();
            badge.textContent = data.info.status_text || 'Ready';
            if (st === 'printing' || st === 'processing') {
                badge.style.background = 'rgba(26, 115, 232, 0.2)';
                badge.style.color = 'var(--accent)';
            } else if (st === 'stopped' || st === 'disabled') {
                badge.style.background = 'rgba(217, 48, 37, 0.2)';
                badge.style.color = 'var(--danger)';
            } else {
                badge.style.background = 'rgba(30, 142, 62, 0.2)';
                badge.style.color = 'var(--success)';
            }
        }

        // Hardware details
        const elUri = document.getElementById('cupsInfoUri');
        if (elUri) elUri.textContent = data.info.uri || 'Direct USB / Network Interface';
        const elDriver = document.getElementById('cupsInfoDriver');
        if (elDriver) elDriver.textContent = data.info.make_and_model || 'Standard CUPS Driver';
        const elAcc = document.getElementById('cupsInfoAccepting');
        if (elAcc) {
            elAcc.textContent = data.info.accepting ? 'Accepting Jobs' : 'Rejecting Jobs';
            elAcc.style.color = data.info.accepting ? 'var(--success)' : 'var(--warning)';
        }
        const elShared = document.getElementById('cupsInfoShared');
        if (elShared) {
            elShared.textContent = data.info.shared ? 'Shared Network Queue (IPP/mDNS)' : 'Local Only (Not Shared)';
            elShared.style.color = data.info.shared ? 'var(--accent)' : 'var(--text-secondary)';
        }

        // Action buttons labels
        const btnPause = document.getElementById('btnCupsPauseResume');
        if (btnPause) {
            const isStopped = (data.info.status || '').toLowerCase().includes('stop') || (data.info.status || '').toLowerCase().includes('disable');
            btnPause.innerHTML = isStopped
                ? '<svg class="icon" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> Resume Printer'
                : '<svg class="icon" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg> Pause Printer';
            btnPause.setAttribute('data-action', isStopped ? 'resume' : 'pause');
        }

        const btnAccept = document.getElementById('btnCupsAcceptReject');
        if (btnAccept) {
            btnAccept.innerHTML = data.info.accepting
                ? '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg> Reject Jobs'
                : '<svg class="icon" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"></polyline></svg> Accept Jobs';
            btnAccept.setAttribute('data-action', data.info.accepting ? 'reject' : 'accept');
        }

        // Defaults Form
        const defs = data.defaults || {};
        const setVal = (id, val) => {
            const el = document.getElementById(id);
            if (!el) return;
            if (val) {
                let found = false;
                for (let i = 0; i < el.options.length; i++) {
                    if (el.options[i].value.toLowerCase() === String(val).toLowerCase()) {
                        el.selectedIndex = i;
                        found = true;
                        break;
                    }
                }
                if (!found && String(val).trim()) {
                    const opt = document.createElement('option');
                    opt.value = val;
                    opt.textContent = val;
                    opt.selected = true;
                    el.appendChild(opt);
                }
            }
        };

        setVal('cupsDefOutputOrder', defs.outputorder || 'reverse');
        setVal('cupsDefPageSize', defs.pagesize || 'A4');
        setVal('cupsDefMediaType', defs.mediatype || 'Plain');
        setVal('cupsDefQuality', defs.quality || 'Normal');
        setVal('cupsDefColorModel', defs.colormodel || 'RGB');
        setVal('cupsDefScaling', defs.scaling || 'none');
        setVal('cupsDefDuplex', defs.duplex || 'None');
        setVal('cupsDefResolution', defs.resolution ? defs.resolution.replace('dpi', '') : '600');

        // Render Spool Queue
        renderCupsQueue(data.queue || []);

        // Render Completed Jobs (24-Hour Spool Retention)
        renderCupsCompletedJobs(data.completed_jobs || []);

        // Anti clog setting
        const chkAnti = document.getElementById('chkAutoAntiClog');
        if (chkAnti && data.auto_anti_clog !== undefined) {
            chkAnti.checked = (data.auto_anti_clog === '1' || data.auto_anti_clog === 'on' || data.auto_anti_clog === true);
        }

        // Load network companion daemons
        loadNetworkServices();

        // Render Advanced PPD options
        renderCupsPpdOptions(data.ppd_options || []);

        // Sync admin lock state
        updateCupsAdminState(!!currentAdminToken);
    }

    function renderCupsQueue(queue) {
        const tbody = document.getElementById('cupsQueueBody');
        if (!tbody) return;
        tbody.innerHTML = '';
        if (!queue || queue.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; padding: 24px; color: var(--text-secondary);"><svg class="icon" viewBox="0 0 24 24" style="stroke: var(--success); width: 20px; height: 20px; display: block; margin: 0 auto 6px auto;"><polyline points="20 6 9 17 4 12"></polyline></svg>Spool queue is empty. Printer is ready.</td></tr>';
            return;
        }

        queue.forEach(job => {
            const tr = document.createElement('tr');
            tr.style.borderBottom = '1px solid var(--border)';
            
            const isProcessing = (job.status || '').toLowerCase().includes('print') || (job.status || '').toLowerCase().includes('proc');
            const statusColor = isProcessing ? 'var(--accent)' : 'var(--text-secondary)';

            tr.innerHTML = `
                <td style="padding: 8px 10px; font-family: var(--font-mono); font-weight: 600;">${job.job_id}</td>
                <td style="padding: 8px 10px;">${job.user}</td>
                <td style="padding: 8px 10px;">${job.size} bytes</td>
                <td style="padding: 8px 10px; color: var(--text-secondary); font-size: 11px;">${job.date}</td>
                <td style="padding: 8px 10px;"><span style="color: ${statusColor}; font-weight: 500;">${job.status}</span></td>
                <td style="padding: 8px 10px; text-align: right; white-space: nowrap;">
                    ${job.is_held 
                        ? `<button type="button" class="cr-btn-pill cr-btn-print cups-admin-req" style="padding: 2px 8px; font-size: 11px; margin-right: 4px;" onclick="releaseCupsJob('${job.job_id}')">Release</button>`
                        : `<button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" style="padding: 2px 8px; font-size: 11px; margin-right: 4px;" onclick="holdCupsJob('${job.job_id}')">Hold</button>`
                    }
                    <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" style="color: var(--danger); padding: 2px 8px; font-size: 11px;" onclick="cancelCupsJob('${job.job_id}')">
                        Cancel
                    </button>
                </td>
            `;
            tbody.appendChild(tr);
        });

        updateCupsAdminState(!!currentAdminToken);
    }

    function renderCupsPpdOptions(ppdOptions) {
        const grid = document.getElementById('cupsPpdGrid');
        const countBadge = document.getElementById('cupsPpdCountBadge');
        if (!grid) return;
        grid.innerHTML = '';
        if (countBadge) countBadge.textContent = ppdOptions.length + ' options';

        if (!ppdOptions || ppdOptions.length === 0) {
            grid.innerHTML = '<div style="grid-column: 1 / -1; color: var(--text-secondary); font-size: 12px; padding: 10px 0;">No raw PPD options reported by driver.</div>';
            return;
        }

        ppdOptions.forEach(opt => {
            const col = document.createElement('div');
            col.style.fontSize = '12px';
            
            const label = document.createElement('label');
            label.style.cssText = 'display: block; font-weight: 600; margin-bottom: 4px; color: var(--text-secondary); font-size: 11px;';
            label.textContent = opt.label + ' (' + opt.name + ')';
            
            const sel = document.createElement('select');
            sel.className = 'cr-select raw-ppd-input';
            sel.setAttribute('data-ppd-key', opt.name);
            sel.style.width = '100%';

            opt.choices.forEach(ch => {
                const o = document.createElement('option');
                o.value = ch;
                o.textContent = ch + (ch === opt.current ? ' *' : '');
                if (ch === opt.current) o.selected = true;
                sel.appendChild(o);
            });

            col.appendChild(label);
            col.appendChild(sel);
            grid.appendChild(col);
        });

        updateCupsAdminState(!!currentAdminToken);
    }

    async function executeCupsAction(action, extraParams = {}) {
        if (!currentAdminToken) {
            showToast('Administrator privileges required to control printer', 'error');
            window.switchTab('settings');
            return;
        }

        const printer = document.getElementById('cupsPrinterSelect') ? document.getElementById('cupsPrinterSelect').value : '';
        const fd = new FormData();
        fd.append('action', action);
        fd.append('printer', printer);
        fd.append('token', currentAdminToken);
        for (let k in extraParams) fd.append(k, extraParams[k]);

        try {
            const res = await fetch('/api/cups/action', {
                method: 'POST',
                headers: { 'X-Admin-Token': currentAdminToken },
                body: fd
            });
            const data = await res.json();
            if (data.success) {
                showToast(data.message, 'success');
                loadCupsOverview();
            } else {
                showToast('Action failed: ' + (data.error || 'Unknown error'), 'error');
            }
        } catch (e) {
            showToast('Network error while sending CUPS action', 'error');
        }
    }

    async function saveCupsDefaults() {
        if (!currentAdminToken) {
            showToast('Administrator privileges required to save CUPS defaults', 'error');
            window.switchTab('settings');
            return;
        }

        const printer = document.getElementById('cupsPrinterSelect') ? document.getElementById('cupsPrinterSelect').value : '';
        const payload = {
            printer: printer,
            outputorder: document.getElementById('cupsDefOutputOrder').value,
            pagesize: document.getElementById('cupsDefPageSize').value,
            mediatype: document.getElementById('cupsDefMediaType').value,
            quality: document.getElementById('cupsDefQuality').value,
            colormodel: document.getElementById('cupsDefColorModel').value,
            scaling: document.getElementById('cupsDefScaling').value,
            duplex: document.getElementById('cupsDefDuplex').value,
            resolution: document.getElementById('cupsDefResolution').value
        };

        try {
            const res = await fetch('/api/cups/settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Admin-Token': currentAdminToken
                },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.success) {
                showToast(data.message, 'success');
                loadCupsOverview();
            } else {
                showToast('Failed to save CUPS defaults: ' + (data.error || 'Unknown error'), 'error');
            }
        } catch (e) {
            showToast('Network error while saving CUPS defaults', 'error');
        }
    }

    async function applyRawPpdOptions() {
        if (!currentAdminToken) {
            showToast('Administrator privileges required to modify PPD options', 'error');
            window.switchTab('settings');
            return;
        }

        const printer = document.getElementById('cupsPrinterSelect') ? document.getElementById('cupsPrinterSelect').value : '';
        const raw_options = {};
        document.querySelectorAll('.raw-ppd-input').forEach(sel => {
            const key = sel.getAttribute('data-ppd-key');
            if (key) raw_options[key] = sel.value;
        });

        try {
            const res = await fetch('/api/cups/settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Admin-Token': currentAdminToken
                },
                body: JSON.stringify({ printer: printer, raw_options: raw_options })
            });
            const data = await res.json();
            if (data.success) {
                showToast('Advanced PPD options updated', 'success');
                loadCupsOverview();
            } else {
                showToast('PPD update failed: ' + (data.error || 'Unknown error'), 'error');
            }
        } catch (e) {
            showToast('Network error while applying PPD options', 'error');
        }
    }

    window.cancelCupsJob = function(jobId) {
        if (!confirm('Cancel print job ' + jobId + '?')) return;
        executeCupsAction('cancel_job', { job_id: jobId });
    };

    // CUPS Event Listeners
    const pSel = document.getElementById('cupsPrinterSelect');
    if (pSel) pSel.addEventListener('change', (e) => loadCupsOverview(e.target.value));

    const btnRefCups = document.getElementById('btnRefreshCups');
    if (btnRefCups) btnRefCups.addEventListener('click', () => loadCupsOverview());

    const btnRefQueue = document.getElementById('btnRefreshQueueManual');
    if (btnRefQueue) btnRefQueue.addEventListener('click', () => loadCupsOverview());

    const btnSaveDefs = document.getElementById('btnSaveCupsDefaults');
    if (btnSaveDefs) btnSaveDefs.addEventListener('click', saveCupsDefaults);

    const btnPauseRes = document.getElementById('btnCupsPauseResume');
    if (btnPauseRes) btnPauseRes.addEventListener('click', () => {
        const action = btnPauseRes.getAttribute('data-action') || 'pause';
        executeCupsAction(action);
    });

    const btnAccRej = document.getElementById('btnCupsAcceptReject');
    if (btnAccRej) btnAccRej.addEventListener('click', () => {
        const action = btnAccRej.getAttribute('data-action') || 'reject';
        executeCupsAction(action);
    });

    const btnTestPg = document.getElementById('btnCupsTestPage');
    if (btnTestPg) btnTestPg.addEventListener('click', () => executeCupsAction('test_page'));

    const btnRestartCupsDaemon = document.getElementById('btnCupsRestartDaemon');
    if (btnRestartCupsDaemon) btnRestartCupsDaemon.addEventListener('click', () => executeCupsAction('restart_cups'));

    const btnPurgeQ = document.getElementById('btnPurgeQueue');
    if (btnPurgeQ) btnPurgeQ.addEventListener('click', () => {
        if (confirm('Warning: Are you sure you want to purge ALL queued print jobs from the spooler?')) {
            executeCupsAction('purge_queue');
        }
    });

    const cupsPpdToggle = document.getElementById('cupsPpdToggle');
    const cupsPpdContent = document.getElementById('cupsPpdContent');
    const cupsPpdArrow = document.getElementById('cupsPpdArrow');
    if (cupsPpdToggle && cupsPpdContent) {
        cupsPpdToggle.addEventListener('click', () => {
            const isOpen = cupsPpdContent.style.display !== 'none';
            cupsPpdContent.style.display = isOpen ? 'none' : 'block';
            if (cupsPpdArrow) cupsPpdArrow.style.transform = isOpen ? 'rotate(0deg)' : 'rotate(180deg)';
        });
    }

    const btnApplyPpd = document.getElementById('btnApplyRawPpdOptions');
    if (btnApplyPpd) btnApplyPpd.addEventListener('click', applyRawPpdOptions);

    const btnAdminHdr = document.getElementById('btnAdminLock');
    if (btnAdminHdr) btnAdminHdr.addEventListener('click', () => window.switchTab('settings'));

    // ==========================================
    // MODULE 3, 4, 5, 6: CUPS COMMAND CENTER EXTENSIONS
    // ==========================================
    const CUPS_PROFILES = {
        'standard_docs': {
            pagesize: 'A4',
            mediatype: 'Plain',
            quality: 'Normal',
            colormodel: 'KGray',
            outputorder: 'reverse',
            scaling: 'none',
            duplex: 'None',
            resolution: '600'
        },
        'eco_draft': {
            pagesize: 'A4',
            mediatype: 'Plain',
            quality: 'Draft',
            colormodel: 'KGray',
            outputorder: 'reverse',
            scaling: 'fit',
            duplex: 'None',
            resolution: '300'
        },
        'glossy_photo': {
            pagesize: '4x6in',
            mediatype: 'Glossy',
            quality: 'Photo',
            colormodel: 'RGB',
            outputorder: 'normal',
            scaling: 'fill',
            duplex: 'None',
            resolution: '1200'
        },
        'certificate': {
            pagesize: 'A4',
            mediatype: 'CoatedMatte',
            quality: 'Best',
            colormodel: 'RGB',
            outputorder: 'reverse',
            scaling: 'none',
            duplex: 'None',
            resolution: '600'
        }
    };

    function applyCupsProfile(profileKey) {
        const p = CUPS_PROFILES[profileKey];
        if (!p) return;

        const setVal = (id, val) => {
            const el = document.getElementById(id);
            if (!el) return;
            for (let i = 0; i < el.options.length; i++) {
                if (el.options[i].value.toLowerCase() === String(val).toLowerCase()) {
                    el.selectedIndex = i;
                    break;
                }
            }
        };

        setVal('cupsDefOutputOrder', p.outputorder);
        setVal('cupsDefPageSize', p.pagesize);
        setVal('cupsDefMediaType', p.mediatype);
        setVal('cupsDefQuality', p.quality);
        setVal('cupsDefColorModel', p.colormodel);
        setVal('cupsDefScaling', p.scaling);
        setVal('cupsDefDuplex', p.duplex);
        setVal('cupsDefResolution', p.resolution);

        showToast(`Preset "${profileKey.replace('_', ' ').toUpperCase()}" loaded into CUPS defaults. Click "Save CUPS Defaults" to commit to all devices.`, 'info');
    }

    function renderCupsCompletedJobs(completed) {
        const tbody = document.getElementById('cupsCompletedJobsBody');
        if (!tbody) return;
        tbody.innerHTML = '';
        if (!completed || completed.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; padding: 20px; color: var(--text-secondary);">No completed jobs retained in spool cache.</td></tr>';
            return;
        }

        completed.forEach(job => {
            const tr = document.createElement('tr');
            tr.style.borderBottom = '1px solid var(--border)';
            tr.innerHTML = `
                <td style="padding: 8px 10px; font-family: var(--font-mono); font-weight: 600;">${job.job_id}</td>
                <td style="padding: 8px 10px;">${job.user}</td>
                <td style="padding: 8px 10px;">${job.size}</td>
                <td style="padding: 8px 10px; color: var(--text-secondary); font-size: 11px;">${job.date}</td>
                <td style="padding: 8px 10px;"><span style="color: var(--success); font-weight: 500;">${job.status}</span></td>
                <td style="padding: 8px 10px; text-align: right;">
                    <button type="button" class="cr-btn-pill cr-btn-print cups-admin-req" style="padding: 2px 10px; font-size: 11px;" onclick="reprintCupsJob('${job.job_id}')">
                        <svg class="icon" viewBox="0 0 24 24"><polyline points="6 9 6 2 18 2 18 9"></polyline><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path><rect x="6" y="14" width="12" height="8"></rect></svg> Reprint
                    </button>
                </td>
            `;
            tbody.appendChild(tr);
        });

        updateCupsAdminState(!!currentAdminToken);
    }

    window.holdCupsJob = function(jobId) {
        executeCupsAction('hold_job', { job_id: jobId });
    };

    window.releaseCupsJob = function(jobId) {
        executeCupsAction('release_job', { job_id: jobId });
    };

    window.reprintCupsJob = function(jobId) {
        if (!confirm(`Reprint preserved spool job ${jobId}?`)) return;
        executeCupsAction('reprint_job', { job_id: jobId });
    };

    window.runNozzleCheck = function() {
        if (!confirm('Print CMYK Vector Printhead Nozzle Check pattern on current printer?')) return;
        executeCupsAction('nozzle_check');
    };

    window.runUsbReset = function() {
        if (!confirm('Execute kernel USB port reset for HP Smart Tank 520 (03f0:4554)?')) return;
        executeCupsAction('usb_reset');
    };

    window.toggleAntiClog = function(enabled) {
        executeCupsAction('toggle_anti_clog', { enabled: enabled ? '1' : '0' });
    };

    async function loadNetworkServices() {
        const grid = document.getElementById('networkServicesGrid');
        if (!grid) return;
        try {
            const res = await fetch('/api/network/services');
            const data = await res.json();
            if (data.success && data.services) {
                grid.innerHTML = '';
                data.services.forEach(svc => {
                    const col = document.createElement('div');
                    col.style.cssText = 'background: var(--surface-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 12px; display: flex; flex-direction: column; justify-content: space-between; gap: 8px;';
                    
                    const statusColor = svc.active ? 'var(--success)' : 'var(--danger)';
                    const statusBg = svc.active ? 'rgba(30, 142, 62, 0.2)' : 'rgba(217, 48, 37, 0.2)';
                    
                    col.innerHTML = `
                        <div>
                            <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 4px;">
                                <strong style="font-size: 13px; color: var(--text-primary);">${svc.name}</strong>
                                <span class="badge" style="background: ${statusBg}; color: ${statusColor}; font-size: 10px;">${svc.status}</span>
                            </div>
                            <code style="font-size: 10px; color: var(--text-secondary);">${svc.service}</code>
                        </div>
                        <button type="button" class="cr-btn-pill cr-btn-cancel cups-admin-req" style="padding: 3px 8px; font-size: 11px; align-self: flex-start;" onclick="restartNetworkService('${svc.id}')">
                            <svg class="icon" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg> Restart
                        </button>
                    `;
                    grid.appendChild(col);
                });
                updateCupsAdminState(!!currentAdminToken);
            }
        } catch (e) {
            console.error("Failed to load network services:", e);
        }
    }

    window.restartNetworkService = async function(svcId) {
        if (!currentAdminToken) {
            showToast('Administrator privileges required to restart network services', 'error');
            window.switchTab('settings');
            return;
        }

        const fd = new FormData();
        fd.append('service', svcId);
        try {
            const res = await fetch('/api/network/services/restart', {
                method: 'POST',
                headers: { 'X-Admin-Token': currentAdminToken },
                body: fd
            });
            const data = await res.json();
            if (data.success) {
                showToast(data.message, 'success');
                setTimeout(loadNetworkServices, 1200);
            } else {
                showToast('Failed to restart service: ' + (data.error || 'Unknown error'), 'error');
            }
        } catch (e) {
            showToast('Network error while restarting service', 'error');
        }
    };

    // Queue auto-refresh
    cupsQueueTimer = setInterval(() => {
        const tabCups = document.getElementById('tab-cups');
        const autoCheck = document.getElementById('chkAutoRefreshQueue');
        if (tabCups && tabCups.classList.contains('active') && autoCheck && autoCheck.checked) {
            loadCupsOverview();
        }
    }, 5000);

    // Initial boot
    if (currentAdminToken) {
        document.getElementById('adminLockedState').style.display = 'none';
        document.getElementById('adminUnlockedState').style.display = 'block';
        document.getElementById('btnAdminLock').innerHTML = '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 9.9-1"></path></svg> <span>Admin</span>';
        updateCupsAdminState(true);
    } else {
        updateCupsAdminState(false);
    }
    loadPrinters();
    updatePreviewLayout();
</script>
</body>
</html>"""

def cleanup_expired_jobs():
    now = int(time.time())
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT job_id, file_path FROM pinned_jobs WHERE expires_at < ?", (now,))
        for r in cur.fetchall():
            try:
                if os.path.exists(r['file_path']):
                    os.remove(r['file_path'])
            except Exception:
                pass
        cur.execute("DELETE FROM pinned_jobs WHERE expires_at < ?", (now,))
        cur.execute("SELECT id, file_path FROM inbox WHERE received_at < ?", (now - INBOX_TTL_SECONDS,))
        for r in cur.fetchall():
            try:
                if os.path.exists(r['file_path']):
                    os.remove(r['file_path'])
            except Exception:
                pass
        cur.execute("DELETE FROM inbox WHERE received_at < ?", (now - INBOX_TTL_SECONDS,))
        conn.commit()

        # Clean preview cache files older than 1 hour (3600s)
        if os.path.exists(SPOOL_PREVIEWS_DIR):
            for fname in os.listdir(SPOOL_PREVIEWS_DIR):
                fpath = os.path.join(SPOOL_PREVIEWS_DIR, fname)
                try:
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath) > 3600):
                        os.remove(fpath)
                except Exception:
                    pass
    except Exception:
        pass

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/api/status')
def api_status():
    cleanup_expired_jobs()
    temp_c = 0
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp_c = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass

    ram_used = 0
    ram_total = 0
    try:
        res = subprocess.run(['free', '-m'], capture_output=True, text=True, timeout=5)
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
        res = subprocess.run(['uptime', '-p'], capture_output=True, text=True, timeout=5)
        uptime_str = res.stdout.strip()
    except Exception:
        pass

    return jsonify({
        'temp_c': temp_c,
        'ram_used': ram_used,
        'ram_total': ram_total,
        'disk_free': disk_free,
        'cpu_load': cpu_load,
        'uptime': uptime_str,
        'public_print': is_public_print_enabled()
    })

@app.route('/api/printers')
def api_printers():
    printers, def_p = get_printers()
    return jsonify({'printers': printers, 'default': def_p})

# ============================================================
# CUPS COMMAND CENTER: BACKEND HELPERS & ENDPOINTS
# ============================================================
def find_cups_binary(name: str) -> str:
    """Finds binary in standard system binary paths or PATH."""
    for p in ['/usr/sbin', '/sbin', '/usr/bin', '/bin']:
        candidate = os.path.join(p, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name) or name

def run_cups_cmd(cmd_args: list, timeout=10) -> subprocess.CompletedProcess:
    """Runs a command ensuring /usr/sbin is present in PATH and resolving binary."""
    env = os.environ.copy()
    env['PATH'] = f"/usr/sbin:/sbin:/usr/bin:/bin:{env.get('PATH', '')}"
    bin_name = cmd_args[0]
    resolved = find_cups_binary(bin_name)
    exec_args = [resolved] + cmd_args[1:]
    try:
        return subprocess.run(exec_args, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        # Fallback for dev environments without CUPS binaries
        return subprocess.CompletedProcess(args=exec_args, returncode=0, stdout='', stderr='')

def parse_lpoptions_l(raw_output: str) -> list:
    """Parses `lpoptions -l` output into structured options with choices and current default choice."""
    options = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        header, choices_str = line.split(':', 1)
        if '/' in header:
            opt_name, opt_label = header.split('/', 1)
        else:
            opt_name = header.strip()
            opt_label = opt_name
        opt_name = opt_name.strip()
        opt_label = opt_label.strip()

        choices = []
        current = None
        for item in choices_str.split():
            item = item.strip()
            if not item:
                continue
            if item.startswith('*'):
                val = item[1:]
                current = val
                choices.append(val)
            else:
                choices.append(item)

        options.append({
            'name': opt_name,
            'label': opt_label,
            'current': current,
            'choices': choices
        })
    return options

def update_ppd_outputorder(printer: str, order: str):
    """Updates PPD DefaultOutputOrder and PageStackOrder keywords and tests with cupstestppd."""
    ppd_path = f"/etc/cups/ppd/{printer}.ppd"
    if not os.path.exists(ppd_path):
        return
    cap_order = 'Reverse' if order.lower() == 'reverse' else 'Normal'
    try:
        with open(ppd_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        if re.search(r'^\*DefaultOutputOrder:\s*\w+', content, re.MULTILINE):
            content = re.sub(r'^\*DefaultOutputOrder:\s*\w+', f'*DefaultOutputOrder: {cap_order}', content, flags=re.MULTILINE)
        else:
            content += f"\n*DefaultOutputOrder: {cap_order}\n"

        if re.search(r'^\*PageStackOrder:\s*\w+', content, re.MULTILINE):
            content = re.sub(r'^\*PageStackOrder:\s*\w+', f'*PageStackOrder: {cap_order}', content, flags=re.MULTILINE)
        else:
            content += f"*PageStackOrder: {cap_order}\n"

        temp_ppd = ppd_path + '.tmp'
        with open(temp_ppd, 'w', encoding='utf-8') as f:
            f.write(content)

        test_res = run_cups_cmd(['cupstestppd', '-q', temp_ppd])
        if test_res.returncode == 0:
            shutil.move(temp_ppd, ppd_path)
            try:
                os.chmod(ppd_path, 0o664)
            except Exception:
                pass
        else:
            if os.path.exists(temp_ppd):
                os.remove(temp_ppd)
    except Exception:
        pass

def update_system_lpoptions(printer: str, options_dict: dict):
    """Updates /etc/cups/lpoptions with system-wide default options for printer."""
    lpoptions_path = '/etc/cups/lpoptions'
    lines = []
    target_prefix = f'Default {printer}'
    dest_prefix = f'Dest {printer}'
    found = False

    if os.path.exists(lpoptions_path):
        try:
            with open(lpoptions_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            lines = []

    new_lines = []
    for line in lines:
        if line.startswith(target_prefix) or line.startswith(dest_prefix):
            tokens = line.strip().split()
            prefix = tokens[0] + ' ' + tokens[1]
            existing_opts = {}
            for t in tokens[2:]:
                if '=' in t:
                    k, v = t.split('=', 1)
                    existing_opts[k] = v
            existing_opts.update(options_dict)
            opt_str = ' '.join(f"{k}={v}" for k, v in existing_opts.items())
            new_lines.append(f"{prefix} {opt_str}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        opt_str = ' '.join(f"{k}={v}" for k, v in options_dict.items())
        new_lines.append(f"Default {printer} {opt_str}\n")

    try:
        with open(lpoptions_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        try:
            os.chmod(lpoptions_path, 0o664)
        except Exception:
            pass
    except Exception:
        pass

def parse_cups_control_file(c_path: str) -> dict:
    """Parses raw CUPS job control file (e.g. /var/spool/cups/c00016) for IPP attributes."""
    if not os.path.exists(c_path):
        return {}
    try:
        with open(c_path, 'rb') as f:
            data = f.read().decode('latin1', errors='ignore')
    except Exception:
        return {}

    info = {}
    m_name = re.search(r'job-name.([^\x00-\x1f\x7f-\xff]+?)(?:job-|media-|time-|attributes-|\x00|\x01|\x02|\x03|\x04|\x05)', data)
    if m_name:
        info['title'] = m_name.group(1).strip()
    m_user = re.search(r'job-originating-user-name.([^\x00-\x1f\x7f-\xff]+)', data)
    if m_user:
        info['user'] = m_user.group(1).strip()
    m_host = re.search(r'job-originating-host-name.([^\x00-\x1f\x7f-\xff]+)', data)
    if m_host:
        info['host'] = m_host.group(1).strip()
    m_color = re.search(r'print-color-mode.([a-zA-Z0-9_-]+)', data)
    if m_color:
        info['color'] = m_color.group(1).strip()
    m_sheets = re.search(r'job-media-sheets-completed[!#\x00-\x05]+(\d+)', data)
    if m_sheets:
        try:
            info['pages'] = int(m_sheets.group(1))
        except Exception:
            pass
    return info

def sync_cups_ledger():
    """
    Parses CUPS page_log and completed queue to synchronize direct prints
    (Windows IPP, Android Mopria, iOS AirPrint) into SQLite print_ledger.
    """
    try:
        conn = get_db()
        jobs_from_log = {}
        for p_path in ['/var/log/cups/page_log', '/var/log/cups/page_log.1']:
            if not os.path.exists(p_path):
                continue
            try:
                with open(p_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        parts = line.split()
                        if len(parts) >= 6:
                            try:
                                printer_name = parts[0]
                                user_name = parts[1]
                                job_id = int(parts[2])
                                date_m = re.search(r'\[([^\]]+)\]', line)
                                rest = line[date_m.end():].strip() if date_m else ''
                                rest_parts = rest.split()
                                page_num = int(rest_parts[0]) if len(rest_parts) > 0 and rest_parts[0].isdigit() else 1
                                num_copies = int(rest_parts[1]) if len(rest_parts) > 1 and rest_parts[1].isdigit() else 1
                                host = rest_parts[3] if len(rest_parts) > 3 else ''
                                title_m = re.search(r'"([^"]+)"', rest)
                                title = title_m.group(1) if title_m else (rest_parts[4] if len(rest_parts) > 4 else f"Print Job #{job_id}")

                                if job_id not in jobs_from_log:
                                    jobs_from_log[job_id] = {
                                        'printer': printer_name,
                                        'user': user_name,
                                        'job_id': job_id,
                                        'max_page': page_num,
                                        'page_count': 1,
                                        'copies': num_copies,
                                        'host': host,
                                        'title': title
                                    }
                                else:
                                    jobs_from_log[job_id]['max_page'] = max(jobs_from_log[job_id]['max_page'], page_num)
                                    jobs_from_log[job_id]['page_count'] += 1
                                    jobs_from_log[job_id]['copies'] = max(jobs_from_log[job_id]['copies'], num_copies)
                            except Exception:
                                continue
            except Exception:
                pass

        completed_lpstat = {}
        try:
            res = subprocess.run(['lpstat', '-W', 'completed', '-o'], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and '-' in parts[0]:
                        try:
                            jid = int(parts[0].split('-')[-1])
                            user = parts[1]
                            size = int(parts[2]) if parts[2].isdigit() else 0
                            completed_lpstat[jid] = {'user': user, 'size': size, 'dest': parts[0]}
                        except Exception:
                            pass
        except Exception:
            pass

        cur = conn.cursor()
        cur.execute("SELECT cups_job_id FROM print_ledger WHERE cups_job_id IS NOT NULL")
        existing_cups_jids = set(r['cups_job_id'] for r in cur.fetchall())

        all_jids = set(jobs_from_log.keys()).union(set(completed_lpstat.keys()))
        new_synced = 0
        for jid in sorted(all_jids):
            if jid in existing_cups_jids:
                continue

            c_path = f"/var/spool/cups/c{jid:05d}"
            c_meta = parse_cups_control_file(c_path) if os.path.exists(c_path) else {}
            log_meta = jobs_from_log.get(jid, {})
            stat_meta = completed_lpstat.get(jid, {})

            user = c_meta.get('user') or log_meta.get('user') or stat_meta.get('user') or 'direct-client'
            if user == 'webprint':
                continue

            host = c_meta.get('host') or log_meta.get('host') or ''
            title = c_meta.get('title') or log_meta.get('title') or f"Print Job #{jid}"
            pages = c_meta.get('pages') or log_meta.get('max_page') or 1
            copies = c_meta.get('copies') or log_meta.get('copies') or 1
            colormodel = c_meta.get('color') or 'RGB'
            timestamp = c_meta.get('timestamp') or int(time.time())

            user_upper = user.upper()
            host_upper = host.upper()
            if 'DESKTOP-' in user_upper or 'LAPTOP-' in user_upper or '\\' in user or 'WINDOWS' in host_upper:
                source = 'windows_ipp'
            elif 'ANDROID' in host_upper or 'MOPRIA' in host_upper or 'IPHONE' in host_upper or 'IPAD' in host_upper or 'AIRPRINT' in host_upper:
                source = 'mobile_airprint'
            elif host and host not in ('127.0.0.1', 'localhost', '::1'):
                source = 'windows_ipp'
            else:
                source = 'cups_direct'

            is_color = colormodel not in ('KGray', 'Gray', 'monochrome', 'mono', 'K')
            rate = get_rate(is_color)
            cost = rate * pages * copies
            printer_name = log_meta.get('printer') or 'HP_Smart_Tank_520'

            with conn:
                conn.execute("""
                    INSERT INTO print_ledger 
                    (timestamp, job_title, printer, pages, copies, total_pages, colormodel, cost, client_ip, status, source, cups_job_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, f"{title} ({user})", printer_name, pages, copies, pages * copies, colormodel, cost, host or 'LAN', 'COMPLETED', source, jid))
                new_synced += 1
        return new_synced
    except Exception:
        return 0

def generate_cmyk_nozzle_check_pdf(output_path: str, printer_name: str = "HP Smart Tank 520"):
    """Generates standard vector CMYK diagnostic sheet in pure Python (PDF 1.4)."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    ops = [
        f"BT /F1 16 Tf 50 780 Td ({printer_name} - CMYK Nozzle Check & Diagnostics) Tj ET",
        f"BT /F1 10 Tf 50 765 Td (Timestamp: {ts} | Armbian WebPrint Hardware Center) Tj ET",
        "0 1 1 0 k 50 710 495 24 re f",
        "BT /F1 9 Tf 0 0 0 1 k 55 718 Td (CYAN - 100% PRIMARY) Tj ET",
        "1 0 1 0 k 50 670 495 24 re f",
        "BT /F1 9 Tf 0 0 0 1 k 55 678 Td (MAGENTA - 100% PRIMARY) Tj ET",
        "1 1 0 0 k 50 630 495 24 re f",
        "BT /F1 9 Tf 0 0 0 1 k 55 638 Td (YELLOW - 100% PRIMARY) Tj ET",
        "0 0 0 1 k 50 590 495 24 re f",
        "BT /F1 9 Tf 0 0 0 0 k 55 598 Td (BLACK - 100% PIGMENT) Tj ET"
    ]
    colors = [('Cyan', '0 1 1 0 k', 530), ('Magenta', '1 0 1 0 k', 460), ('Yellow', '1 1 0 0 k', 390), ('Black', '0 0 0 1 k', 320)]
    for name, cmyk, y_base in colors:
        ops.append(f"BT /F1 9 Tf 0 0 0 1 k 50 {y_base+40} Td ({name} Nozzle Test Grid) Tj ET")
        ops.append(f"0.5 w {cmyk}")
        for i in range(40):
            x = 50 + i * 12
            y1 = y_base + (i % 6) * 6
            y2 = y1 + 10
            ops.append(f"{x} {y1} m {x+8} {y2} l S")

    ops.append("0 0 0 1 k 0.5 w")
    ops.append("50 150 m 100 150 l S 75 125 m 75 175 l S")
    ops.append("495 150 m 545 150 l S 520 125 m 520 175 l S")
    ops.append("BT /F1 8 Tf 50 80 Td (Verify continuous lines for each nozzle color. Breaks indicate clogged printhead nozzles.) Tj ET")

    content = '\n'.join(ops).encode('utf-8')
    c_len = len(content)

    body = (
        b'%PDF-1.4\n'
        b'1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n'
        b'2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n'
        b'3 0 obj\n<</Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R>>\nendobj\n'
        b'4 0 obj\n<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>\nendobj\n'
        b'5 0 obj\n<</Length ' + str(c_len).encode('ascii') + b'>>\nstream\n' + content + b'\nendstream\nendobj\n'
    )
    o1 = body.find(b'1 0 obj')
    o2 = body.find(b'2 0 obj')
    o3 = body.find(b'3 0 obj')
    o4 = body.find(b'4 0 obj')
    o5 = body.find(b'5 0 obj')
    xref_pos = len(body)
    xref = (
        b'xref\n'
        b'0 6\n'
        b'0000000000 65535 f \n'
        + f'{o1:010d} 00000 n \n'.encode('ascii')
        + f'{o2:010d} 00000 n \n'.encode('ascii')
        + f'{o3:010d} 00000 n \n'.encode('ascii')
        + f'{o4:010d} 00000 n \n'.encode('ascii')
        + f'{o5:010d} 00000 n \n'.encode('ascii')
        + b'trailer\n<</Size 6 /Root 1 0 R>>\nstartxref\n'
        + str(xref_pos).encode('ascii')
        + b'\n%%EOF\n'
    )
    with open(output_path, 'wb') as f:
        f.write(body + xref)

def reset_usb_printer() -> dict:
    """Executes Linux usbreset against HP Smart Tank 520 (03f0:4554)."""
    if os.name == 'nt':
        return {'success': True, 'message': 'Simulated USB reset on Windows host.'}
    cmd = ['sudo', 'usbreset', '03f0:4554'] if (hasattr(os, 'geteuid') and os.geteuid() != 0) else ['usbreset', '03f0:4554']
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return {'success': True, 'message': "HP Smart Tank 520 USB port reset successfully. Device descriptor re-enumerated."}
        return {'success': False, 'error': res.stderr.strip() or 'usbreset returned non-zero code'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def render_counter_sign_html(server_ip: str, rate_bw: str, rate_color: str, currency: str) -> str:
    portal_url = f"http://{server_ip}/"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Print Shop Counter Sign &amp; QR Guide</title>
<style>
  @page {{ size: A4 portrait; margin: 15mm; }}
  * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ background: #f4f6f8; color: #1a1a1a; margin: 0; padding: 20px; display: flex; justify-content: center; }}
  .sheet {{ background: #fff; width: 210mm; min-height: 297mm; padding: 20mm; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); border: 2px solid #202124; position: relative; }}
  .header {{ text-align: center; border-bottom: 3px solid #1a73e8; padding-bottom: 16px; margin-bottom: 24px; }}
  .title {{ font-size: 32px; font-weight: 800; letter-spacing: 1px; color: #1a1a1a; margin: 0; }}
  .subtitle {{ font-size: 16px; font-weight: 600; color: #1a73e8; margin-top: 6px; text-transform: uppercase; }}
  .rates-banner {{ display: flex; justify-content: space-around; background: #e8f0fe; border: 2px dashed #1a73e8; border-radius: 12px; padding: 14px 20px; margin-bottom: 24px; text-align: center; }}
  .rate-box h3 {{ font-size: 13px; text-transform: uppercase; color: #5f6368; margin: 0 0 4px 0; }}
  .rate-box .price {{ font-size: 28px; font-weight: 800; color: #1a73e8; margin: 0; }}
  .qr-section {{ display: flex; justify-content: center; align-items: center; gap: 30px; margin: 24px 0; background: #fafafa; border: 1px solid #dadce0; border-radius: 12px; padding: 20px; }}
  .qr-box {{ text-align: center; }}
  .qr-box canvas, .qr-box img {{ width: 160px; height: 160px; border-radius: 8px; border: 1px solid #ccc; }}
  .qr-label {{ font-size: 13px; font-weight: 700; margin-top: 8px; color: #202124; }}
  .qr-desc {{ font-size: 11px; color: #5f6368; }}
  .guide-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 20px; }}
  .guide-card {{ border: 1px solid #dadce0; border-radius: 8px; padding: 14px; background: #fff; }}
  .guide-card h4 {{ margin: 0 0 8px 0; font-size: 15px; font-weight: 700; color: #202124; display: flex; align-items: center; gap: 6px; }}
  .guide-card ol {{ margin: 0; padding-left: 20px; font-size: 12px; color: #3c4043; line-height: 1.5; }}
  .footer {{ text-align: center; margin-top: 28px; font-size: 12px; color: #70757a; border-top: 1px solid #eee; padding-top: 14px; }}
  .print-btn-bar {{ text-align: center; margin-bottom: 20px; }}
  .print-btn {{ background: #1a73e8; color: #fff; border: none; padding: 10px 24px; font-size: 15px; font-weight: 600; border-radius: 20px; cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.2); }}
  @media print {{
    body {{ background: #fff; padding: 0; }}
    .sheet {{ box-shadow: none; border: none; width: 100%; min-height: auto; padding: 0; }}
    .print-btn-bar {{ display: none; }}
  }}
</style>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
</head>
<body>
<div style="width: 100%; max-width: 210mm;">
  <div class="print-btn-bar">
    <button class="print-btn" onclick="window.print()">🖨️ Print Counter Sign</button>
  </div>
  <div class="sheet">
    <div class="header">
      <h1 class="title">PRINT SHOP &amp; SELF-SERVICE</h1>
      <div class="subtitle">Fast • High Quality • Direct From Any Device</div>
    </div>

    <div class="rates-banner">
      <div class="rate-box">
        <h3>Black &amp; White Printing</h3>
        <p class="price">{currency}{rate_bw} <span style="font-size: 14px; font-weight: normal; color: #5f6368;">/ page</span></p>
      </div>
      <div class="rate-box" style="border-left: 1px solid #c2e7ff; padding-left: 30px;">
        <h3>Full Color Printing</h3>
        <p class="price" style="color: #ea8600;">{currency}{rate_color} <span style="font-size: 14px; font-weight: normal; color: #5f6368;">/ page</span></p>
      </div>
    </div>

    <div class="qr-section">
      <div class="qr-box">
        <canvas id="qrPortal"></canvas>
        <div class="qr-label">1. Connect &amp; Print Online</div>
        <div class="qr-desc">{portal_url}</div>
      </div>
    </div>

    <div class="guide-grid">
      <div class="guide-card">
        <h4>💻 Direct Windows PC</h4>
        <ol>
          <li>Ensure you are connected to the local Wi-Fi.</li>
          <li>Open document and press <strong>Ctrl + P</strong>.</li>
          <li>Select <strong>HP Smart Tank 520</strong>.</li>
          <li>Click Print – pages stack face-up in order!</li>
        </ol>
      </div>
      <div class="guide-card">
        <h4>📱 Android (Mopria Print)</h4>
        <ol>
          <li>Open photo or document on your phone.</li>
          <li>Tap <strong>Share</strong> or <strong>Print</strong>.</li>
          <li>Choose <strong>HP Smart Tank 520</strong> from discovered printers.</li>
          <li>Tap the Printer icon to print immediately.</li>
        </ol>
      </div>
      <div class="guide-card">
        <h4>🍎 iPhone / iPad (AirPrint)</h4>
        <ol>
          <li>Open document, tap Share icon ( <span style="font-size: 14px;">⬆</span> ).</li>
          <li>Select <strong>Print</strong>.</li>
          <li>Printer auto-detects via AirPrint.</li>
          <li>Tap <strong>Print</strong> in top right.</li>
        </ol>
      </div>
      <div class="guide-card">
        <h4>🌐 Web Print Portal</h4>
        <ol>
          <li>Use the QR code above or visit <strong>{portal_url}</strong>.</li>
          <li>Upload your PDF or Word document.</li>
          <li>Preview in print dialog and hit Print!</li>
          <li>Collect your clean prints from the output tray.</li>
        </ol>
      </div>
    </div>

    <div class="footer">
      Powered by Armbian Print Server Appliance • HP Smart Tank 520 Series
    </div>
  </div>
</div>
<script>
  window.addEventListener('DOMContentLoaded', function() {{
    if (typeof QRCode !== 'undefined') {{
      QRCode.toCanvas(document.getElementById('qrPortal'), '{portal_url}', {{ width: 160, margin: 1 }});
    }}
  }});
</script>
</body>
</html>"""

def check_anti_clog_routine():
    """Checks if printer has been idle >= 7 days and prints anti-clog pattern if enabled."""
    if get_setting('auto_anti_clog', 'on').lower() not in ['1', 'true', 'on', 'yes']:
        return
    now = int(time.time())
    last_check = int(get_setting('last_anti_clog_check', '0') or 0)
    if (now - last_check) < 12 * 3600:
        return

    conn = get_db()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_anti_clog_check', ?)", (str(now),))

    cur = conn.cursor()
    cur.execute("SELECT MAX(timestamp) as last_ts FROM print_ledger")
    row = cur.fetchone()
    last_print = row['last_ts'] if row and row['last_ts'] else 0
    if (now - last_print) >= 604800:
        printers_list, def_p = get_printers()
        printer = def_p or (printers_list[0]['name'] if printers_list else 'HP_Smart_Tank_520')
        temp_dir = tempfile.mkdtemp(prefix="keepwet_")
        keepwet_pdf = os.path.join(temp_dir, "keepwet.pdf")
        try:
            generate_cmyk_nozzle_check_pdf(keepwet_pdf, printer)
            cmd = ['lp', '-d', printer, '-t', 'Scheduled Keep-Wet Anti-Clog Maintenance', '-o', 'ColorModel=RGB', keepwet_pdf]
            subprocess.run(cmd, capture_output=True, timeout=30)
            record_ledger("7-Day Auto Keep-Wet Maintenance", printer, 1, 1, 1, "RGB", 0.0, "127.0.0.1", "COMPLETED", source="maintenance")
        except Exception:
            pass
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

def get_cups_overview_data(selected_printer=None) -> dict:
    """Gathers comprehensive status, defaults, active queue, and PPD options for a printer."""
    printers_list, default_printer = get_printers()
    if not selected_printer:
        selected_printer = default_printer or (printers_list[0]['name'] if printers_list else 'HP_Smart_Tank_520')

    p_info = {
        'name': selected_printer,
        'status': 'idle',
        'status_text': 'Ready',
        'accepting': True,
        'shared': True,
        'uri': '',
        'make_and_model': '',
        'ppd_file': f"/etc/cups/ppd/{selected_printer}.ppd"
    }

    for p in printers_list:
        if p['name'] == selected_printer:
            p_info['status'] = p.get('status', 'idle')
            p_info['status_text'] = p.get('status_text', 'Ready')
            break

    try:
        res = run_cups_cmd(['lpstat', '-a'])
        for line in res.stdout.splitlines():
            if selected_printer in line:
                if 'not accepting' in line.lower():
                    p_info['accepting'] = False
                elif 'accepting' in line.lower():
                    p_info['accepting'] = True
    except Exception:
        pass

    opts_kv = {}
    try:
        res = run_cups_cmd(['lpoptions', '-p', selected_printer])
        if res.returncode == 0:
            for item in re.findall(r'(\S+)=(?:\'([^\']*)\'|"([^"]*)"|(\S+))', res.stdout):
                key = item[0]
                val = item[1] or item[2] or item[3]
                opts_kv[key] = val
            p_info['uri'] = opts_kv.get('device-uri', '')
            p_info['make_and_model'] = opts_kv.get('printer-make-and-model', '')
            p_info['shared'] = (opts_kv.get('printer-is-shared', 'true').lower() == 'true')
    except Exception:
        pass

    ppd_options = []
    try:
        res_l = run_cups_cmd(['lpoptions', '-p', selected_printer, '-l'])
        if res_l.returncode == 0:
            ppd_options = parse_lpoptions_l(res_l.stdout)
    except Exception:
        pass

    defaults = {
        'outputorder': get_setting('cups_default_outputorder', opts_kv.get('outputorder', 'reverse')),
        'pagesize': get_setting('cups_default_pagesize', 'A4'),
        'mediatype': get_setting('cups_default_mediatype', 'Plain'),
        'quality': get_setting('cups_default_quality', 'Normal'),
        'colormodel': get_setting('cups_default_colormodel', 'RGB'),
        'scaling': get_setting('cups_default_scaling', opts_kv.get('print-scaling', 'none')),
        'resolution': '600',
        'duplex': 'None'
    }

    if os.path.exists('/etc/cups/lpoptions'):
        try:
            with open('/etc/cups/lpoptions', 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith(f'Default {selected_printer}') or line.startswith(f'Dest {selected_printer}'):
                        m = re.search(r'outputorder=(\w+)', line)
                        if m:
                            defaults['outputorder'] = m.group(1).lower()
                        m_scale = re.search(r'print-scaling=(\w+)', line)
                        if m_scale:
                            defaults['scaling'] = m_scale.group(1).lower()
        except Exception:
            pass

    ppd_path = f"/etc/cups/ppd/{selected_printer}.ppd"
    if os.path.exists(ppd_path):
        try:
            with open(ppd_path, 'r', encoding='utf-8', errors='ignore') as f:
                ppd_text = f.read()
                m_ord = re.search(r'^\*DefaultOutputOrder:\s*(\w+)', ppd_text, re.MULTILINE)
                if m_ord:
                    defaults['outputorder'] = m_ord.group(1).lower()
        except Exception:
            pass

    for opt in ppd_options:
        name = opt['name'].lower()
        if name == 'pagesize' and opt['current']:
            defaults['pagesize'] = opt['current']
        elif name == 'mediatype' and opt['current']:
            defaults['mediatype'] = opt['current']
        elif name == 'outputmode' and opt['current']:
            defaults['quality'] = opt['current']
        elif name == 'colormodel' and opt['current']:
            defaults['colormodel'] = opt['current']
        elif name == 'duplex' and opt['current']:
            defaults['duplex'] = opt['current']
        elif name == 'resolution' and opt['current']:
            defaults['resolution'] = opt['current']

    queue = []
    try:
        res_o = run_cups_cmd(['lpstat', '-o', selected_printer])
        if res_o.returncode == 0:
            for line in res_o.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    job_id = parts[0]
                    user = parts[1]
                    size_str = parts[2]
                    date_str = parts[3]
                    is_held = False
                    status = 'Spooling'
                    try:
                        res_job = run_cups_cmd(['lpstat', '-l', '-o', job_id])
                        out_j = res_job.stdout.lower()
                        if 'held' in out_j or 'holding' in out_j:
                            status = 'Held'
                            is_held = True
                        elif 'printing' in out_j or 'processing' in out_j:
                            status = 'Printing'
                    except Exception:
                        pass
                    queue.append({
                        'job_id': job_id,
                        'user': user,
                        'size': size_str,
                        'date': date_str,
                        'status': status,
                        'is_held': is_held
                    })
    except Exception:
        pass

    completed_jobs = []
    try:
        res_comp = run_cups_cmd(['lpstat', '-W', 'completed', '-o', selected_printer])
        if res_comp.returncode == 0:
            for line in res_comp.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    completed_jobs.append({
                        'job_id': parts[0],
                        'user': parts[1],
                        'size': parts[2],
                        'date': parts[3],
                        'status': 'Completed'
                    })
    except Exception:
        pass

    return {
        'success': True,
        'printers': printers_list,
        'selected_printer': selected_printer,
        'is_default': (selected_printer == default_printer),
        'info': p_info,
        'defaults': defaults,
        'ppd_options': ppd_options,
        'queue': queue,
        'completed_jobs': completed_jobs,
        'auto_anti_clog': get_setting('auto_anti_clog', 'on')
    }

@app.route('/api/cups/overview', methods=['GET'])
def api_cups_overview():
    printer = request.args.get('printer')
    try:
        data = get_cups_overview_data(printer)
        return jsonify(data)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/cups/settings', methods=['POST'])
def api_cups_settings():
    token = request.headers.get('X-Admin-Token') or request.form.get('token')
    if not verify_admin_token(token):
        return jsonify({'success': False, 'error': 'Unauthorized: admin login required to modify CUPS hardware settings'}), 401

    data = request.get_json(silent=True) or request.form.to_dict()
    printer = (data.get('printer') or '').strip()
    if not printer:
        printers_list, def_p = get_printers()
        printer = def_p or (printers_list[0]['name'] if printers_list else 'HP_Smart_Tank_520')

    outputorder = (data.get('outputorder') or '').strip().lower()
    pagesize = (data.get('pagesize') or '').strip()
    mediatype = (data.get('mediatype') or '').strip()
    quality = (data.get('quality') or '').strip()
    colormodel = (data.get('colormodel') or '').strip()
    scaling = (data.get('scaling') or '').strip().lower()
    duplex = (data.get('duplex') or '').strip()
    resolution = (data.get('resolution') or '').strip()
    raw_options = data.get('raw_options', {})
    if isinstance(raw_options, str):
        try:
            raw_options = json.loads(raw_options)
        except Exception:
            raw_options = {}

    cmd = ['lpadmin', '-p', printer]
    lpoptions_dict = {}

    if pagesize:
        cmd.extend(['-o', f'PageSize={pagesize}'])
        lpoptions_dict['PageSize'] = pagesize
    if mediatype:
        cmd.extend(['-o', f'MediaType={mediatype}'])
        lpoptions_dict['MediaType'] = mediatype
    if quality:
        cmd.extend(['-o', f'OutputMode={quality}'])
        lpoptions_dict['OutputMode'] = quality
    if colormodel:
        if colormodel in ('KGray', 'Gray', 'monochrome', 'mono'):
            cmd.extend(['-o', 'ColorModel=KGray', '-o', 'print-color-mode-default=monochrome'])
            lpoptions_dict['ColorModel'] = 'KGray'
            lpoptions_dict['print-color-mode'] = 'monochrome'
        else:
            cmd.extend(['-o', f'ColorModel={colormodel}', '-o', 'print-color-mode-default=color'])
            lpoptions_dict['ColorModel'] = colormodel
            lpoptions_dict['print-color-mode'] = 'color'
    if outputorder in ('reverse', 'normal'):
        cmd.extend(['-o', f'outputorder-default={outputorder}'])
        lpoptions_dict['outputorder'] = outputorder
    if scaling:
        cmd.extend(['-o', f'print-scaling-default={scaling}'])
        lpoptions_dict['print-scaling'] = scaling
    if duplex and duplex != 'None':
        cmd.extend(['-o', f'Duplex={duplex}'])
        lpoptions_dict['Duplex'] = duplex
    if resolution:
        res_val = resolution if resolution.endswith('dpi') else f"{resolution}dpi"
        cmd.extend(['-o', f'resolution-default={res_val}'])
        lpoptions_dict['resolution'] = res_val

    for k, v in raw_options.items():
        if k and v:
            cmd.extend(['-o', f'{k}={v}'])
            lpoptions_dict[k] = v

    res = run_cups_cmd(cmd)
    if res.returncode != 0 and 'not found' not in res.stderr.lower():
        return jsonify({'success': False, 'error': f"lpadmin failed: {res.stderr.strip() or res.stdout.strip()}"}), 500

    if outputorder in ('reverse', 'normal'):
        update_ppd_outputorder(printer, outputorder)

    if lpoptions_dict:
        update_system_lpoptions(printer, lpoptions_dict)

    conn = get_db()
    with conn:
        if outputorder:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cups_default_outputorder', ?)", (outputorder,))
        if pagesize:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cups_default_pagesize', ?)", (pagesize,))
        if mediatype:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cups_default_mediatype', ?)", (mediatype,))
        if quality:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cups_default_quality', ?)", (quality,))
        if colormodel:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cups_default_colormodel', ?)", (colormodel,))
        if scaling:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cups_default_scaling', ?)", (scaling,))

    return jsonify({
        'success': True,
        'message': f"CUPS defaults for '{printer}' successfully applied across all clients (Windows, Android, iOS & Web)."
    })

@app.route('/api/cups/action', methods=['POST'])
def api_cups_action():
    token = request.headers.get('X-Admin-Token') or request.form.get('token')
    if not verify_admin_token(token):
        return jsonify({'success': False, 'error': 'Unauthorized: admin login required'}), 401

    action = request.form.get('action')
    printer = (request.form.get('printer') or '').strip()
    if not printer:
        printers_list, def_p = get_printers()
        printer = def_p or (printers_list[0]['name'] if printers_list else 'HP_Smart_Tank_520')

    if action == 'pause':
        res = run_cups_cmd(['cupsdisable', printer])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to pause printer'}), 500
        return jsonify({'success': True, 'message': f"Printer '{printer}' paused (queue held)."})

    elif action == 'resume':
        res = run_cups_cmd(['cupsenable', printer])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to resume printer'}), 500
        return jsonify({'success': True, 'message': f"Printer '{printer}' resumed (ready to print)."})

    elif action == 'accept':
        res = run_cups_cmd(['cupsaccept', printer])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to accept jobs'}), 500
        return jsonify({'success': True, 'message': f"Printer '{printer}' is now accepting print jobs."})

    elif action == 'reject':
        res = run_cups_cmd(['cupsreject', printer])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to reject jobs'}), 500
        return jsonify({'success': True, 'message': f"Printer '{printer}' is rejecting new print jobs."})

    elif action == 'cancel_job':
        job_id = (request.form.get('job_id') or '').strip()
        if not job_id:
            return jsonify({'success': False, 'error': 'Job ID required'}), 400
        res = run_cups_cmd(['cancel', job_id])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to cancel job'}), 500
        return jsonify({'success': True, 'message': f"Print job '{job_id}' cancelled."})

    elif action == 'purge_queue':
        res = run_cups_cmd(['cancel', '-a', '-x', printer])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to purge queue'}), 500
        return jsonify({'success': True, 'message': f"All print jobs for '{printer}' purged from spooler."})

    elif action == 'test_page':
        test_file = '/usr/share/cups/data/testprint'
        if not os.path.exists(test_file):
            temp_test = os.path.join(tempfile.gettempdir(), 'cups_test_page.txt')
            with open(temp_test, 'w', encoding='utf-8') as f:
                f.write(f"CUPS Command Center Test Print\nPrinter: {printer}\nTimestamp: {time.ctime()}\nServer: Armbian B860H Print Server\n")
            test_file = temp_test
        res = run_cups_cmd(['lp', '-d', printer, '-t', 'CUPS Command Center Test Page', test_file])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to send test page'}), 500
        return jsonify({'success': True, 'message': f"Official test page submitted to '{printer}' ({res.stdout.strip()})."})

    elif action == 'restart_cups':
        cmd = ['sudo', 'systemctl', 'restart', 'cups'] if (os.name != 'nt' and hasattr(os, 'geteuid') and os.geteuid() != 0) else ['systemctl', 'restart', 'cups']
        try:
            subprocess.run(cmd, check=True, timeout=15)
            return jsonify({'success': True, 'message': 'CUPS print daemon restarted successfully.'})
        except Exception as e:
            return jsonify({'success': False, 'error': f"Failed to restart CUPS: {e}"}), 500

    elif action == 'hold_job':
        job_id = (request.form.get('job_id') or '').strip()
        if not job_id:
            return jsonify({'success': False, 'error': 'Job ID required'}), 400
        res = run_cups_cmd(['lp', '-i', job_id, '-H', 'hold'])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to hold job'}), 500
        return jsonify({'success': True, 'message': f"Print job '{job_id}' held in queue."})

    elif action == 'release_job':
        job_id = (request.form.get('job_id') or '').strip()
        if not job_id:
            return jsonify({'success': False, 'error': 'Job ID required'}), 400
        res = run_cups_cmd(['lp', '-i', job_id, '-H', 'release'])
        if res.returncode != 0:
            res = run_cups_cmd(['lp', '-i', job_id, '-H', 'resume'])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to release job'}), 500
        return jsonify({'success': True, 'message': f"Print job '{job_id}' released."})

    elif action == 'reprint_job':
        job_id = (request.form.get('job_id') or '').strip()
        if not job_id:
            return jsonify({'success': False, 'error': 'Job ID required'}), 400
        res = run_cups_cmd(['lp', '-i', job_id, '-H', 'restart'])
        if res.returncode != 0:
            return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to restart job. Spool retention may have expired.'}), 500
        return jsonify({'success': True, 'message': f"Print job '{job_id}' restarted for reprinting."})

    elif action == 'nozzle_check':
        temp_dir = tempfile.mkdtemp(prefix="nozzle_")
        nozzle_pdf = os.path.join(temp_dir, "nozzle_check.pdf")
        try:
            generate_cmyk_nozzle_check_pdf(nozzle_pdf, printer)
            res = run_cups_cmd(['lp', '-d', printer, '-t', 'CMYK Nozzle Check Pattern', '-o', 'ColorModel=RGB', nozzle_pdf])
            if res.returncode != 0:
                return jsonify({'success': False, 'error': res.stderr.strip() or 'Failed to submit nozzle check'}), 500
            record_ledger("CMYK Nozzle Check", printer, 1, 1, 1, "RGB", 0.0, "127.0.0.1", "COMPLETED", source="maintenance")
            return jsonify({'success': True, 'message': f"CMYK Nozzle Check pattern sent to '{printer}'."})
        except Exception as e:
            return jsonify({'success': False, 'error': f"Failed to generate nozzle pattern: {e}"}), 500
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    elif action == 'usb_reset':
        res = reset_usb_printer()
        if not res.get('success'):
            return jsonify({'success': False, 'error': res.get('error', 'USB reset failed')}), 500
        return jsonify({'success': True, 'message': res.get('message', 'Printer USB port reset successfully.')})

    elif action == 'toggle_anti_clog':
        current = get_setting('auto_anti_clog', 'on')
        new_val = 'off' if current == 'on' else 'on'
        conn = get_db()
        with conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_anti_clog', ?)", (new_val,))
        return jsonify({'success': True, 'auto_anti_clog': new_val, 'message': f"Automated 7-day anti-clog maintenance is now {new_val.upper()}."})

    return jsonify({'success': False, 'error': 'Unknown action'}), 400

def get_cups_job_status(job_id: str) -> dict:
    if not job_id:
        return {'status': 'unknown', 'message': 'No job ID provided'}
    
    # Check if this is a PIN-held job
    if job_id.startswith('pin_'):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT job_id, failed_attempts FROM pinned_jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        if row:
            return {'status': 'held', 'message': 'Document held in secure PIN queue awaiting release.'}
        return {'status': 'not_found', 'message': 'PIN job not found or expired.'}

    # Extract CUPS request id if format is 'request id is printer-123 (...)'
    m = re.search(r'([A-Za-z0-9_-]+-\d+)', job_id)
    cups_id = m.group(1) if m else job_id

    # Query CUPS lpstat
    try:
        res = subprocess.run(['lpstat', '-l', '-o', cups_id], capture_output=True, text=True, timeout=5)
        out = (res.stdout or '').lower()
        if res.returncode == 0 and out.strip():
            if 'processing' in out or 'printing' in out or 'sending data' in out:
                return {'status': 'printing', 'message': 'Printing in progress on device...', 'job_id': cups_id}
            return {'status': 'spooling', 'message': 'Job spooled in queue, awaiting device...', 'job_id': cups_id}
        
        # Check completed jobs via lpstat -W completed -o cups_id
        res_comp = subprocess.run(['lpstat', '-W', 'completed', '-o', cups_id], capture_output=True, text=True, timeout=5)
        out_comp = (res_comp.stdout or '').lower()
        if res_comp.returncode == 0 and out_comp.strip():
            return {'status': 'completed', 'message': 'Print job completed.', 'job_id': cups_id}
        
        return {'status': 'completed', 'message': 'Print job dispatched to printer.', 'job_id': cups_id}
    except Exception:
        # Fallback for Windows development / environments without CUPS
        return {'status': 'completed', 'message': 'Job submitted.', 'job_id': cups_id}

@app.route('/api/preview/<preview_id>.pdf', methods=['GET'])
def api_preview_pdf(preview_id):
    if not re.match(r'^prev_[0-9]+_[a-f0-9]+$', preview_id):
        return jsonify({'success': False, 'error': 'Invalid preview token'}), 400
    pdf_path = os.path.join(SPOOL_PREVIEWS_DIR, f"{preview_id}.pdf")
    if not os.path.exists(pdf_path):
        return jsonify({'success': False, 'error': 'Preview not found or expired'}), 404
    return send_file(pdf_path, mimetype='application/pdf')

@app.route('/api/jobs/<job_id>/status', methods=['GET'])
def api_job_status(job_id):
    clean_id = re.sub(r'[^A-Za-z0-9_\-\.]', '', job_id)
    if not clean_id:
        return jsonify({'success': False, 'error': 'Invalid Job ID'}), 400
    st = get_cups_job_status(clean_id)
    return jsonify({'success': True, 'job_id': clean_id, **st})

@app.route('/api/inspect', methods=['POST'])
def api_inspect():
    client_ip = request.remote_addr or ''
    if client_ip.startswith('::ffff:'):
        client_ip = client_ip[7:]
    if not is_public_print_enabled():
        return jsonify({'success': False, 'error': 'Public printing is currently disabled by administrator'}), 403
    if not public_print_limiter.is_allowed(client_ip):
        return jsonify({'success': False, 'error': 'Rate limit exceeded: please wait a minute'}), 429
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': 'Empty filename'}), 400

    try:
        validate_extension(file.filename)
        copies = validate_copies(request.form.get('copies', '1'))
        pagerange = validate_page_range(request.form.get('pagerange', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    colormodel = request.form.get('colormodel', 'RGB')
    if colormodel not in ALLOWED_COLORMODELS:
        colormodel = 'RGB'

    duplex = request.form.get('duplex', 'None')
    if duplex not in ALLOWED_DUPLEX:
        duplex = 'None'

    suffix = os.path.splitext(file.filename)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        raw_path = tmp.name

    temp_dir = None
    try:
        conv_pdf, temp_dir = convert_to_pdf(raw_path, file.filename)
        pages = get_pdf_page_count(conv_pdf)
        billable_pages = parse_page_ranges(pagerange, pages)
        is_duplex = duplex in ('DuplexNoTumble', 'DuplexTumble')
        sheets = (billable_pages + 1) // 2 if is_duplex else billable_pages
        total_sheets = sheets * copies

        rate = get_rate(colormodel not in ('KGray', 'Gray', 'K'))
        cost = rate * billable_pages * copies
        symbol = get_setting('currency_symbol', '₱')

        # Cache converted PDF for instant client preview and 0ms print execution
        preview_id = f"prev_{int(time.time())}_{secrets.token_hex(6)}"
        cached_pdf_path = os.path.join(SPOOL_PREVIEWS_DIR, f"{preview_id}.pdf")
        shutil.copy2(conv_pdf, cached_pdf_path)

        return jsonify({
            'success': True,
            'preview_id': preview_id,
            'pages': pages,
            'billable_pages': billable_pages,
            'sheets': sheets,
            'total_sheets': total_sheets,
            'copies': copies,
            'cost': cost,
            'cost_formatted': f"{symbol}{cost:.2f}",
            'filename': file.filename,
            'is_pdf': (suffix == '.pdf')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except Exception:
                pass

@app.route('/api/print', methods=['POST'])
def api_print():
    client_ip = request.remote_addr or ''
    if client_ip.startswith('::ffff:'):
        client_ip = client_ip[7:]

    if not is_public_print_enabled():
        return jsonify({'success': False, 'error': 'Public printing is currently disabled by administrator'}), 403

    if not public_print_limiter.is_allowed(client_ip):
        return jsonify({'success': False, 'error': 'Rate limit exceeded: maximum 10 print submissions per minute'}), 429

    preview_id = request.form.get('preview_id', '').strip()
    cached_pdf = None
    if preview_id and re.match(r'^prev_[0-9]+_[a-f0-9]+$', preview_id):
        cand = os.path.join(SPOOL_PREVIEWS_DIR, f"{preview_id}.pdf")
        if os.path.exists(cand):
            cached_pdf = cand

    file = request.files.get('file')
    if not cached_pdf and (not file or not file.filename):
        return jsonify({'success': False, 'error': 'No file uploaded or preview expired'}), 400

    filename = file.filename if file and file.filename else f"{preview_id}.pdf"

    try:
        if file and file.filename:
            ext = validate_extension(file.filename)
        else:
            ext = '.pdf'
        copies = validate_copies(request.form.get('copies', '1'))
        pagerange = validate_page_range(request.form.get('pagerange', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    printers_list, def_p = get_printers()
    valid_names = [p['name'] for p in printers_list]
    printer = request.form.get('printer', '').strip()
    if not printer:
        printer = def_p or (valid_names[0] if valid_names else 'HP_Smart_Tank_520')
    elif valid_names and printer not in valid_names:
        return jsonify({'success': False, 'error': 'Selected printer is not available'}), 400

    pagesize = request.form.get('pagesize', 'A4')
    if pagesize not in ALLOWED_PAGESIZES: pagesize = 'A4'
    orientation = request.form.get('orientation', 'auto')
    if orientation not in ALLOWED_ORIENTATIONS: orientation = 'auto'
    margin_mode = request.form.get('margin_mode', 'printable')
    if margin_mode not in ALLOWED_MARGIN_MODES: margin_mode = 'printable'
    mediatype = request.form.get('mediatype', 'Plain')
    if mediatype not in ALLOWED_MEDIATYPES: mediatype = 'Plain'
    quality = request.form.get('quality', 'Normal')
    if quality not in ALLOWED_QUALITIES: quality = 'Normal'
    colormodel = request.form.get('colormodel', 'RGB')
    if colormodel not in ALLOWED_COLORMODELS: colormodel = 'RGB'
    duplex = request.form.get('duplex', 'None')
    if duplex not in ALLOWED_DUPLEX: duplex = 'None'
    resolution = request.form.get('resolution', '300')
    if resolution not in ALLOWED_RESOLUTIONS: resolution = '300'
    fitpage = (request.form.get('fitpage', 'true').lower() == 'true')

    raw_path = None
    temp_dir = None
    try:
        if cached_pdf:
            conv_pdf = cached_pdf
        else:
            suffix = ext
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                file.save(tmp.name)
                raw_path = tmp.name
            conv_pdf, temp_dir = convert_to_pdf(raw_path, file.filename)

        total_pages = get_pdf_page_count(conv_pdf)
        if total_pages > MAX_PAGES_PER_JOB:
            return jsonify({'success': False, 'error': f'Document exceeds maximum allowed pages ({MAX_PAGES_PER_JOB})'}), 400

        billable_pages = parse_page_ranges(pagerange, total_pages)
        rate = get_rate(colormodel not in ('KGray', 'Gray', 'K'))
        cost = rate * billable_pages * copies

        options = {
            'copies': copies,
            'pagesize': pagesize,
            'mediatype': mediatype,
            'quality': quality,
            'colormodel': colormodel,
            'duplex': duplex,
            'orientation': orientation,
            'fitpage': fitpage,
            'pagerange': pagerange,
            'resolution': resolution,
            'margin_mode': margin_mode,
            'm_top': request.form.get('m_top', '10'),
            'm_bottom': request.form.get('m_bottom', '10'),
            'm_left': request.form.get('m_left', '10'),
            'm_right': request.form.get('m_right', '10'),
            'number_up': request.form.get('number_up', '1'),
            'page_border': request.form.get('page_border', 'none')
        }

        cmd = build_lp_command(printer, conv_pdf, options)
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        m = re.search(r'([A-Za-z0-9_-]+-\d+)', res.stdout)
        job_id = m.group(1) if m else res.stdout.strip()
        cups_jid = None
        if job_id and '-' in job_id:
            try:
                cups_jid = int(job_id.split('-')[-1])
            except Exception:
                pass

        record_ledger(filename, printer, billable_pages, copies, billable_pages * copies, colormodel, cost, client_ip, "COMPLETED", source="portal", cups_job_id=cups_jid)
        return jsonify({'success': True, 'job_id': job_id, 'pages': billable_pages, 'cost': cost})
    except subprocess.CalledProcessError as e:
        return jsonify({'success': False, 'error': e.stderr.strip() or str(e)}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        if raw_path and os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except Exception:
                pass

@app.route('/api/print/pin-hold', methods=['POST'])
def api_pin_hold():
    client_ip = request.remote_addr or ''
    if client_ip.startswith('::ffff:'):
        client_ip = client_ip[7:]

    if not is_public_print_enabled():
        return jsonify({'success': False, 'error': 'Public printing is currently disabled by administrator'}), 403

    if not public_print_limiter.is_allowed(client_ip):
        return jsonify({'success': False, 'error': 'Rate limit exceeded: maximum 10 submissions per minute'}), 429

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': 'Empty filename'}), 400

    try:
        ext = validate_extension(file.filename)
        pin = validate_pin(request.form.get('pin', ''))
        copies = validate_copies(request.form.get('copies', '1'))
        pagerange = validate_page_range(request.form.get('pagerange', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    printers_list, def_p = get_printers()
    valid_names = [p['name'] for p in printers_list]
    printer = request.form.get('printer', '').strip()
    if not printer:
        printer = def_p or (valid_names[0] if valid_names else 'HP_Smart_Tank_520')
    elif valid_names and printer not in valid_names:
        return jsonify({'success': False, 'error': 'Selected printer is not available'}), 400

    pagesize = request.form.get('pagesize', 'A4')
    if pagesize not in ALLOWED_PAGESIZES: pagesize = 'A4'
    orientation = request.form.get('orientation', 'auto')
    if orientation not in ALLOWED_ORIENTATIONS: orientation = 'auto'
    margin_mode = request.form.get('margin_mode', 'printable')
    if margin_mode not in ALLOWED_MARGIN_MODES: margin_mode = 'printable'
    mediatype = request.form.get('mediatype', 'Plain')
    if mediatype not in ALLOWED_MEDIATYPES: mediatype = 'Plain'
    quality = request.form.get('quality', 'Normal')
    if quality not in ALLOWED_QUALITIES: quality = 'Normal'
    colormodel = request.form.get('colormodel', 'RGB')
    if colormodel not in ALLOWED_COLORMODELS: colormodel = 'RGB'
    duplex = request.form.get('duplex', 'None')
    if duplex not in ALLOWED_DUPLEX: duplex = 'None'
    resolution = request.form.get('resolution', '300')
    if resolution not in ALLOWED_RESOLUTIONS: resolution = '300'
    fitpage = (request.form.get('fitpage', 'true').lower() == 'true')

    job_id = f"pin_{int(time.time())}_{secrets.token_hex(4)}"
    pin_hash = hash_pin(pin)

    suffix = ext
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        raw_path = tmp.name

    temp_dir = None
    try:
        conv_pdf, temp_dir = convert_to_pdf(raw_path, file.filename)
        pages = get_pdf_page_count(conv_pdf)
        if pages > MAX_PAGES_PER_JOB:
            return jsonify({'success': False, 'error': f'Document exceeds maximum allowed pages ({MAX_PAGES_PER_JOB})'}), 400

        dest_enc_path = os.path.join(SPOOL_PINNED_DIR, f"{job_id}.enc")
        encrypt_held_file(conv_pdf, dest_enc_path)

        billable_pages = parse_page_ranges(pagerange, pages)
        rate = get_rate(colormodel not in ('KGray', 'Gray', 'K'))
        cost = rate * billable_pages * copies

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
            'pagerange': pagerange,
            'number_up': request.form.get('number_up', '1'),
            'page_border': request.form.get('page_border', 'none')
        }

        conn = get_db()
        with conn:
            conn.execute("""
                INSERT INTO pinned_jobs 
                (job_id, file_path, filename, pin_hash, pages, copies, colormodel, printer, options_json, cost, created_at, expires_at, failed_attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                job_id, dest_enc_path, file.filename, pin_hash, pages, copies, colormodel, printer,
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
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except Exception:
                pass

@app.route('/api/pin/list', methods=['GET'])
def api_pin_list():
    token = request.headers.get('X-Admin-Token')
    if not verify_admin_token(token):
        return jsonify({'success': False, 'error': 'Unauthorized: administrator login required'}), 401

    cleanup_expired_jobs()
    conn = get_db()
    cur = conn.cursor()
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
    client_ip = request.remote_addr or ''
    if client_ip.startswith('::ffff:'):
        client_ip = client_ip[7:]

    if not pin_release_limiter.is_allowed(client_ip):
        return jsonify({'success': False, 'error': 'Rate limit exceeded: maximum 10 PIN release attempts per minute'}), 429

    job_id = request.form.get('job_id', '').strip()
    pin = request.form.get('pin', '').strip()
    if not job_id or not pin:
        return jsonify({'success': False, 'error': 'Missing Job ID or PIN'}), 400

    if not re.match(r'^[0-9]{6}$', pin):
        return jsonify({'success': False, 'error': 'PIN must be exactly 6 digits'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM pinned_jobs WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({'success': False, 'error': 'Job not found or expired'}), 404

    target_hash = hash_pin(pin)
    if not hmac.compare_digest(row['pin_hash'], target_hash):
        failed = (row['failed_attempts'] or 0) + 1
        if failed >= 5:
            try:
                if os.path.exists(row['file_path']):
                    os.remove(row['file_path'])
            except Exception:
                pass
            with conn:
                conn.execute("DELETE FROM pinned_jobs WHERE job_id = ?", (job_id,))
            return jsonify({'success': False, 'error': 'Incorrect PIN. Maximum 5 attempts exceeded: job deleted.'}), 403
        else:
            with conn:
                conn.execute("UPDATE pinned_jobs SET failed_attempts = ? WHERE job_id = ?", (failed, job_id))
            remaining = 5 - failed
            return jsonify({'success': False, 'error': f'Incorrect PIN. {remaining} attempt(s) remaining.'}), 401

    enc_path = row['file_path']
    if not os.path.exists(enc_path):
        return jsonify({'success': False, 'error': 'Spool file missing'}), 500

    temp_dir = tempfile.mkdtemp(prefix="pin_rel_")
    decrypted_pdf = os.path.join(temp_dir, "release.pdf")
    try:
        decrypt_held_file(enc_path, decrypted_pdf)
        options = json.loads(row['options_json'])
        printer = options.get('printer') or 'HP_Smart_Tank_520'
        copies = options.get('copies', 1)

        cmd = build_lp_command(printer, decrypted_pdf, options)
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        m = re.search(r'([A-Za-z0-9_-]+-\d+)', res.stdout)
        cups_job = m.group(1) if m else res.stdout.strip()
        cups_jid = None
        if cups_job and '-' in cups_job:
            try:
                cups_jid = int(cups_job.split('-')[-1])
            except Exception:
                pass

        try:
            os.remove(enc_path)
        except Exception:
            pass

        with conn:
            conn.execute("DELETE FROM pinned_jobs WHERE job_id = ?", (job_id,))

        record_ledger(row['filename'], printer, row['pages'], row['copies'], row['pages'] * row['copies'],
                      row['colormodel'], row['cost'], client_ip, "RELEASED", source="pin_release", cups_job_id=cups_jid)

        return jsonify({'success': True, 'cups_job': cups_job, 'message': 'Job released to printer.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.route('/api/reports/ledger', methods=['GET'])
def api_reports_ledger():
    token = request.headers.get('X-Admin-Token')
    if not verify_admin_token(token):
        return jsonify({'success': False, 'error': 'Unauthorized: administrator authentication required'}), 401

    sync_cups_ledger()
    check_anti_clog_routine()

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
            'status': r['status'],
            'source': r['source'] if 'source' in r.keys() else 'portal'
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
    token = request.headers.get('X-Admin-Token') or request.args.get('token')
    if not verify_admin_token(token):
        return Response("Unauthorized: administrator authentication required\n", status=401, mimetype='text/plain')

    sync_cups_ledger()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM print_ledger ORDER BY timestamp DESC")
    rows = cur.fetchall()

    def generate_csv():
        yield "ID,Timestamp,Job Title,Printer,Pages,Copies,Total Pages,Color Mode,Cost,Client IP,Status,Source\n"
        for r in rows:
            ts = datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            title = r['job_title'].replace('"', '""')
            source = r['source'] if 'source' in r.keys() else 'portal'
            yield f'"{r["id"]}","{ts}","{title}","{r["printer"]}","{r["pages"]}","{r["copies"]}","{r["total_pages"]}","{r["colormodel"]}","{r["cost"]:.2f}","{r["client_ip"]}","{r["status"]}","{source}"\n'

    return Response(generate_csv(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=print_ledger.csv'})

@app.route('/api/network/services', methods=['GET'])
def api_network_services():
    services_to_check = [
        {'id': 'cups', 'name': 'CUPS Print Spooler', 'unit': 'cups.service', 'port': 631},
        {'id': 'avahi-daemon', 'name': 'Avahi (AirPrint / Mopria mDNS)', 'unit': 'avahi-daemon.service', 'port': 5353},
        {'id': 'smbd', 'name': 'Samba (Windows SMB/WSD)', 'unit': 'smbd.service', 'port': 445},
        {'id': 'tailscaled', 'name': 'Tailscale (Remote VPN Gateway)', 'unit': 'tailscaled.service', 'port': None}
    ]
    results = []
    for s in services_to_check:
        active = False
        status_text = 'Unknown'
        uptime_str = ''
        if os.name != 'nt':
            try:
                res = subprocess.run(['systemctl', 'is-active', s['unit']], capture_output=True, text=True, timeout=3)
                active = (res.stdout.strip() == 'active')
                status_text = 'Active (running)' if active else res.stdout.strip()
                res_time = subprocess.run(['systemctl', 'show', s['unit'], '--property=ActiveEnterTimestamp'], capture_output=True, text=True, timeout=3)
                for line in res_time.stdout.splitlines():
                    if line.startswith('ActiveEnterTimestamp='):
                        uptime_str = line.split('=', 1)[1].strip()
            except Exception:
                status_text = 'Check failed'
        else:
            active = True
            status_text = 'Active (Dev Mock)'
            uptime_str = 'Running'

        results.append({
            'id': s['id'],
            'name': s['name'],
            'unit': s['unit'],
            'port': s['port'],
            'active': active,
            'status': status_text,
            'uptime': uptime_str
        })
    return jsonify({'success': True, 'services': results})

@app.route('/api/network/services/restart', methods=['POST'])
def api_network_service_restart():
    token = request.headers.get('X-Admin-Token') or request.form.get('token')
    if not verify_admin_token(token):
        return jsonify({'success': False, 'error': 'Unauthorized: administrator authentication required'}), 401
    
    service_id = (request.form.get('service') or '').strip()
    valid_units = {
        'cups': 'cups',
        'avahi-daemon': 'avahi-daemon',
        'smbd': 'smbd',
        'tailscaled': 'tailscaled'
    }
    if service_id not in valid_units:
        return jsonify({'success': False, 'error': f"Invalid service '{service_id}'"}), 400

    unit_name = valid_units[service_id]
    if os.name != 'nt':
        cmd = ['sudo', 'systemctl', 'restart', unit_name] if (hasattr(os, 'geteuid') and os.geteuid() != 0) else ['systemctl', 'restart', unit_name]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode != 0:
                return jsonify({'success': False, 'error': res.stderr.strip() or f'Failed to restart {unit_name}'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    return jsonify({'success': True, 'message': f"Service '{unit_name}' restarted successfully."})

@app.route('/api/reports/counter-sign', methods=['GET'])
def api_reports_counter_sign():
    server_ip = request.host.split(':')[0]
    rate_bw = get_setting('rate_bw', '2.00')
    rate_color = get_setting('rate_color', '5.00')
    currency = get_setting('currency_symbol', '₱')
    return render_counter_sign_html(server_ip, rate_bw, rate_color, currency)

@app.route('/api/admin/auth', methods=['POST'])
def api_admin_auth():
    client_ip = request.remote_addr or ''
    if client_ip.startswith('::ffff:'):
        client_ip = client_ip[7:]

    if not admin_login_limiter.is_allowed(client_ip):
        return jsonify({'success': False, 'error': 'Too many failed login attempts. Please wait 1 minute.'}), 429

    password = request.form.get('password', '')
    admin_hash = get_setting('admin_hash', '')
    if not admin_hash:
        return jsonify({'success': False, 'error': 'Administrator account not configured'}), 500

    valid = False
    try:
        valid = check_password_hash(admin_hash, password)
    except Exception:
        pass

    if not valid and len(admin_hash) == 64:
        if hmac.compare_digest(hashlib.sha256(password.encode('utf-8')).hexdigest(), admin_hash):
            valid = True
            new_hash = generate_password_hash(password, method='scrypt')
            conn = get_db()
            with conn:
                conn.execute("UPDATE settings SET value = ? WHERE key = 'admin_hash'", (new_hash,))

    if valid:
        admin_login_limiter.reset(client_ip)
        token = secrets.token_hex(24)
        ACTIVE_TOKENS[token] = time.time() + 7200
        return jsonify({'success': True, 'token': token})

    return jsonify({'success': False, 'error': 'Invalid administrator password'}), 401

@app.route('/api/admin/action', methods=['POST'])
def api_admin_action():
    token = request.headers.get('X-Admin-Token') or request.form.get('token')
    if not verify_admin_token(token):
        return jsonify({'success': False, 'error': 'Unauthorized: admin login required'}), 401

    action = request.form.get('action')
    if action == 'reboot':
        cmd = ['sudo', 'reboot'] if (os.name != 'nt' and hasattr(os, 'geteuid') and os.geteuid() != 0) else ['reboot']
        subprocess.Popen(cmd)
        return jsonify({'success': True, 'message': 'System rebooting in 3 seconds...'})
    elif action == 'shutdown':
        cmd = ['sudo', 'poweroff'] if (os.name != 'nt' and hasattr(os, 'geteuid') and os.geteuid() != 0) else ['poweroff']
        subprocess.Popen(cmd)
        return jsonify({'success': True, 'message': 'System shutting down...'})
    elif action == 'restart_cups':
        cmd = ['sudo', 'systemctl', 'restart', 'cups'] if (os.name != 'nt' and hasattr(os, 'geteuid') and os.geteuid() != 0) else ['systemctl', 'restart', 'cups']
        subprocess.run(cmd, check=True, timeout=15)
        return jsonify({'success': True, 'message': 'CUPS print spooler restarted successfully.'})
    elif action == 'purge_cache':
        purged = 0
        temp_dir = tempfile.gettempdir()
        for pattern in ['webprint*', 'conv_*', 'lo_*', 'pin_rel_*']:
            for item in glob.glob(os.path.join(temp_dir, pattern)):
                try:
                    if os.path.isdir(item):
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        os.remove(item)
                    purged += 1
                except Exception:
                    pass
        return jsonify({'success': True, 'message': f'Temporary spool cache purged ({purged} items cleaned).'})
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
        if len(new_pass) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters'}), 400
        new_hash = generate_password_hash(new_pass, method='scrypt')
        conn = get_db()
        with conn:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'admin_hash'", (new_hash,))
        return jsonify({'success': True, 'message': 'Admin password changed.'})
    elif action == 'toggle_public_print':
        current = is_public_print_enabled()
        new_val = 'off' if current else 'on'
        conn = get_db()
        with conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('public_print', ?)", (new_val,))
        return jsonify({'success': True, 'public_print': (new_val == 'on'), 'message': f'Public printing turned {new_val}.'})
    return jsonify({'success': False, 'error': 'Unknown action'}), 400

# ============================================================
# INBOX: FILES SENT BY WI-FI GUESTS (relayed by the OpenWrt router)
# ============================================================
def get_send_key() -> str:
    try:
        with open(SEND_KEY_PATH, 'r') as f:
            return f.read().strip()
    except Exception:
        return os.environ.get('SEND_KEY', '').strip()

def clean_filename(name: str) -> str:
    base = os.path.basename((name or '').replace('\\', '/'))
    base = ''.join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    return base.strip()[:80] or 'document'

@app.route('/api/inbox/upload', methods=['POST'])
def api_inbox_upload():
    """Raw file body from the router. Only the router address with the shared key; never printed automatically."""
    client_ip = request.remote_addr or ''
    if client_ip.startswith('::ffff:'):
        client_ip = client_ip[7:]
    if client_ip not in (ROUTER_IP, '127.0.0.1', '::1'):
        return jsonify({'success': False, 'error': 'Forbidden: source address not allowed'}), 403
    expected = get_send_key()
    if not expected:
        return jsonify({'success': False, 'error': 'Inbox is not configured on this server'}), 503
    provided = request.headers.get('X-Router-Key', '').strip()
    if not provided or not hmac.compare_digest(provided, expected):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    length = request.content_length
    if not length:
        return jsonify({'success': False, 'error': 'Empty file'}), 400
    if length > INBOX_MAX_BYTES:
        return jsonify({'success': False, 'error': f'File is larger than {INBOX_MAX_BYTES // (1024 * 1024)} MB'}), 413

    filename = clean_filename(unquote(request.headers.get('X-Filename', '')))
    try:
        ext = validate_extension(filename)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 415

    cleanup_expired_jobs()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total FROM inbox")
    row = cur.fetchone()
    if row['n'] >= INBOX_MAX_FILES or row['total'] + length > INBOX_TOTAL_BYTES:
        return jsonify({'success': False, 'error': 'Inbox is full. Please try again later.'}), 507

    file_id = f"in_{int(time.time())}_{secrets.token_hex(4)}"
    path = os.path.join(SPOOL_INBOX_DIR, file_id + ext)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    total_written = 0
    header_bytes = b''
    with open(fd, 'wb') as f:
        while True:
            chunk = request.stream.read(65536)
            if not chunk:
                break
            if len(header_bytes) < 4:
                header_bytes += chunk[:4 - len(header_bytes)]
            f.write(chunk)
            total_written += len(chunk)
            if total_written > INBOX_MAX_BYTES:
                break

    if total_written > INBOX_MAX_BYTES:
        try: os.remove(path)
        except Exception: pass
        return jsonify({'success': False, 'error': f'File is larger than {INBOX_MAX_BYTES // (1024 * 1024)} MB'}), 413

    if total_written != length:
        try: os.remove(path)
        except Exception: pass
        return jsonify({'success': False, 'error': 'Incomplete upload'}), 400

    if ext == '.pdf' and not header_bytes.startswith(b'%PDF'):
        try: os.remove(path)
        except Exception: pass
        return jsonify({'success': False, 'error': 'Not a PDF file'}), 415
    device = ''.join(ch for ch in request.headers.get('X-Device', '') if ch.isalnum() or ch in ':.-')[:40]
    with conn:
        conn.execute("INSERT INTO inbox (id, file_path, filename, size, device, received_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (file_id, path, filename, length, device, int(time.time())))
    return jsonify({'success': True, 'id': file_id})

@app.route('/api/inbox/count', methods=['GET'])
def api_inbox_count():
    cleanup_expired_jobs()
    cur = get_db().cursor()
    cur.execute("SELECT COUNT(*) AS n FROM inbox")
    return jsonify({'success': True, 'count': cur.fetchone()['n']})

@app.route('/api/inbox/list', methods=['GET'])
def api_inbox_list():
    if not verify_admin_token(request.headers.get('X-Admin-Token')):
        return jsonify({'success': False, 'error': 'Unauthorized: administrator login required'}), 401
    cleanup_expired_jobs()
    cur = get_db().cursor()
    cur.execute("SELECT * FROM inbox ORDER BY received_at DESC")
    now = int(time.time())
    files = [{
        'id': r['id'], 'filename': r['filename'], 'size': r['size'], 'device': r['device'],
        'received': datetime.fromtimestamp(r['received_at']).strftime('%H:%M:%S'),
        'age_minutes': max(0, (now - r['received_at']) // 60)
    } for r in cur.fetchall()]
    return jsonify({'success': True, 'files': files, 'ttl_hours': INBOX_TTL_SECONDS // 3600})

@app.route('/api/inbox/file', methods=['GET'])
def api_inbox_file():
    if not verify_admin_token(request.headers.get('X-Admin-Token')):
        return jsonify({'success': False, 'error': 'Unauthorized: administrator login required'}), 401
    cur = get_db().cursor()
    cur.execute("SELECT * FROM inbox WHERE id = ?", (request.args.get('id', ''),))
    row = cur.fetchone()
    if not row or not os.path.exists(row['file_path']):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    with open(row['file_path'], 'rb') as f:
        data = f.read()
    return Response(data, mimetype='application/octet-stream',
                    headers={'Content-Disposition': 'attachment', 'X-Filename': quote(row['filename']),
                             'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

@app.route('/api/inbox/delete', methods=['POST'])
def api_inbox_delete():
    if not verify_admin_token(request.headers.get('X-Admin-Token')):
        return jsonify({'success': False, 'error': 'Unauthorized: administrator login required'}), 401
    file_id = (request.form.get('id') or '').strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM inbox WHERE id = ?", (file_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({'success': False, 'error': 'File not found'}), 404
    try:
        if os.path.exists(row['file_path']):
            os.remove(row['file_path'])
    except Exception:
        pass
    with conn:
        conn.execute("DELETE FROM inbox WHERE id = ?", (file_id,))
    return jsonify({'success': True})

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 80))
    app.run(host='0.0.0.0', port=port)

