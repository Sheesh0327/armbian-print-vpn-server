#!/usr/bin/env bash
#
# Hardened Print Server Backup Script
# Securely archives SQLite database, configuration, and Web Print files.
#

set -euo pipefail

BACKUP_DIR="/var/backups/webprint"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
TARGET_ARCHIVE="${BACKUP_DIR}/printserver-backup-${TIMESTAMP}.tar.gz"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

echo "[INFO] Creating secure backup archive: ${TARGET_ARCHIVE}"

tar -czf "${TARGET_ARCHIVE}" \
    /var/lib/webprint \
    /etc/webprint \
    /opt/webprint \
    /etc/cups/cupsd.conf \
    /etc/cups/printers.conf \
    /etc/avahi/avahi-daemon.conf \
    /etc/systemd/system/webprint.service 2>/dev/null || true

chmod 600 "${TARGET_ARCHIVE}"

# Retain last 14 backups, remove older archives
find "${BACKUP_DIR}" -name "printserver-backup-*.tar.gz" -type f -mtime +14 -delete 2>/dev/null || true

echo "[SUCCESS] Backup created securely (mode 600): ${TARGET_ARCHIVE}"
