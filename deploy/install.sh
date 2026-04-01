#!/bin/bash
# Install hustle agent as a systemd service
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo ./install.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Create service user if needed
if ! id -u hustle >/dev/null 2>&1; then
    useradd -r -s /bin/false -d /opt/hustle-agent hustle
    echo "Created 'hustle' service user."
fi

# Copy service file
cp "$SCRIPT_DIR/hustle-agent.service" /etc/systemd/system/
echo "Copied service file."

# Ensure ownership
chown -R hustle:hustle /opt/hustle-agent

# Enable and start
systemctl daemon-reload
systemctl enable hustle-agent
systemctl start hustle-agent

echo "Hustle agent installed and started."
echo "Check status: systemctl status hustle-agent"
echo "View logs:    journalctl -u hustle-agent -f"
echo ""
echo "IMPORTANT: Edit /etc/systemd/system/hustle-agent.service"
echo "and set your ANTHROPIC_API_KEY before restarting."
