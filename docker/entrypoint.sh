#!/bin/bash
# =============================================================================
# Photonarium Docker Entrypoint
#
# Handles:
#   - PUID/PGID user creation for NAS filesystem permissions
#   - First-run model copying from image to /config volume
#   - Default config file generation
#   - Application startup
# =============================================================================

set -e

# -----------------------------------------------------------------------------
# PUID/PGID handling (standard NAS container pattern)
# -----------------------------------------------------------------------------
PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo "Starting Photonarium..."
echo "  PUID: $PUID"
echo "  PGID: $PGID"

# Create group and user if they don't exist
groupadd -o -g "$PGID" photonarium 2>/dev/null || true
useradd -o -u "$PUID" -g "$PGID" -d /config -s /bin/bash photonarium 2>/dev/null || true

# -----------------------------------------------------------------------------
# First-run setup: copy models from image to /config volume
# -----------------------------------------------------------------------------
# Models are baked into the image during docker build:
#   - /defaults/ contains LAION aesthetic head and NIMA weights
#   - /root/.cache/huggingface/ contains OpenCLIP and BLIP models
#
# On first run, we copy them to /config so they persist across container
# updates. Uses cp -n (no-clobber) so user-replaced models aren't overwritten.

if [ -d "/defaults" ] && [ "$(ls -A /defaults 2>/dev/null)" ]; then
    echo "Copying ML models to /config (first run only)..."
    cp -rn /defaults/. /config/ 2>/dev/null || true
fi

# Copy HuggingFace cache (OpenCLIP, BLIP models) to /config/.cache/huggingface/
# The app runs as photonarium user with HOME=/config, so HF looks here.
if [ -d "/root/.cache/huggingface" ] && [ ! -d "/config/.cache/huggingface" ]; then
    echo "Copying HuggingFace models to /config/.cache/ (first run only)..."
    mkdir -p /config/.cache
    cp -r /root/.cache/huggingface /config/.cache/
fi

# -----------------------------------------------------------------------------
# Generate default config if not present
# -----------------------------------------------------------------------------
if [ ! -f /config/photonarium.yml ]; then
    echo "Creating default configuration..."
    cat > /config/photonarium.yml << 'EOF'
# Photonarium Configuration (Docker)
# See documentation for all available settings.

# Data directory (container path)
data_dir: /config

# Server binding
server_host: 0.0.0.0
server_port: 5000

# Headless mode: hides desktop-only features (folder picker, reveal in explorer)
headless: true

# Catalogue directory for imported photos (organised by date)
catalogue_dir: /catalogue

# Automatic rescan interval in minutes (0 = disabled)
# Recommended for NAS with sync apps that continuously add photos
scan_interval_minutes: 60
EOF
fi

# -----------------------------------------------------------------------------
# Fix ownership of config directory
# -----------------------------------------------------------------------------
chown -R "$PUID:$PGID" /config

# -----------------------------------------------------------------------------
# Start application as the configured user
# -----------------------------------------------------------------------------
echo "Starting Photonarium server..."
exec gosu "$PUID:$PGID" python /app/app/app.py \
    --config /config/photonarium.yml \
    --data-dir /config \
    --port "${PHOTONARIUM_PORT:-5000}" \
    "$@"
