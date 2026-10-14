#!/usr/bin/env bash
#
# Armbian Print & VPN Bridge Server Installer
# Suitable for ZTE B860H and other Amlogic/Allwinner/Rockchip/Raspberry Pi boards.
#

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}=====================================================${NC}"
echo -e "${BLUE}   Armbian Home Print Server & Tailscale VPN Setup   ${NC}"
echo -e "${BLUE}=====================================================${NC}"

if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}[ERROR] This script must be run as root.${NC}" 
   exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "\n${YELLOW}[1/5] Updating repositories & installing packages...${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    cups \
    cups-filters \
    cups-client \
    cups-browsed \
    printer-driver-gutenprint \
    hplip \
    avahi-daemon \
    avahi-utils \
    python3-flask \
    python3-werkzeug \
    curl \
    usbutils

echo -e "\n${YELLOW}[2/5] Configuring CUPS and Avahi mDNS sharing...${NC}"
usermod -aG lpadmin root || true
cupsctl --remote-admin --remote-any --share-printers
systemctl restart cups
systemctl enable --now cups
systemctl restart avahi-daemon
systemctl enable --now avahi-daemon
systemctl enable --now cups-browsed

echo -e "\n${YELLOW}[3/5] Setting up Web Print Portal...${NC}"
mkdir -p /opt/webprint
cp "${SCRIPT_DIR}/webprint/app.py" /opt/webprint/app.py
chmod 755 /opt/webprint/app.py

cp "${SCRIPT_DIR}/systemd/webprint.service" /etc/systemd/system/webprint.service
systemctl daemon-reload
systemctl enable --now webprint.service

echo -e "\n${YELLOW}[4/5] Enabling IP packet forwarding for VPN subnet routing...${NC}"
cp "${SCRIPT_DIR}/sysctl/99-tailscale.conf" /etc/sysctl.d/99-tailscale.conf
sysctl -p /etc/sysctl.d/99-tailscale.conf

echo -e "\n${YELLOW}[5/5] Installing Tailscale...${NC}"
if ! command -v tailscale &>/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi

LAN_SUBNET="$(ip -o -f inet route show default 2>/dev/null | awk '{print $3}' | sed -E 's/\.[0-9]+$/.0\/24/')"
if [[ -z "$LAN_SUBNET" ]]; then
    LAN_SUBNET="192.168.1.0/24"
fi

echo -e "\n${GREEN}=====================================================${NC}"
echo -e "${GREEN}   Setup Complete!   ${NC}"
echo -e "${GREEN}=====================================================${NC}"
echo -e "Web Print Portal:    http://$(hostname -I | awk '{print $1}'):8080"
echo -e "CUPS Admin Panel:    https://$(hostname -I | awk '{print $1}'):631"
echo -e "Detected LAN Subnet: ${LAN_SUBNET}"
echo ""
echo -e "To authenticate Tailscale with subnet routing, run:"
echo -e "${BLUE}tailscale up --advertise-routes=${LAN_SUBNET} --advertise-exit-node${NC}"
echo ""
echo -e "To verify connected USB printer:"
echo -e "${BLUE}lsusb && lpstat -p -d${NC}"
