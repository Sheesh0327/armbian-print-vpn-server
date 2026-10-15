# Turnkey Commercial Print Appliance and Server Gateway

Turn low-cost ARM SBCs and TV boxes (such as the ZTE B860H with Amlogic S905X, 1 GB RAM) running Armbian Linux into a hardened commercial print appliance, client-side design kiosk, and integrated driverless print gateway.

---

## System Architecture and Network Placement

The print server sits on the downstream local network behind a gateway router (OpenWrt) separating upstream modem clients and isolated guest Wi-Fi clients.

```
       [ISP Modem / Upstream Subnet: 192.168.1.0/24]
                            |
                 (WAN: 192.168.1.13)
             [OpenWrt Gateway / Router]
                 (LAN: 10.0.0.1/24)
                            |
         +------------------+------------------+
         |                                     |
   [Downstream LAN: 10.0.0.0/24]        [Guest Wi-Fi (openNDS): 10.0.30.0/24]
   - Print Server: 10.0.0.95:80          - Strictly isolated via OpenWrt firewall
   - Workstation PC: 10.0.0.112          - Cannot route or connect to 10.0.0.95
   - CUPS Server: 10.0.0.95:631          - Prints via OpenWrt Router API hook
```

### Network Access Rules
1. **Downstream LAN (`10.0.0.0/24`)**: Full access to the Web Print portal (Port 80) and raw IPP printing (Port 631). CUPS web administration (`https://10.0.0.95:631/admin`) is restricted to localhost and `10.0.0.0/24`.
2. **Upstream Modem Network (`192.168.1.0/24`)**: Accesses Port 80 and Port 631 forwarded via OpenWrt DNAT rules. Avahi reflector on OpenWrt advertises the printer to modem devices. CUPS administrative endpoints reject upstream connections.
3. **Guest Wi-Fi Network (`10.0.30.0/24`)**: Direct layer-3 access to `10.0.0.95` is blocked by firewall forwarding drop rules. Guest print jobs are handled by the router calling the internal Router API (`/api/router/*`) over the trusted LAN interface.

---

## Core Capabilities

### 1. Web Print Portal (Port 80)
- **Production WSGI Service**: Runs Gunicorn bound to `0.0.0.0:80` under the unprivileged system user `webprint` (group `lp`) utilizing Linux ambient capabilities (`CAP_NET_BIND_SERVICE`).
- **Dynamic CUPS Auto-Discovery**: Automatically enumerates connected USB and IPP network printers (`lpstat -e`) with real-time status reporting.
- **Strict Input Validation**:
  - Copies bounded between 1 and 99.
  - Page ranges verified against `^[0-9,\- ]+$`.
  - Printers strictly matched against enumerated active CUPS queues.
  - Page sizes, media types, print qualities, color models, duplex modes, and resolutions validated against fixed server-side allow-lists before passing to `lp -o`. Raw form values are never passed to the print spooler.
  - Maximum upload size enforced at 50 MB, and maximum page count enforced at 50 pages per job.
- **Universal Document Ingestion**:
  - PDF, plain text, and Office formats (`.docx`, `.xlsx`, `.pptx`, `.odt`, `.rtf`).
  - Mobile image formats (`.heic`, `.heif`, `.avif`, `.webp`, `.png`, `.jpg`).
- **Vendored PDF Preview Engine**:
  - Mozilla PDF.js v3.11.174 vendored locally (`webprint/static/pdf.min.js` and `webprint/static/pdf.worker.min.js`) for reliable, air-gapped document rendering without external CDN dependencies.
- **Public Portal Toggle**: Set `PUBLIC_PRINT=off` in the environment to disable general walk-up uploads while preserving the router paid-print pipeline.

### 2. Client-Side Canva Design Studio
- **Zero Server RAM Footprint**:
  - Powered by client-side HTML5 Canvas, Fabric.js, jsPDF, and QRCode.js.
  - All graphic rendering, typography calculations, image filters, and vector manipulation execute inside the client browser.
- **Built-in Templates and Tools**:
  - Pre-built templates: Promotional flyers, corporate memos, guest Wi-Fi connection signs, certificates of recognition, and 2x2 passport ID photo grids.
  - Vector stamp assets: `APPROVED`, `CONFIDENTIAL`, `PAID`, `URGENT`, and `DRAFT`.
  - Direct 300 DPI canvas rasterization sent straight to the print spooler.

