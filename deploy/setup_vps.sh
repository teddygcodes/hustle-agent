#!/bin/bash
# One-shot Ubuntu VPS setup for the hustle agent
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo ./setup_vps.sh"
    exit 1
fi

echo "=== Hustle Agent VPS Setup ==="

# System deps
echo "Installing system dependencies..."
apt update && apt install -y python3 python3-pip python3-venv git

# Project directory
INSTALL_DIR="/opt/hustle-agent"
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Copying project to $INSTALL_DIR..."
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
    cp -r "$PROJECT_DIR" "$INSTALL_DIR"
fi

# Python deps
echo "Installing Python dependencies..."
cd "$INSTALL_DIR"
pip3 install -r requirements.txt

# API key
echo ""
read -p "Enter your ANTHROPIC_API_KEY: " API_KEY
if [ -n "$API_KEY" ]; then
    # Write to service file
    sed -i "s|Environment=ANTHROPIC_API_KEY=your_key_here|Environment=ANTHROPIC_API_KEY=$API_KEY|" \
        "$INSTALL_DIR/deploy/hustle-agent.service"
    echo "API key configured."
fi

# Create required directories
mkdir -p "$INSTALL_DIR/logs" "$INSTALL_DIR/state" "$INSTALL_DIR/tools" "$INSTALL_DIR/output"

# Install as service
echo "Installing systemd service..."
bash "$INSTALL_DIR/deploy/install.sh"

echo ""
echo "=== Setup complete! ==="
echo "Agent is running. Check: systemctl status hustle-agent"
echo "Send a message: cd $INSTALL_DIR && python3 agent/engine.py send -m 'Hello!'"
echo "Check health:   cd $INSTALL_DIR && python3 agent/engine.py health"
