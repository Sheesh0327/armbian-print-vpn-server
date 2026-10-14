# 🖨️ Armbian Print Server & Tailscale VPN Gateway

Turn any low-cost ARM SBC or TV box (such as the **ZTE B860H** with Amlogic S905X) running **Armbian Linux** into a full-fledged home print server and secure remote VPN bridge.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Platform: Armbian / Debian](https://img.shields.io/badge/Platform-Armbian%20%7C%20Debian-red.svg)
![Arch: ARM64 / ARMv7](https://img.shields.io/badge/Arch-ARM64%20%2F%20ARMv7-green.svg)

---

## 🌟 Features

- **🌐 Zero-Install Web Print Portal (`:8080`)**:
  - Drag-and-drop file upload for **PDF, DOCX, TXT, PNG, JPG**.
  - Customizable copies and duplex (two-sided) printing controls.
  - No drivers or passwords needed for family members or guests on your Wi-Fi.
  - Ultra-lightweight: written in Python Flask, uses less than **3 MB of RAM**!
- **📱 Native System Discovery (AirPrint & Mopria)**:
  - Powered by **CUPS** and **Avahi DNS-SD**.
  - Apple devices (iPhone, iPad, Mac) discover the printer automatically via **AirPrint**.
  - Android phones discover the printer automatically via the **Default Print Service (Mopria)**.
  - Windows & Linux PCs discover the printer automatically via network printer discovery.
- **🛡️ Secure Remote VPN Bridge (Tailscale Subnet Router)**:
  - Access your home network from anywhere in the world without port forwarding or a static public IP.
  - Print documents remotely from cellular data / cafe Wi-Fi to have them ready when you get home.
  - Acts as an optional encrypted **Exit Node** to secure public Wi-Fi browsing.

---

## 📐 Architecture

```
                    +------------------------------------+
                    |        Remote Client (5G/Cafe)     |
                    +-----------------+------------------+
                                      |
                     [WireGuard / Tailscale Tunnel]
                                      |
                                      v
  ============================= HOME LAN =============================
  |                                                                  |
  |  +------------------------------------------------------------+  |
  |  |               ZTE B860H (Armbian aarch64)                  |  |
  |  |                                                            |  |
  |  |  +-----------------------+     +------------------------+  |  |
  |  |  | Web Portal (:8080)    |     | Tailscale Subnet Route |  |  |
  |  |  +-----------+-----------+     +-----------+------------+  |  |
  |  |              |                             |               |  |
  |  |  +-----------v-----------+                 |               |  |
  |  |  | CUPS Spooler (:631)   |                 |               |  |
  |  |  | Avahi mDNS (AirPrint) |                 |               |  |
  |  |  +-----------+-----------+                 |               |  |
  |  +--------------|-----------------------------|---------------+  |
  |                 | (USB)                       |                  |
  |                 v                             v                  |
  |       [USB Printer]               [Other LAN Devices]            |
  |    (HP / Epson / etc.)          (Router, NAS, IP Cameras)        |
  ====================================================================
```

---

## 🚀 Quick Start (Automated Installation)

Clone this repository on your Armbian box and run the installer:

```bash
git clone https://github.com/Sheesh0327/armbian-print-vpn-server.git
cd armbian-print-vpn-server
sudo chmod +x setup.sh
sudo ./setup.sh
```

The script will:
1. Install all dependencies (`cups`, `cups-filters`, `printer-driver-gutenprint`, `hplip`, `avahi-daemon`, `python3-flask`).
2. Configure CUPS for LAN sharing and Avahi for AirPrint / Mopria.
3. Install and start the `webprint` systemd service on port `8080`.
4. Enable kernel IP forwarding (`net.ipv4.ip_forward = 1`).
5. Install and prepare Tailscale.

---

## 🛠️ Configuration Steps

### 1. Connecting Your Printer
1. Plug your printer's USB cable into any USB port on your SBC.
2. Verify detection:
   ```bash
   lsusb
   ```
3. Open the CUPS Admin panel in your browser:
   `https://<sbc-ip>:631/admin`
4. Click **Add Printer** (log in with your `root` or `sudo` user), select your USB printer, choose the recommended driver (e.g. `hpcups` or `gutenprint`), check **Share this printer**, and click Save.

*Or configure directly via CLI:*
```bash
# Example for HP Smart Tank 500 / 520 series:
lpadmin -p "HP_Smart_Tank" -v "usb://HP/Smart%20Tank%20520_540%20series?serial=TH56G6T0T0&interface=1" -m "drv:///hpcups.drv/hp-smart_tank_500_series.ppd" -E -o printer-is-shared=true
lpadmin -d "HP_Smart_Tank"
```

---

### 2. Activating Tailscale Subnet Routing

1. Authenticate Tailscale with your LAN subnet route:
   ```bash
   sudo tailscale up --advertise-routes=10.0.0.0/24 --advertise-exit-node
   ```
   *(Replace `10.0.0.0/24` with your home router's subnet if different).*

2. Open the [Tailscale Admin Console](https://login.tailscale.com/admin/machines).
3. Click `...` next to your device $\rightarrow$ **Edit route settings...**.
4. Approve the advertised subnet route.

---

## 🖥️ Usage

### 1. Web Print Portal
Navigate to `http://<sbc-ip>:8080` from any phone, tablet, or PC on your network:
- Drag-and-drop your document or picture.
- Select target printer, number of copies, and single/duplex mode.
- Click **Send to Printer**.

### 2. Native AirPrint (iOS / macOS)
- Open any photo, document, or webpage.
- Tap **Share $\rightarrow$ Print**.
- Select the automatically discovered printer.

### 3. Remote Printing & LAN Access
- Turn on Tailscale on your mobile device or laptop.
- Open `http://<sbc-tailscale-ip>:8080` to print remotely.
- Access any other local device on your LAN as if connected to your home Wi-Fi.

---

## 📂 Project Structure

```
armbian-print-vpn-server/
├── README.md                 # Project documentation
├── LICENSE                   # MIT License
├── setup.sh                  # One-command installer script
├── .gitignore                # Git ignore rules
├── webprint/
│   └── app.py                # Python Flask web print portal
├── systemd/
│   └── webprint.service      # Systemd service unit file
└── sysctl/
    └── 99-tailscale.conf     # Kernel IP forwarding config
```

---

## 📜 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
