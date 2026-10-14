# 🖨️ Turnkey Commercial Print Appliance & Server Gateway

Turn any low-cost ARM SBC or TV box (such as the **ZTE B860H** with Amlogic S905X, 1GB RAM) running **Armbian Linux** into a commercial-grade, driverless print kiosk, full-fledged Canva design workstation, and network server.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Platform: Armbian / Debian](https://img.shields.io/badge/Platform-Armbian%20%7C%20Debian-red.svg)
![Arch: ARM64](https://img.shields.io/badge/Arch-ARM64-green.svg)
![Memory Footprint: <400MB](https://img.shields.io/badge/RAM%20Idle-~380MB-brightgreen.svg)

---

## 🌟 Commercial Capabilities Overview

### 1. 🖨️ Permanent Advanced Web Print Portal (`Port 80`)
- **Direct Access**: Accessible at **`http://printserver.local`** or `http://10.0.0.95`.
- **Dynamic Multi-Printer Auto-Discovery**: Automatically queries connected USB or network IPP printers (`HP`, `Canon`, `Epson`, `Brother`, thermal label printers) with real-time status and default queue auto-selection.
- **Universal Document & Mobile Ingestion**:
  - **PDF & Office Suite**: Native headless conversion for `.pdf`, `.docx`, `.doc`, `.xlsx`, `.xls`, `.pptx`, `.ppt`, `.odt`, `.rtf`, `.csv`, `.txt`.
  - **Modern Smartphone Formats**: iPhone `.heic`/`.heif` and modern `.avif`, `.webp`, `.png`, `.jpg`, `.tiff`.
- **Permanent Advanced Controls Suite**:
  - Exact Paper Sizes (`A4`, `Letter`, `Legal`, `A5`, `A6`, `B5`, `4×6"`, `5×7"`, `8×10"`, `Envelope DL`).
  - Dynamic Physical Paper Preview matching physical dimensions with multi-page navigation (`‹` `1 / N` `›`).
  - Millimetric Margins Engine: `Printable Area`, `Borderless (0 mm)`, `Normal (12.7 mm)`, or `Custom mm` (Top, Bottom, Left, Right).
  - Media types (`Plain`, `Glossy`, `Matte`, `Brochure`) & Print Quality (`Normal`, `Best`, `Photo`).
  - Resolution / DPI (`150`, `300`, `600`, `1200 DPI`).
  - Duplex (`Single-Sided`, `Long-Edge`, `Short-Edge`) & Orientation (`Auto`, `Portrait`, `Landscape`).
  - Scaling (`Fit to Printable Area`, `Fill / Crop Page`) & Page Range (`1-3, 5`).
- **Pre-Flight Accounting & Cost Estimator**:
  - Real-time page counting via `pdfinfo`.
  - Live cost calculation configured in Philippine Pesos (`₱`) (default: ₱2.00/page B&W, ₱5.00/page Color).

### 2. 🎨 Full-Fledged Browser-Offloaded Canva Clone Studio
- **100% Client-Side Architecture (0 MB Added to Server RAM)**:
  - Powered by Fabric.js, client-side HTML5 Canvas, jsPDF, and QRCode.js.
  - All graphic compositing, layer dragging, and typography rendering execute on the client's GPU and RAM.
- **Canva Workstation Features**:
  - **Icon Rail & Sliding Drawer**: Quick switching between `Design / Templates`, `Shapes / Elements`, `Text / Typography`, `Uploads`, `QR Code Generator`, and `Freehand Pen Draw`.
  - **1-Click Pre-Built Canva Templates**:
    - Promotional Sale & Clearance Flyer (bold header, 50% badge, offer box, address footer).
    - Corporate Memorandum & Official Notice (letterhead, date, subject line, body text, signature block).
    - Guest Wi-Fi Sign & QR (network credentials with embedded scan-to-connect QR code).
    - Certificate of Recognition & Achievement (ornate gold borders, award text, ribbon seal).
    - 2×2 ID Photo Sheet (automatically tiles 6x framed passport photos with cut guides on an A4 sheet).
  - **Vector Shapes & Enterprise Stamps**: Rectangles, circles, stars, arrows, triangles, and 1-click `APPROVED`, `CONFIDENTIAL`, `PAID`, `URGENT`, `DRAFT`, and `Authorized Signature Line` stamps.
  - **Contextual Dynamic Toolbar**: Top toolbar automatically adapts based on the active selection (font family, font size, text color, bold/italic for text; fill/stroke for shapes; grayscale, sepia, contrast, invert filters for images).
  - **Direct 300 DPI Spooling**: Browser renders a crisp 300 DPI canvas blob client-side and submits directly to `/api/print` in one tap. Also supports downloading as pristine PDF or PNG.

### 3. 🔒 Confidential Pull-Printing (PIN-Release Queue)
- **Secure Staging Spool**: Jobs marked confidential are encrypted and held in `/var/spool/webprint/pinned/`. The printer stays silent until unlocked.
- **PIN Unlock**: User walks up to the printer, enters their 4-digit PIN in the portal, and releases the job to the tray.
- **Auto-Purge Security**: Unclaimed PIN jobs automatically expire and delete after 24 hours.

### 4. 📊 Print Accounting & Audit Ledger
- **Complete Job Audit Trail**: Tracks timestamp, document title, printer, page count, copies, color mode, cost (`₱`), and client IP.
- **Revenue & Volume Analytics**: Real-time summary cards for total jobs, total pages, and total billed cost.
- **Exportable Reports**: One-click download of the complete audit log in CSV format (`/api/reports/export.csv`).

### 5. 🛡️ Password-Protected Appliance Administration
- **Admin Security**: Hardware actions are protected by administrator password (default: `admin`).
- **System Controls**: Safe web-based Reboot, Shutdown, CUPS Service Restart, Cache & Temp Spool Purging, Rate Card Editor (B&W/Color pricing), and Admin Password Manager.

### 6. 📁 SANE Flatbed Scanner & Network Shares
- **Web Scanner (`/scan`)**: Scan documents to PDF, PNG, or JPEG at 75–600 DPI.
- **Samba File Sharing**: Scans auto-saved to `\\printserver.local\Public\Scans`.
- **Automated Daily Backups**: Daily 3:00 AM server snapshots stored in `\\printserver.local\Public\backups`.

### 7. 🛡️ Network Ad-Blocking & Remote VPN
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
| **Print Appliance Portal** | [`http://printserver.local`](http://printserver.local) | Direct print, Canva clone studio, PIN queue, scanner |
| **AdGuard Home** | [`http://printserver.local:3000`](http://printserver.local:3000) | Network ad-blocking dashboard |
| **Samba File Share** | `\\printserver.local\Public` | Shared network folder & scan storage |
| **CUPS Admin** | [`https://printserver.local:631`](https://printserver.local:631) | Raw hardware and queue administration |
| **Tailscale Remote VPN** | `http://100.104.185.40` | Secure remote access over cellular / WAN |

---

## 📜 License
Licensed under the [MIT License](LICENSE).
