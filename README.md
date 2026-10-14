# 🖨️ Turnkey Commercial Print Appliance & Server Gateway

Turn any low-cost ARM SBC or TV box (such as the **ZTE B860H** with Amlogic S905X, 1GB RAM) running **Armbian Linux** into a commercial-grade, driverless print kiosk, document workstation, and network server.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Platform: Armbian / Debian](https://img.shields.io/badge/Platform-Armbian%20%7C%20Debian-red.svg)
![Arch: ARM64](https://img.shields.io/badge/Arch-ARM64-green.svg)
![Memory Footprint: <400MB](https://img.shields.io/badge/RAM%20Idle-~380MB-brightgreen.svg)

---

## 🌟 Commercial Capabilities Overview

### 1. 🖨️ Universal Multi-Printer Web Portal (`Port 80`)
- **Direct Access**: Accessible at **`http://printserver.local`** or `http://10.0.0.95`.
- **Dynamic Printer Auto-Discovery**: Automatically queries connected USB or network IPP printers (`HP`, `Canon`, `Epson`, `Brother`, label printers) with real-time status and default queue auto-selection.
- **Universal Document & Mobile Ingestion**:
  - **PDF & Office Suite**: Native headless conversion for `.pdf`, `.docx`, `.doc`, `.xlsx`, `.xls`, `.pptx`, `.ppt`, `.odt`, `.rtf`, `.csv`, `.txt`.
  - **Modern Smartphone Formats**: iPhone `.heic`/`.heif` and modern `.avif`, `.webp`, `.png`, `.jpg`, `.tiff`.
- **Pre-Flight Accounting & Cost Estimator**:
  - Real-time page counting via `pdfinfo`.
  - Live cost calculation configured in Philippine Pesos (`₱`) (e.g. ₱2.00/page B&W, ₱5.00/page Color).
- **Millimetric Margins & Quality Control**:
  - Presets: Printable area, Borderless (0mm), Normal (12.7mm), or Custom mm.
  - DPI Selection: 150 DPI (Fast), 300 DPI (High-Quality), 600 DPI (Ultra-Fine), 1200 DPI.

### 2. 🎨 Client-Side "Canva-Style" Design & Print Studio
- **100% Browser-Offloaded (Zero Server Memory Overhead)**:
  - Runs in the client's web browser using HTML5 Canvas / Fabric.js and `jsPDF`.
  - The client's PC/smartphone GPU and RAM handle all layout rendering, dragging, scaling, and rasterization.
- **Turnkey Kiosk Tools**:
  - **Presets**: A4 Document (Default), US Letter, 4x6" Photo Print, 2x2" ID Photo Grid.
  - **DPI Selector**: Choose 150 DPI (Draft), 300 DPI (Standard Print), or 600 DPI (Ultra Crisp).
  - **Enterprise Stamps**: 1-click vector stamps (`APPROVED`, `CONFIDENTIAL`, `PAID`, `URGENT`, `Authorized Signature` lines).
  - **Image Filters**: B&W Grayscale, High Contrast, Sepia Vintage, Brightness/Contrast.
  - **1-Click 2x2 ID Photo Grid Maker**: Upload an ID photo $\rightarrow$ automatically tiles 6x framed 2x2" photos with cut guidelines on an A4 sheet.
  - **1-Click Direct Print**: Browser compiles 300 DPI high-res canvas directly and spools to the printer in one tap.

### 3. 📠 Walk-Up Digital Photocopier
- **Standalone Kiosk Mode**: Large high-contrast touch controls designed for a tablet or smartphone mounted near the printer.
- **Instant Duplication**:
  - One tap triggers scanner acquisition, ImageMagick contrast/brightness normalization, and direct CUPS spooling with zero manual file downloads.
  - Controls: B&W vs Full Color, 1–99 copies stepper, Darkness slider (-20% to +20%), 150/300/600 DPI, and Paper Size (A4/Letter).

### 4. 🔒 Confidential Pull-Printing (PIN-Release Queue)
- **Secure Staging Spool**: Jobs marked confidential are encrypted and held in `/var/spool/webprint/pinned/`. The printer stays silent until unlocked.
- **PIN Unlock**: User walks up to the printer, enters their 4-digit PIN in the portal, and releases the job to the tray.
- **Auto-Purge Security**: Unclaimed PIN jobs automatically expire and delete after 24 hours.

### 5. 📊 Print Accounting & Audit Ledger
- **Complete Job Audit Trail**: Tracks timestamp, document title, printer, page count, copies, color mode, cost (`₱`), and client IP.
- **Revenue & Volume Analytics**: Real-time summary cards for total jobs, total pages, and total billed cost.
- **Exportable Reports**: One-click download of the complete audit log in CSV format (`/api/reports/export.csv`).

### 6. 🛡️ Password-Protected Appliance Administration
- **Admin Security**: Hardware actions are protected by administrator password.
- **System Controls**: Safe web-based Reboot, Shutdown, CUPS Service Restart, Cache & Temp Spool Purging, Rate Card Editor (B&W/Color pricing), and Admin Password Manager.

### 7. 📁 SANE Flatbed Scanner & Network Shares
- **Web Scanner (`/scan`)**: Scan documents to PDF, PNG, or JPEG at 75–600 DPI.
- **Samba File Sharing**: Scans auto-saved to `\\printserver.local\Public\Scans`.
- **Automated Daily Backups**: Daily 3:00 AM server snapshots stored in `\\printserver.local\Public\backups`.

### 8. 🛡️ Network Ad-Blocking & Remote VPN
- **AdGuard Home**: Running on port `3000` & `53` with balanced filter lists.
- **Tailscale Mesh VPN**: Remote printing and admin access from anywhere at `http://100.104.185.40`.

---

## 🚀 Installation & Deployment

```bash
# Clone the repository
git clone https://github.com/Sheesh0327/armbian-print-vpn-server.git
cd armbian-print-vpn-server

# Run the automated appliance setup
sudo chmod +x setup.sh
sudo ./setup.sh
```

---

## 🌐 Network Service Directory

| Service | Access URL | Purpose |
| :--- | :--- | :--- |
| **Print Appliance Portal** | [`http://printserver.local`](http://printserver.local) | Direct print, Canva studio, photocopier, PIN queue |
| **AdGuard Home** | [`http://printserver.local:3000`](http://printserver.local:3000) | Network ad-blocking dashboard |
| **Samba File Share** | `\\printserver.local\Public` | Shared network folder & scan storage |
| **CUPS Admin** | [`https://printserver.local:631`](https://printserver.local:631) | Raw hardware and queue administration |
| **Tailscale Remote VPN** | `http://100.104.185.40` | Secure remote access over cellular / WAN |

---

## 📜 License
Licensed under the [MIT License](LICENSE).
