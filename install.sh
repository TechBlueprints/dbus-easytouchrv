#!/bin/bash
#
# Remote installer for dbus-easytouchrv on Venus OS
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-easytouchrv/main/install.sh | bash
#

set -e

REPO_URL="https://github.com/TechBlueprints/dbus-easytouchrv.git"
INSTALL_DIR="/data/apps/dbus-easytouchrv"
SERVICE_NAME="dbus-easytouchrv"

echo "========================================"
echo "EasyTouch RV Thermostat Installer"
echo "========================================"
echo ""

# Check if running on Venus OS
if [ ! -d "/data/apps" ]; then
    echo "Error: /data/apps not found. This script must run on Venus OS."
    exit 1
fi

# Step 1: Ensure git is installed
echo "Step 1: Checking for git..."
if ! command -v git >/dev/null 2>&1; then
    echo "Git not found. Installing git..."
    if ! opkg install git; then
        echo "Error: Failed to install git."
        exit 1
    fi
    echo "Git installed successfully"
else
    echo "Git already installed"
fi
echo ""

# Step 2: Clone or update repository
echo "Step 2: Setting up repository..."
cd /data/apps

NEEDS_RESTART=false

if [ -d "$INSTALL_DIR" ]; then
    echo "Directory exists: $INSTALL_DIR"
    cd "$INSTALL_DIR"

    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true

    if [ -d .git ]; then
        CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
        if [ "$CURRENT_REMOTE" != "$REPO_URL" ]; then
            echo "Updating remote URL to $REPO_URL..."
            git remote set-url origin "$REPO_URL" 2>/dev/null || git remote add origin "$REPO_URL"
        fi

        echo "Fetching latest changes..."
        git fetch origin

        LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "none")
        REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "none")

        if [ "$LOCAL" != "$REMOTE" ]; then
            echo "Updates available. Resetting to latest..."
            git checkout main 2>/dev/null || git checkout -b main origin/main
            git reset --hard origin/main
            NEEDS_RESTART=true
            echo "Repository updated to latest"
        else
            echo "Already up to date"
        fi
    else
        echo "Not a git repository. Converting..."
        git init
        git remote add origin "$REPO_URL"
        git fetch origin
        git checkout -b main origin/main 2>/dev/null || git checkout main
        git reset --hard origin/main
        git branch --set-upstream-to=origin/main main 2>/dev/null || true
        NEEDS_RESTART=true
        echo "Converted to git repository and updated to latest"
    fi
else
    echo "Directory does not exist. Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    echo "Repository cloned"
fi
echo ""

# Step 2b: Converge the shared BCM checkout (fleet-wide BLE stack).
# Idempotent and forward-only: ff-only means a stale installer can never
# move the fleet backwards.  Its install.sh smoke-imports the stack and
# only then updates the /data/bcm/python3 shim the run script prefers.
echo "Step 2b: Shared bleak-connection-manager install..."
BCM_DIR=/data/bcm
if [ -d "$BCM_DIR/.git" ]; then
    git -C "$BCM_DIR" fetch -q origin && git -C "$BCM_DIR" merge -q --ff-only origin/main
else
    git clone -q https://github.com/TechBlueprints/bleak-connection-manager "$BCM_DIR"
fi
if ! "$BCM_DIR/install.sh"; then
    echo "Error: shared BCM install failed; see output above (rollback: git -C $BCM_DIR checkout <hash> && $BCM_DIR/install.sh)"
    exit 1
fi
echo ""

# Step 3: Initialize submodules (velib_python + BLE stack)
echo "Step 3: Setting up submodules..."
cd "$INSTALL_DIR"
if [ ! -f ext/velib_python/vedbus.py ]; then
    git submodule update --init --recursive 2>/dev/null || {
        echo "Submodule init failed, cloning velib_python directly..."
        mkdir -p ext
        if [ -d ext/velib_python ]; then
            rm -rf ext/velib_python
        fi
        git clone https://github.com/victronenergy/velib_python.git ext/velib_python
    }
    echo "Submodules ready"
else
    echo "Submodules already present"
fi
echo ""

# Step 4: Run enable script
echo "Step 4: Enabling service..."
bash "$INSTALL_DIR/enable.sh"
echo ""

# Step 5: Start or restart service
echo "Step 5: Starting service..."
if svstat "/service/$SERVICE_NAME" 2>/dev/null | grep -q ": up "; then
    if [ "$NEEDS_RESTART" = true ]; then
        echo "Restarting service to apply updates..."
        svc -t "/service/$SERVICE_NAME"
        sleep 2
        echo "Service restarted"
    else
        echo "Service already running, no updates needed"
    fi
else
    svc -u "/service/$SERVICE_NAME"
    sleep 2
    echo "Service started"
fi

echo ""
echo "========================================"
echo "Installation Complete!"
echo "========================================"
echo ""
echo "IMPORTANT: Configure your thermostats in config.ini"
echo "  cd $INSTALL_DIR"
echo "  python3 provision.py            # scan and generate config.ini"
echo "  # or: cp config.default.ini config.ini && vi config.ini"
echo ""
echo "Then restart the service:"
echo "  svc -t /service/$SERVICE_NAME"
echo ""
echo "Service status:"
svstat "/service/$SERVICE_NAME"
echo ""
echo "View logs:"
echo "  tail -n 30 /var/log/$SERVICE_NAME/current | tai64nlocal"
echo ""
echo "Service management:"
echo "  svc -u /service/$SERVICE_NAME  # Start"
echo "  svc -d /service/$SERVICE_NAME  # Stop"
echo "  svc -t /service/$SERVICE_NAME  # Restart"
echo ""
