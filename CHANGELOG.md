# Changelog

All notable changes to the Armbian Print Appliance and Gateway project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.3.0] - 2026-10-16

### Added
- **Preview Caching & Single-Pass Conversion**: Uploaded Office documents (.docx, .xlsx, .pptx) and .heic photos are converted once under concurrency control, cached at `/api/preview/<preview_id>.pdf`, rendered directly in the Chromium-style PDF.js preview desk, and printed instantly with 0ms conversion delay.
- **Physical Millimeter ID Photo Calibration**: Recalibrated ID Photo Studio package templates (Rush Combo A & B, All 2x2, All 1x1, Passport, Wallet) to strict 300 DPI physical measurements ($2\times 2" = 600\text{ px}$, $1\times 1" = 300\text{ px}$, Passport $= 413\times 531\text{ px}$, Wallet $= 750\times 1050\text{ px}$) on both 4×6" and A4 photo paper. Added `-o print-scaling=none` flag to guarantee exact ruler dimensions without CUPS margin shrink.
- **Real-Time CUPS Job Tracking**: Added `/api/jobs/<job_id>/status` querying CUPS `lpstat` to provide live feedback on print status (Spooling $\rightarrow$ Printing on device $\rightarrow$ Completed).
- **Accurate Billing & Sheet Accounting**: `parse_page_ranges` (backend) and `parseClientPageRanges` (client) accurately calculate active page selections and duplex sheet counts (`ceil(pages/2)`).

### Fixed
- **Frontend/Backend Parameter Alignment**: Added `KGray` to allowed ColorModels (passing `-o ColorModel=KGray` and `-o print-color-mode=monochrome`), canonical mapping for `4x6in`, `5x7in`, `EnvelopeDL`, and media types `FastGlossy`, `CoatedMatte`, `CoatedBrochure`.
- **Concurrency & OOM Protection**: Added `CONVERSION_SEMAPHORE` to serialize LibreOffice and ImageMagick conversions, preventing OOM on 913 MB RAM hardware.
- **SQLite Concurrency**: Configured WAL mode (`PRAGMA journal_mode=WAL`), `PRAGMA synchronous=NORMAL`, and 20s busy timeout to eliminate `database is locked` errors.
- **Memory Streaming**: Refactored `/api/inbox/upload` to stream file uploads to disk in 64 KB chunks rather than buffering whole files in Python heap memory.

## [2.2.0] - 2026-10-15

### Added
- **Guest file inbox**: files that Wi-Fi guests send from the captive portal (relayed by the router, no payment, nothing printed automatically) show up in an Inbox bar on the Print tab for the admin: Open for printing / Delete. New endpoints `/api/inbox/upload` (router + key only), `count`, `list`, `file`, `delete`; new `inbox` table and `/var/spool/webprint/inbox`; `setup.sh` creates `/etc/webprint/send.key`. Limits and expiry are configurable. Tests in `tests/test_inbox.py`.

## [2.1.0] - 2026-10-15

### Removed
- The Wi-Fi quick print / captive portal payment feature is gone again to keep this server simple: no `/api/router/*`, no admin review queue, no router key, no `router_jobs` use. Guests on the PisoWiFi network cannot print.

### Added
- **Trusted devices only**: every request is checked against `TRUSTED_NETWORKS` (default `10.0.0.0/24,192.168.1.0/24`, plus loopback) and answered with HTTP 403 otherwise. Configured in `/etc/webprint/webprint.env` (created by `setup.sh`, never overwritten). CUPS print queues get matching `Allow from` lines (never `/admin`).
- Tests for the allow-list (`tests/test_trusted_devices.py`) and a test that the page script parses (`tests/test_page_script.py`, uses Node).

### Fixed
- **The whole web page script failed to parse** (an unescaped quote in the "login required" row), so tabs and admin did nothing.
- Admin login failed at random with two gunicorn workers (tokens and limits live in memory): now one worker with four threads.
- Printing from the modem network failed once `--remote-any` was removed (CUPS did not treat 192.168.1.x as local): fixed with the allow-list above.
- `/api/inspect` had no rate limit and ignored `PUBLIC_PRINT=off`.
- The initial admin password no longer appears on a command line; admin tokens are no longer accepted in URLs except for the CSV download; stale Canva text removed from the README; the backup no longer lists a file that is never installed.

## [2.0.0] - 2026-10-15

### Security Hardening
- **Unprivileged System User**: Migrated web print runtime from `root` to a dedicated `webprint` system user in group `lp`.
- **Systemd Sandboxing**: Configured `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, and restricted `ReadWritePaths` to `/var/lib/webprint`, `/var/spool/webprint`, and `/tmp`.
- **Privileged Port Binding**: Enabled `AmbientCapabilities=CAP_NET_BIND_SERVICE` and `CapabilityBoundingSet=CAP_NET_BIND_SERVICE` allowing non-root binding to TCP port 80.
- **Production WSGI Deployment**: Integrated `gunicorn` with multi-worker concurrency and request timeouts, replacing the Flask development server.
- **Administrator Authentication Overhaul**:
  - Eliminated default passwords. Initial install dynamically generates a secure 16-character alphanumeric password displayed once during setup.
  - Stored exclusively as a salted `scrypt` hash using `werkzeug.security.generate_password_hash`.
  - Added sliding-window rate limiting on `/api/admin/auth` (5 failed attempts per minute per IP triggers temporary lockout).
  - Admin session tokens compared using timing-safe `hmac.compare_digest`.
- **Protected Endpoints**:
  - `/api/reports/ledger`, `/api/reports/export.csv`, and `/api/pin/list` now strictly require valid administrator authentication.
  - Added direct walk-up job release for end-users, preventing unauthorized visibility into other users' held documents.
- **Confidential PIN Queue Hardening**:
  - Upgraded to 6-digit PINs (`^[0-9]{6}$`).
  - Implemented HMAC-SHA256 salted PIN hashing using a 256-bit server secret key (`/var/lib/webprint/secret.key`, mode 0600).
  - Staged print files are encrypted at rest using AES-256-GCM (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`).
  - Added brute-force protection: jobs are permanently deleted after 5 incorrect PIN attempts.
  - Applied per-IP rate limiting (maximum 10 PIN release attempts per minute).
