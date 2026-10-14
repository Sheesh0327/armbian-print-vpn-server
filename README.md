# 🖨️ Armbian Home Print & Server Gateway

Turn any low-cost ARM SBC or TV box (such as the **ZTE B860H** with Amlogic S905X) running **Armbian Linux** into a comprehensive, zero-maintenance home server.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Platform: Armbian / Debian](https://img.shields.io/badge/Platform-Armbian%20%7C%20Debian-red.svg)
![Arch: ARM64](https://img.shields.io/badge/Arch-ARM64-green.svg)

---

## 🌟 Capabilities Overview

### 1. 🖨️ Professional Web Print Portal (`Port 80`)
- **Direct Access**: Accessible at **`http://printserver.local`** (no port number needed) or `http://10.0.0.95`.
- **Large Dynamic Paper Preview**:
  - Live virtual paper sheet reflecting exact physical dimensions, paper stock, and orientation (**A4, Letter, Legal, 4×6 photo, 5×7, 8×10, Envelope DL**).
  - **Millimetric Margins Engine**: Choose between **Full Printable Area**, **Borderless (0 mm)**, **Normal (12.7 mm)**, or **Custom mm** (Top, Bottom, Left, Right).
  - Live Color vs. Monochrome rendering preview.
  - Multi-page PDF preview with interactive page navigation.
- **Dual UI Modes**:
  - **Simple Mode**: Upload $\rightarrow$ Pick copies & color $\rightarrow$ Print.
  - **Advanced Mode**: Media type (Plain, Glossy, Matte, Brochure), DPI quality, two-sided duplex, orientation, scaling, custom margins, and custom page ranges.
- **Native Office Document Conversion**: Automatically renders `.docx`, `.doc`, `.odt`, `.rtf` to PDF on the fly via headless LibreOffice.
- **Dark & Light Themes**: Apple/Vercel-inspired minimal aesthetic with persistent theme memory and zero-flicker loading.

### 2. 📄 Web Flatbed Document Scanner (`/scan`)
- **Browser-Based Scanning**: Scan documents and photos straight from your browser or mobile phone without installing manufacturer scanning software.
- **Adjustable Quality**: 75 DPI (Draft), 150 DPI (Document), 300 DPI (High Res), and 600 DPI (Archival).
- **Multiple Output Formats**: Direct export to **PDF**, **PNG**, or **JPEG**.
- **1-Click Print & Auto-Save**: Automatically saves scans to the Samba share (`\\printserver.local\Public\Scans`) and offers instant one-click transfer into the Print portal.

### 3. 📱 Zero-Install Mobile Printing (AirPrint & Mopria)
- **Apple AirPrint**: Fully announced via Avahi DNS-SD (`_universal._sub._ipp._tcp`). Native printing from any iPhone, iPad, or Mac via the standard Share $\rightarrow$ Print menu.
- **Android Mopria**: Discovered by Android's Default Print Service.
- **Windows / Linux**: Discovered automatically over the local network.

### 4. 🛡️ Network-Wide Ad-Blocking & DNS (AdGuard Home)
- Pre-installed and running on **`http://printserver.local:3000`**.
- Blocks ads, trackers, and malware across all smart TVs, phones, and computers on your Wi-Fi.

### 5. 📁 Network File Sharing (Samba / SMB)
- Public network share located at **`\\printserver.local\Public`** (or `smb://printserver.local/Public`).
- Instant file dropping, backups, and shared storage across Windows, macOS, iOS, and Android.
- Scanned documents automatically saved to `\\printserver.local\Public\Scans`.

### 6. 🌐 Remote VPN Bridge (Tailscale Subnet Router)
- Encrypted WireGuard-based mesh access to your entire home LAN from anywhere in the world.
- Print documents remotely from 5G cellular data or cafe Wi-Fi.
- Access services at `http://100.104.185.40`.

### 7. 🔒 Zero-Maintenance & Daily Backups
- **Unattended Security Patches**: Automatic background installation of Debian security updates.
- **Automated Daily Backups**: Archives CUPS configs, drivers, and server settings daily at 3:00 AM to `\\printserver.local\Public\backups`.

---

## 🚀 Quick Start (Automated Setup)

```bash
git clone https://github.com/Sheesh0327/armbian-print-vpn-server.git
cd armbian-print-vpn-server
sudo chmod +x setup.sh
sudo ./setup.sh
```

---

## 🌐 Network Service Directory

| Service | Access URL | Purpose |
| :--- | :--- | :--- |
| **Web Print Portal** | [`http://printserver.local`](http://printserver.local) | Drag & drop document printing |
| **AdGuard Home** | [`http://printserver.local:3000`](http://printserver.local:3000) | Network ad-blocking dashboard |
| **Samba File Share** | `\\printserver.local\Public` | Shared network folder |
| **CUPS Admin** | [`https://printserver.local:631`](https://printserver.local:631) | Raw hardware and queue administration |
| **Remote Access** | `http://100.104.185.40` | Remote access via Tailscale |

---

## 📜 License
Licensed under the [MIT License](LICENSE).
