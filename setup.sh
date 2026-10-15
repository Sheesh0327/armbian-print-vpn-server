#!/usr/bin/env bash
#
# Hardened Turnkey Print Appliance & Gateway Installer
# Compatible with Armbian, Debian, Ubuntu on ARM/x86 SBCs (e.g. ZTE B860H).
#

set -euo pipefail

# ANSI Color Codes
CLR_RED='\033[0;31m'
CLR_GRN='\033[0;32m'
CLR_BLU='\033[0;34m'
CLR_YLW='\033[1;33m'
CLR_RST='\033[0m'

echo -e "${CLR_BLU}=====================================================${CLR_RST}"
echo -e "${CLR_BLU}    Hardened Armbian Web Print Appliance Setup       ${CLR_RST}"
echo -e "${CLR_BLU}=====================================================${CLR_RST}"

if [[ "${EUID}" -ne 0 ]]; then
   echo -e "${CLR_RED}[ERROR] This setup script must be executed as root.${CLR_RST}" 
   exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "\n${CLR_YLW}[1/7] Updating package index and installing dependencies...${CLR_RST}"
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
    python3-cryptography \
    gunicorn \
    imagemagick \
    libreoffice-writer-nogui \
    libreoffice-calc-nogui \
    poppler-utils \
    curl \
    usbutils \
    openssl

echo -e "\n${CLR_YLW}[2/7] Creating unprivileged webprint system user & directories...${CLR_RST}"
if ! id -u webprint &>/dev/null; then
    useradd -r -s /usr/sbin/nologin -d /var/lib/webprint -M -G lp webprint
else
    usermod -aG lp webprint
fi

mkdir -p /var/lib/webprint
mkdir -p /var/spool/webprint/pinned
mkdir -p /var/spool/webprint/held
mkdir -p /var/backups/webprint
mkdir -p /etc/webprint
mkdir -p /opt/webprint/scripts
mkdir -p /opt/webprint/static

chown -R webprint:lp /var/lib/webprint /var/spool/webprint
chmod 750 /var/lib/webprint /var/spool/webprint
chmod 700 /var/spool/webprint/pinned /var/spool/webprint/held
chmod 700 /var/backups/webprint
chown root:webprint /etc/webprint
chmod 750 /etc/webprint

echo -e "\n${CLR_YLW}[3/7] Generating security keys and administrator credentials...${CLR_RST}"
# Server secret key for PIN HMAC and AES-GCM
if [[ ! -f /var/lib/webprint/secret.key ]]; then
    openssl rand 32 > /var/lib/webprint/secret.key
    chown webprint:lp /var/lib/webprint/secret.key
    chmod 600 /var/lib/webprint/secret.key
fi

# Router API key
if [[ ! -f /etc/webprint/router.key ]]; then
    openssl rand -hex 32 > /etc/webprint/router.key
    chown root:webprint /etc/webprint/router.key
    chmod 640 /etc/webprint/router.key
fi
cp /etc/webprint/router.key /var/lib/webprint/router.key
chown webprint:lp /var/lib/webprint/router.key
chmod 600 /var/lib/webprint/router.key

# Admin password initialization (random, no default)
ADMIN_PASSWORD=""
if [[ ! -f /var/lib/webprint/webprint.db ]]; then
    ADMIN_PASSWORD="$(openssl rand -base64 12 | tr -dc 'a-zA-Z0-9' | head -c 16)"
    python3 -c "
import sqlite3
from werkzeug.security import generate_password_hash
db_path = '/var/lib/webprint/webprint.db'
h = generate_password_hash('${ADMIN_PASSWORD}', method='scrypt')
conn = sqlite3.connect(db_path)
with conn:
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (\'admin_hash\', ?)', (h,))
"
    chown webprint:lp /var/lib/webprint/webprint.db
    chmod 640 /var/lib/webprint/webprint.db
fi

echo -e "\n${CLR_YLW}[4/7] Applying hardened CUPS & Avahi configuration...${CLR_RST}"
usermod -aG lpadmin root || true
# Share printers without exposing remote admin
cupsctl --share-printers

# Restrict /admin to localhost and 10.0.0.0/24 in cupsd.conf
if grep -q "<Location /admin>" /etc/cups/cupsd.conf; then
    sed -i '/<Location \/admin>/,/<\/Location>/c\<Location /admin>\n  AuthType Default\n  Require user @SYSTEM\n  Order allow,deny\n  Allow from 127.0.0.1\n  Allow from ::1\n  Allow from 10.0.0.0/24\n</Location>' /etc/cups/cupsd.conf
fi
if grep -q "<Location /admin/conf>" /etc/cups/cupsd.conf; then
    sed -i '/<Location \/admin\/conf>/,/<\/Location>/c\<Location /admin/conf>\n  AuthType Default\n  Require user @SYSTEM\n  Order allow,deny\n  Allow from 127.0.0.1\n  Allow from ::1\n  Allow from 10.0.0.0/24\n</Location>' /etc/cups/cupsd.conf