### 3. Encrypted Confidential Pull-Printing (PIN Queue)
- **Cryptographic Storage**: Held print files are encrypted at rest using AES-256-GCM. The encryption key is generated at install time in `/var/lib/webprint/secret.key` with file mode `0600` (readable only by user `webprint`).
- **6-Digit PIN Security**: PINs are strictly 6 digits (`000000` to `999999`). Only a salted HMAC-SHA256 digest is stored in the SQLite database.
- **Brute-Force Protection**: 
  - Maximum 5 failed PIN attempts per job; reaching the threshold immediately wipes the encrypted spool file and purges the job from the database.
  - IP-based rate limiting (10 attempts per minute per client IP).
  - Unclaimed jobs automatically purge after 24 hours.

### 4. Accounting and Audit Ledger
- **Transaction Logging**: Records timestamp, job name, printer, page count, copies, color mode, billing amount, client IP, and source (`direct` vs `wifi`).
- **Protected Administration**:
  - Ledger view (`/api/reports/ledger`), CSV export (`/api/reports/export.csv`), and PIN queue management (`/api/pin/list`) require an active admin session.
  - Hardware controls (reboot, shutdown, CUPS restart, spool purge) require admin credentials.

### 5. Authentication and Hardened Access Control
- **No Default Passwords**: `setup.sh` generates a cryptographically random admin password upon initial installation and displays it once to the terminal.
- **Salted Password Hashing**: Passwords stored using `scrypt` hashing via Werkzeug security.
- **Rate-Limited Logins**: 5 failed login attempts per minute per IP triggers temporary lockout.
- **Constant-Time Verification**: Session authentication tokens verified using `hmac.compare_digest`.

### 6. Sandboxed Document Conversion
- **Privilege Separation**: Conversions execute under the unprivileged `webprint` system account.
- **Isolated User Profiles**: LibreOffice conversions run with an isolated, per-job profile directory (`-env:UserInstallation=file:///tmp/lo_<job>`).
- **Restricted ImageMagick Policy**: Installed policy (`/etc/ImageMagick-6/policy.xml`) explicitly disables dangerous coders (`MVG`, `MSL`, `URL`, `HTTPS`, `HTTP`, `FTP`, `EPHEMERAL`, `PS`, `EPS`).
- **Execution Timeouts**: ImageMagick limited to 30 seconds; LibreOffice limited to 45 seconds. Per-job temporary files are strictly cleaned up inside `finally` blocks.
- **Safe Directory Purges**: Python `shutil` and `os` primitives used for spool cleanup instead of shell invocations.

### 7. OpenWrt Paid Wi-Fi Router Integration
A dedicated API enables automated, coin-operated, or voucher-based print integration with an upstream OpenWrt captive portal (e.g. openNDS / PisoWiFi):
- **Source Verification**: Requests are accepted only from router IP `10.0.0.1`.
- **Shared Secret Authentication**: Requires an `X-Router-Key` HTTP header matching the secret in `/etc/webprint/router.key` (mode `0600`).
- **Endpoints**:
  - `POST /api/router/job`: Uploads document, converts format, counts pages, and stages the job as `HELD` (max 15-minute expiration). Returns `{job_id, pages, color, expires_at}`.
  - `POST /api/router/release`: Releases and prints a held job upon confirmed payment. Idempotent.
  - `POST /api/router/cancel`: Immediately deletes the held job and staging file.
  - `GET /api/router/status?job_id=...`: Returns current state (`queued`, `printing`, `done`, `failed`, `paper_out`, `offline`).
  - `GET /api/router/health`: Reports CUPS daemon health, default printer status, and alert states.
- **Audit Logging**: All completed router print jobs are recorded in the accounting ledger with `source="wifi"`.

### 8. Automated Daily Backups
- **Service and Timer**: Handled via `backup-webprint.service` and `backup-webprint.timer`.
- **Schedule**: Triggers daily at 03:00.
- **Data Protection**: Archives the SQLite database (`/var/lib/webprint/webprint.db`), encryption keys (`/etc/webprint`), and CUPS configuration into `/var/backups/webprint/` with file mode `0600`.
- **Retention**: Automatically purges archives older than 14 days.

---

## Installation and Deployment

