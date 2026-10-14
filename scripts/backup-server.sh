#!/usr/bin/env bash
#
# Automated Home Server Backup Script
# Archives configuration, CUPS drivers, Samba, and Web Print configurations.
#

set -euo pipefail

BACKUP_DIR="/srv/shares/public/backups"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
TARGET_ARCHIVE="${BACKUP_DIR}/printserver-backup-${TIMESTAMP}.tar.gz"

mkdir -p "${BACKUP_DIR}"

echo "[INFO] Creating system backup archive: ${TARGET_ARCHIVE}"

tar -czf "${TARGET_ARCHIVE}" \
    /etc/cups \
    /etc/avahi \
    /etc/samba \
    /opt/webprint \
    /etc/systemd/system/webprint.service \
    /etc/sysctl.d/99-tailscale.conf 2>/dev/null || true

chmod 664 "${TARGET_ARCHIVE}"

# Retain last 14 backups, remove older archives
find "${BACKUP_DIR}" -name "printserver-backup-*.tar.gz" -type f -mtime +14 -delete 2>/dev/null || true

echo "[SUCCESS] Backup created successfully: ${TARGET_ARCHIVE}"
