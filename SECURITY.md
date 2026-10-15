# Security Policy & Threat Model

## 1. Scope & Objective

This document outlines the security architecture, threat model, and trust boundaries of the **Armbian Web Print Appliance & Gateway** (`armbian-print-vpn-server`).

The appliance is designed for mixed-trust physical and network environments where a single low-cost SBC (e.g. ZTE B860H on Armbian Linux) services both private LAN workstations, upstream modem devices, and guest commercial users, without compromising internal network isolation.

---

## 2. Network Trust Boundaries & Threat Model

```
+-----------------------------------------------------------------------------+
| Upstream Modem Network (192.168.1.0/24)                                      |
| Untrusted / Semi-trusted clients connecting to router WAN IP (192.168.1.13) |
+-----------------------------------------------------------------------------+
                                       |
                                       | (Only DNAT TCP 80, 631; Inbound UDP 5353)
                                       v
+-----------------------------------------------------------------------------+
| OpenWrt Intermediate Router (192.168.1.13 / 10.0.0.1)                       |
| Perimeter firewall rejecting all unmapped ports (22, 53, 2050, 3000, 445)   |
+-----------------------------------------------------------------------------+
               |                                               |
               | (Isolated Guest VLAN)                         | (LAN br-lan: 10.0.0.1)
               v                                               v
+-----------------------------+               +-------------------------------+
| Guest Network (10.0.30.0/24)|               | Downstream LAN (10.0.0.0/24)  |
| openNDS Captive Portal      |               | - Windows PC (10.0.0.112)     |
| [BLOCKED FROM 10.0.0.95]    |               | - Print Server (10.0.0.95)    |
+-----------------------------+               +-------------------------------+
```

### Trust Boundary Analysis

| Zone | Subnet / Interface | Threat Profile | Allowed Access on Print Server | Enforcement Mechanism |
| :--- | :--- | :--- | :--- | :--- |
| **Upstream Modem Network** | `192.168.1.0/24` | Shared household / office devices; untrusted visitors. | Web Print Portal (`:80`), CUPS IPP (`:631`), mDNS (`:5353`). | OpenWrt DNAT rules; CUPS `cupsd.conf` restricts `/admin` to `10.0.0.0/24`; router management ports blocked. |
| **PisoWiFi Guest Network** | `10.0.30.0/24` | Paying commercial Wi-Fi customers; zero-trust. | **NONE.** Direct access is blocked. | OpenWrt firewall forward rule blocks all traffic from `guest` to `lan` (`10.0.0.0/24`). |
| **Downstream Private LAN** | `10.0.0.0/24` | Owner workstations (`10.0.0.112`) and appliances. | Web Print Portal (`:80`), CUPS Admin (`:631`), SSH (`:22`). | Local subnet access permitted with authentication. |
| **Router-Assisted Guest Print** | `10.0.0.1` | Automated requests initiated by OpenWrt router software. | Router API (`/api/router/*`) only. | Source IP validation (`10.0.0.1`) and shared secret header (`X-Router-Key`). |
| **Tailscale Remote VPN** | `tailnet` | Remote administrative and mobile access. | Port 80 and 631 only. | No exit node; subnet route restricted strictly to `10.0.0.95/32`; tailnet ACLs. |

---

## 3. Defense-in-Depth Implementation

### A. Privilege Minimization & Sandboxing
- **Unprivileged Runtime**: The application executes under a dedicated system user `webprint` in group `lp`. Root privileges are completely dropped before starting the WSGI worker processes.
- **Systemd Sandboxing**:
  - `NoNewPrivileges=yes`: Prevents child processes from gaining elevated permissions via setuid binaries.
  - `ProtectSystem=strict`: Mounts the root filesystem read-only for the service.
  - `ProtectHome=yes`: Denies access to `/root` and `/home`.
  - `PrivateTmp=yes`: Isolates `/tmp` inside a private mount namespace.
  - `ReadWritePaths`: Grants write access strictly to `/var/lib/webprint`, `/var/spool/webprint`, and `/tmp`.
  - `AmbientCapabilities=CAP_NET_BIND_SERVICE`: Allows binding to privileged port 80 without root execution.

### B. Authentication & Credential Storage
- **No Default Passwords**: Initial installation dynamically generates a 16-character random password stored exclusively as a salted `scrypt` hash in SQLite.
- **Timing-Safe Comparison**: All token, PIN, and key checks use `hmac.compare_digest` to prevent side-channel timing attacks.
- **Login Rate Limiting**: The `/api/admin/auth` endpoint tracks failed login attempts by client IP. Five consecutive failures trigger a 60-second lockout.
- **Protected Administrative Endpoints**:
  - `/api/reports/ledger`
  - `/api/reports/export.csv`
  - `/api/pin/list`
  - `/api/admin/action`
  All require valid administrator session tokens (`X-Admin-Token`).

### C. Confidential PIN Queue
- **6-Digit Entropy**: Held jobs require a 6-digit release code (`^[0-9]{6}$`).
- **HMAC Salting**: PINs are hashed using HMAC-SHA256 with a 256-bit server secret key (`/var/lib/webprint/secret.key`, mode 0600).
- **Encryption at Rest**: Staged files are encrypted with AES-256-GCM. Decryption keys never leave the server host.
- **Brute-Force Auto-Purge**: After 5 failed PIN release attempts for any job, the staged encrypted file and database record are permanently deleted.
- **IP Rate Limiting**: Maximum 10 PIN release attempts per minute per IP.

### D. File Ingestion & Conversion Hardening
- **Strict Allow-Listing**:
  - File extensions are validated against a strict allow-list (`.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.heic`, `.heif`, `.avif`, `.tiff`, `.docx`, `.doc`, `.odt`, `.rtf`, `.txt`, `.csv`, `.xlsx`, `.xls`, `.pptx`, `.ppt`).
  - Print options (`pagesize`, `mediatype`, `quality`, `colormodel`, `duplex`, `resolution`) are checked against fixed string sets. Raw form input is never interpolated into `lp -o` parameters.
- **Size & Page Limits**: Maximum file upload is capped at 50 MB; maximum document length is capped at 50 pages per job.
- **Conversion Isolation**:
  - Headless LibreOffice executes with per-job user installation profiles (`-env:UserInstallation=file://${temp_dir}/lo_profile`) and a 45-second timeout.
  - ImageMagick executes with a 30-second timeout under a security policy disabling `MVG`, `MSL`, `URL`, `HTTPS`, `HTTP`, `FTP`, `EPHEMERAL`, `PS`, and `EPS` coders.
  - Temporary files and conversion workspaces are guaranteed to be unlinked in `finally` blocks.

### E. Commercial Wi-Fi Router Integration API
- **Strict Origin Check**: Calls to `/api/router/*` are rejected with HTTP 403 unless the connecting IP is `10.0.0.1` (or local test loopback).
- **Cryptographic Header**: Every request must provide the shared secret in `X-Router-Key` matching `/etc/webprint/router.key` (mode 0640).
- **Two-Stage Execution**: Router jobs are staged as `HELD` and are never printed without an explicit `release` call. Held jobs expire and purge automatically after 15 minutes.
- **Privacy Preservation**: Guest print jobs are logged with document title `WiFi Guest Job` and `source="wifi"`. Document contents and client filenames are never retained or logged.

---

## 4. Reporting a Vulnerability

If you discover a security vulnerability in this project:
1. Do **NOT** file a public GitHub issue.
2. Submit a confidential report detailing the vulnerability and reproduction steps to the repository maintainer.
3. Allow reasonable time for verification and patch deployment before any public disclosure.