### 1. Automated Installation
Clone the repository on the Armbian appliance and run the installer:

```bash
git clone https://github.com/Sheesh0327/armbian-print-vpn-server.git
cd armbian-print-vpn-server
sudo chmod +x setup.sh scripts/backup-server.sh
sudo ./setup.sh
```

During installation:
- System packages (`cups`, `gunicorn`, `poppler-utils`, `libreoffice-writer-nogui`, `imagemagick`, `python3-cryptography`) are installed.
- Unprivileged user `webprint` is created and added to group `lp`.
- Cryptographic keys (`secret.key` and `router.key`) are generated with mode `0600`.
- A random admin password is generated and displayed once on the console. Save this password securely.
- The Gunicorn systemd service and daily backup timer are enabled and started.

### 2. Service Management
```bash
# Check Web Print service status
sudo systemctl status webprint.service

# View Web Print logs
sudo journalctl -u webprint.service -f

# Check daily backup timer status
sudo systemctl status backup-webprint.timer
```

---

## Network Service Directory

| Service | Access URL / Port | Security and Restrictions |
| :--- | :--- | :--- |
| **Web Print Portal** | `http://printserver.local` (Port 80) | Unprivileged Gunicorn, rate-limited, input-validated |
| **CUPS Printing (IPP)** | `http://printserver.local:631/printers/...` | Shared queues accessible by LAN and upstream modem |
| **CUPS Admin** | `https://printserver.local:631/admin` | Restricted to localhost and LAN `10.0.0.0/24` |
| **Router API** | `http://10.0.0.95:80/api/router/*` | Restricted to IP `10.0.0.1` + `X-Router-Key` header |
| **Daily Backups** | `/var/backups/webprint/` | Mode `0600`, root/webprint private directory |

---

## Optional Modules

The following supplementary modules are optional and not installed by the core setup script to maintain a minimal attack surface and conserve memory on 1 GB devices:

### 1. Samba File Sharing (Optional)
If Windows network folder sharing is needed for document exchange:
```bash
sudo apt-get install -y samba
# Configure /etc/samba/smb.conf with appropriate access controls
sudo systemctl restart smbd
```

### 2. SANE Network Scanner (Optional)
If an all-in-one USB flatbed scanner is attached:
```bash
sudo apt-get install -y sane sane-utils
# Enumerate scanners
scanimage -L
```

### 3. AdGuard Home (Optional)
For appliance-level DNS caching or ad-blocking:
- Install separately following official Armbian AdGuard instructions.
- Ensure AdGuard does not bind to Port 80 or interfere with mDNS resolution on Port 5353.

### 4. Tailscale Remote Mesh VPN (Optional)
To securely access the print appliance over the internet:
- Install Tailscale via `curl -fsSL https://tailscale.com/install.sh | sh`.
- Authenticate: `sudo tailscale up`.
- **Security Guidance**:
  - Do **not** advertise exit node functionality (`--advertise-exit-node`).
  - If subnet routing is required, advertise only the appliance itself (`--advertise-routes=10.0.0.95/32`), never the wider LAN or guest network.
  - Configure Tailscale Access Control Lists (ACLs) in the admin console to restrict access exclusively to authorized user devices.

---

## Verification and Testing

Execute the test suite to verify validators, rate limiting, authentication, and the router API:

```bash
# Run automated tests
pytest tests/
```

### Test Checklist
1. **Upstream Modem Network Printing**:
   - Connect a smartphone to the upstream modem network (`192.168.1.0/24`).
   - Navigate to `http://192.168.1.13` (router WAN). Verify the portal loads and test-print a document.
   - Verify that accessing `https://192.168.1.13:631/admin` is blocked or rejected.
2. **Downstream LAN Printing**:
   - From a Windows PC (`10.0.0.112`), navigate to `http://10.0.0.95` or `http://printserver.local`.
   - Submit a 6-digit PIN job and verify pull-print release.
3. **Guest Network Isolation Verification**:
   - Connect a device to the guest Wi-Fi network (`10.0.30.0/24`).
   - Attempt to load `http://10.0.0.95`. Confirm the connection times out and no packets reach the print server.

---

## Security Policy

For vulnerability reporting procedures, threat models, and architectural boundaries, see [SECURITY.md](SECURITY.md).

---

## License

Licensed under the [MIT License](LICENSE).
