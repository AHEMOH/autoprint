#!/bin/bash
# AutoPrint entrypoint - configures CUPS and starts Python service
# Canon GX2050 MegaTank: IPP Everywhere, no vendor driver required
set -e

echo "=== AutoPrint Container Startup ==="

# ---------------------------------------------------------------------------
# Configure CUPS: allow external access
# ---------------------------------------------------------------------------
CUPS_CONF=/etc/cups/cupsd.conf

# Listen on all interfaces (not only localhost)
sed -i 's/^Listen localhost:631/Listen *:631/' "$CUPS_CONF"

# Set ServerAlias for remote access if missing
if ! grep -q "ServerAlias \*" "$CUPS_CONF"; then
    sed -i '/^ServerName/a ServerAlias *' "$CUPS_CONF"
fi

# Allow LAN access
python3 - << 'PYEOF'
import re, sys

with open('/etc/cups/cupsd.conf', 'r') as f:
    content = f.read()

# <Location /> Block -> Allow all
content = re.sub(
    r'(<Location\s+/\s*>)(.*?)(</Location>)',
    r'\1\n  Order allow,deny\n  Allow all\n\3',
    content, flags=re.DOTALL
)
# <Location /admin> block -> allow all (LAN only)
content = re.sub(
    r'(<Location\s+/admin\s*>)(.*?)(</Location>)',
    r'\1\n  Order allow,deny\n  Allow all\n\3',
    content, flags=re.DOTALL
)
content = re.sub(
    r'(<Location\s+/admin/conf\s*>)(.*?)(</Location>)',
    r'\1\n  Order allow,deny\n  Allow all\n\3',
    content, flags=re.DOTALL
)

with open('/etc/cups/cupsd.conf', 'w') as f:
    f.write(content)

print("CUPS config updated.")
PYEOF

# ---------------------------------------------------------------------------
# Start CUPS daemon
# ---------------------------------------------------------------------------
echo "Starting CUPS daemon..."
/usr/sbin/cupsd

# Wait until CUPS socket is available
for i in $(seq 1 20); do
    if [ -S /run/cups/cups.sock ] || lpstat -H > /dev/null 2>&1; then
        echo "CUPS is ready (${i}s)."
        break
    fi
    echo "  Waiting for CUPS... ($i/20)"
    sleep 1
done

# ---------------------------------------------------------------------------
# Add network printer (IPP Everywhere / driverless)
# Canon GX2050 supports IPP Everywhere natively - no vendor driver needed
# ---------------------------------------------------------------------------
if [ -n "$PRINTER_URI" ]; then
    PNAME="${PRINTER_NAME:-CanonGX2050}"
    echo "Configuring printer: '$PNAME' -> $PRINTER_URI"

    # Connectivity check via ipptool (direct IPP, no queue needed)
    echo "Testing printer connection via IPP..."
    if ipptool -T 8 -q "$PRINTER_URI" get-printer-attributes.test 2>&1; then
        echo "Printer is reachable via IPP."
    else
        echo ""
        echo "WARNING: Printer is NOT reachable: $PRINTER_URI"
        echo "   Common causes for Canon GX2050:"
        echo "     1. DNS: .home.arpa is not resolvable inside container"
        echo "        Fix: set router IP under 'dns:' in docker-compose.yml"
        echo "        or use 'extra_hosts' with the printer IP."
        echo "     2. Printer is sleeping/offline"
        echo "     3. Wrong URI path - example: ipp://my-printer.local/ipp/print"
        echo "   -> Container will keep running and retry on next schedule."
        echo ""
    fi

    # Remove existing queue with the same name if present
    lpadmin -x "$PNAME" 2>/dev/null || true

    # Register printer in CUPS using IPP Everywhere (driverless)
    if lpadmin -p "$PNAME" -E -v "$PRINTER_URI" -m everywhere -D "Canon GX2050 MegaTank" 2>&1; then
        # Set queue defaults for Canon GX2050
        lpoptions -p "$PNAME" \
            -o media=A4 \
            -o print-color-mode=color \
            -o print-quality=5 \
            -o ColorModel=RGB
        lpoptions -d "$PNAME"
        echo "Printer '$PNAME' configured (IPP Everywhere, A4, color, quality 5)."
    else
        echo ""
        echo "WARNING: CUPS queue registration failed."
        echo "   IPP check worked, but CUPS could not create the queue."
        echo "   Alternative: add printer manually in CUPS web UI:"
        echo "     http://<host-ip>:631 -> Administration -> Add Printer"
        echo "     Then set PRINTER_NAME in docker-compose.yml accordingly."
        echo ""
    fi
else
    echo ""
    echo "WARNING: PRINTER_URI is not set."
    echo "   Please set it in docker-compose.yml/.env:"
    echo "     PRINTER_URI=ipp://my-printer.local/ipp/print"
    echo ""
fi

# ---------------------------------------------------------------------------
# Start AutoPrint Python service (PID 1)
# ---------------------------------------------------------------------------
echo "Starting AutoPrint web service (Gunicorn) on :8080 ..."
exec gunicorn \
    --bind 0.0.0.0:8080 \
    --workers "${GUNICORN_WORKERS:-1}" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-60}" \
    autoprint:app
