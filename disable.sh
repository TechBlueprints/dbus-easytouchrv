#!/bin/bash
#
# Disable script for dbus-easytouchrv
# Cleanly stops and removes the service
#

INSTALL_DIR="/data/apps/dbus-easytouchrv"
SERVICE_NAME="dbus-easytouchrv"

# Stop the service
svc -d "/service/$SERVICE_NAME" 2>/dev/null || true
sleep 2

# Remove service symlink
rm -rf "/service/$SERVICE_NAME" 2>/dev/null || true

# Kill any remaining processes
pkill -f "supervise $SERVICE_NAME" 2>/dev/null || true
pkill -f "python.*dbus-easytouchrv" 2>/dev/null || true
pkill -f "python.*easytouch_device" 2>/dev/null || true

# Remove enable script from rc.local
sed -i "/.*dbus-easytouchrv.*/d" /data/rc.local 2>/dev/null || true

echo "$SERVICE_NAME disabled"
