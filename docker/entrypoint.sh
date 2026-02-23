#!/bin/bash
# =============================================================================
# Photonarium Docker Entrypoint
#
# Handles:
#   - PUID/PGID user creation for NAS filesystem permissions
#   - First-run setup (small model weights + default config)
#   - Application startup with model paths pointing to the image
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
# First-run setup: copy small model weights to /config
# -----------------------------------------------------------------------------
# Only the LAION aesthetic head (~2KB) and NIMA weights (~9MB) live in /config
# because the app loads them from data_dir. These are tiny and copy instantly.
#
# The large models (HuggingFace ~2.5GB, FaceNet ~107MB) stay in the image
# and are accessed via HF_HOME and TORCH_HOME environment variables below.
# This avoids duplicating ~2.6GB to the config volume on every new container.

if [ -d "/defaults" ] && [ "$(ls -A /defaults 2>/dev/null)" ]; then
    echo "Copying model weights to /config..."
    cp -rn /defaults/. /config/ 2>/dev/null || true
fi

# -----------------------------------------------------------------------------
# Point model libraries at the in-image caches (read-only, no copying needed)
# -----------------------------------------------------------------------------
export HF_HOME=/root/.cache/huggingface
export TORCH_HOME=/root/.cache/torch

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