fi
if grep -q "<Location /admin/log>" /etc/cups/cupsd.conf; then
    sed -i '/<Location \/admin\/log>/,/<\/Location>/c\<Location /admin/log>\n  AuthType Default\n  Require user @SYSTEM\n  Order allow,deny\n  Allow from 127.0.0.1\n  Allow from ::1\n  Allow from 10.0.0.0/24\n</Location>' /etc/cups/cupsd.conf
fi

systemctl restart cups
systemctl enable --now cups
systemctl restart avahi-daemon
systemctl enable --now avahi-daemon
systemctl enable --now cups-browsed

echo -e "\n${CLR_YLW}[5/7] Applying ImageMagick security policy...${CLR_RST}"
for im_dir in /etc/ImageMagick-7 /etc/ImageMagick-6; do
    if [[ -d "${im_dir}" && -f "${SCRIPT_DIR}/etc/imagemagick/policy.xml" ]]; then
        cp "${SCRIPT_DIR}/etc/imagemagick/policy.xml" "${im_dir}/policy.xml"
        chmod 644 "${im_dir}/policy.xml"
    fi
done

echo -e "\n${CLR_YLW}[6/7] Deploying Web Print application and backup timer...${CLR_RST}"
cp "${SCRIPT_DIR}/webprint/app.py" /opt/webprint/app.py
chmod 755 /opt/webprint/app.py
if [[ -d "${SCRIPT_DIR}/webprint/static" ]]; then
    cp -r "${SCRIPT_DIR}/webprint/static/." /opt/webprint/static/
fi
chown -R root:webprint /opt/webprint

cp "${SCRIPT_DIR}/scripts/backup-server.sh" /opt/webprint/scripts/backup-server.sh
chmod 750 /opt/webprint/scripts/backup-server.sh
chown root:root /opt/webprint/scripts/backup-server.sh

cp "${SCRIPT_DIR}/systemd/webprint.service" /etc/systemd/system/webprint.service
cp "${SCRIPT_DIR}/systemd/backup-webprint.service" /etc/systemd/system/backup-webprint.service
cp "${SCRIPT_DIR}/systemd/backup-webprint.timer" /etc/systemd/system/backup-webprint.timer

systemctl daemon-reload
systemctl enable webprint.service
systemctl restart webprint.service
systemctl enable --now backup-webprint.timer

echo -e "\n${CLR_YLW}[7/7] Tailscale status & networking...${CLR_RST}"
if command -v tailscale &>/dev/null; then
    echo -e "${CLR_GRN}[INFO] Tailscale detected.${CLR_RST}"
else
    echo -e "[INFO] Tailscale is optional and not installed by default."
fi

PRIMARY_IP="$(hostname -I | awk '{print $1}')"

echo -e "\n${CLR_GRN}=====================================================${CLR_RST}"
echo -e "${CLR_GRN}    Hardened Installation Complete                   ${CLR_RST}"
echo -e "${CLR_GRN}=====================================================${CLR_RST}"
echo -e "Web Print Portal:    http://${PRIMARY_IP}:80"
echo -e "CUPS IPP Printing:   http://${PRIMARY_IP}:631/printers/<printer_name>"
echo -e "CUPS Admin Panel:    https://${PRIMARY_IP}:631 (Local 10.0.0.0/24 subnet only)"
echo -e "Router Secret Key:   /etc/webprint/router.key"

if [[ -n "${ADMIN_PASSWORD}" ]]; then
    echo -e "\n${CLR_YLW}-----------------------------------------------------${CLR_RST}"
    echo -e "${CLR_YLW}[CRITICAL] GENERATED ADMINISTRATOR PASSWORD:${CLR_RST}"
    echo -e "Password: ${CLR_GRN}${ADMIN_PASSWORD}${CLR_RST}"
    echo -e "${CLR_YLW}Store this password in a secure password manager.${CLR_RST}"
    echo -e "${CLR_YLW}It is stored only as a salted scrypt hash in webprint.db${CLR_RST}"
    echo -e "${CLR_YLW}and will NEVER be displayed again.${CLR_RST}"
    echo -e "${CLR_YLW}-----------------------------------------------------${CLR_RST}"
fi

echo ""
echo -e "To verify connected USB printers:"
echo -e "${CLR_BLU}lsusb && lpstat -p -d${CLR_RST}"
echo ""
echo -e "If using Tailscale, authenticate securely without exit nodes:"
echo -e "${CLR_BLU}tailscale up --advertise-routes=10.0.0.95/32${CLR_RST}"
echo -e "${CLR_BLU}Ensure Tailscale ACLs restrict access strictly to ports 80 and 631.${CLR_RST}"