- **CUPS Hardening**:
  - Removed `--remote-admin` and `--remote-any` flags from CUPS setup; retained `--share-printers`.
  - Restricted `/admin`, `/admin/conf`, and `/admin/log` locations in `cupsd.conf` strictly to `localhost` and the trusted `10.0.0.0/24` subnet.
  - Denied administrative access to upstream WAN clients connecting via port 631.
- **Document Conversion Sandbox**:
  - Created isolated, per-job temporary directories (`tempfile.mkdtemp(prefix="conv_")`) with mandatory cleanup in `finally` blocks.
  - Restricted LibreOffice user profiles per-job via `-env:UserInstallation=file://${temp_dir}/lo_profile`.
  - Enforced strict process execution timeouts (30 seconds for ImageMagick, 45 seconds for LibreOffice).
  - Applied hardened ImageMagick `policy.xml` explicitly disabling `MVG`, `MSL`, `URL`, `HTTPS`, `HTTP`, `FTP`, `EPHEMERAL`, `PS`, and `EPS` coders.
  - Replaced `shell=True` cache purging with safe Python directory traversal via `shutil` and `os.remove`.
- **Strict Input Validation**:
  - Enforced copies bounds between 1 and 99.
  - Validated page ranges against regular expression `^[0-9,\- ]+$`.
  - Restricted printers to active queues enumerated by `lpstat -e`.
  - Validated all print parameters (`pagesize`, `mediatype`, `quality`, `colormodel`, `duplex`, `orientation`, `resolution`) against strict allow-lists. Prevented raw string injection into `lp -o` parameters.
  - Enforced maximum upload size of 50 MB (`MAX_CONTENT_LENGTH = 50 * 1024 * 1024`).
  - Enforced maximum page ceiling of 50 pages per job.
  - Validated document extensions against strict allow-list.
- **Tailscale Security**:
  - Removed `--advertise-exit-node` flag.
  - Restricted optional subnet routing strictly to `10.0.0.95/32` instead of the entire LAN.

### Router Integration API
- **Dedicated Paid Print API for OpenWrt Router**:
  - Authenticated via shared secret header (`X-Router-Key` matching `/etc/webprint/router.key`, mode 0640).
  - Source IP restricted strictly to OpenWrt router (`10.0.0.1`) and loopback.
  - `POST /api/router/job`: Ingests document, converts, counts pages, and stages job as HELD with 15-minute auto-expiry.
  - `POST /api/router/release`: Idempotently releases HELD job to printer tray once payment is confirmed.
  - `POST /api/router/cancel`: Purges staged document and marks job cancelled.
  - `GET /api/router/status`: Real-time state query (`queued`, `printing`, `done`, `failed`, `paper_out`, `offline`).
  - `GET /api/router/health`: Telemetry and diagnostic status of connected printers and paper/ink alerts.
  - Audit logs tagged with `source="wifi"` without logging file contents.
- **Public Print Gateway Toggle**: Added `PUBLIC_PRINT=on|off` environment variable and database setting to permit or restrict public walk-up printing independently of the router API.

### Repository Hygiene & Correctness
- **Automated Backup System**:
  - Rewrote `scripts/backup-server.sh` to back up `/var/lib/webprint` (SQLite DB and keys), `/etc/webprint`, `/opt/webprint`, and CUPS configurations.
  - Relocated backup destination to `/var/backups/webprint` (directory mode 0700, archives mode 0600).
  - Removed world-readable share destinations and sensitive Samba password exports.
  - Added `systemd/backup-webprint.service` and `systemd/backup-webprint.timer` scheduled for daily execution at 03:00 with 14-day archive retention.
- **Sanitized Documentation & Artifacts**:
  - Removed static Tailscale IPs, default passwords, and internal network assumptions from `README.md`.
  - Added comprehensive `SECURITY.md` covering architecture, threat models, and trust boundaries.
  - Documented pinned vendor library (PDF.js v3.11.174).
  - Aligned `setup.sh` defaults with documentation.
- **Testing & Continuous Integration**:
  - Added unit test suite in `tests/` covering input validators, rate limiters, token validation, HMAC hashing, and router API endpoints.
  - Added GitHub Actions workflow (`.github/workflows/ci.yml`) executing `shellcheck`, `flake8`, and `pytest`.
  - Updated `.gitignore` to prevent tracking of SQLite databases (`*.db`, `*.sqlite*`), keys (`*.key`), and secrets.

---

## [1.0.0] - 2026-10-14
- Initial release of Armbian Web Print server for ZTE B860H.
- Native web portal with print preview and document conversion.
- Fabric.js client-side ID photo and design maker.
