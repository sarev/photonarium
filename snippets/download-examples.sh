#!/usr/bin/env bash
#
# Download example images from Lorem Picsum for tutorial/demo purposes.
#
# Lorem Picsum (https://picsum.photos) is a free image placeholder service
# that serves random photos from Unsplash. Images are free to use with no
# attribution required.
#
# Usage:
#   ./download-examples.sh              # Download 200 images to ../examples
#   ./download-examples.sh 50           # Download 50 images
#   ./download-examples.sh 50 300       # Download 50 images starting at index 300
#
# URL format:
#   https://picsum.photos/seed/{seed}/{width}/{height}
#
# The 'seed' parameter makes URLs deterministic - the same seed always returns
# the same image. We use "photonarium{N}" as seeds so the example set is
# reproducible. Without a seed, each request returns a random image.
#
# The service redirects to the actual image URL, so curl needs -L to follow.

set -e

# Configuration
COUNT=${1:-200}           # Number of images to download (default: 200)
START=${2:-1}             # Starting index (default: 1)
WIDTH=1920                # Image width in pixels
HEIGHT=1080               # Image height in pixels
SEED_PREFIX="photonarium"   # Seed prefix for reproducibility
DELAY=0.3                 # Delay between requests (be nice to the server)

# Output directory (relative to this script's location)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../examples"

# Create output directory if needed
mkdir -p "$OUTPUT_DIR"

echo "Downloading $COUNT images to $OUTPUT_DIR"
echo "Starting at index $START, resolution ${WIDTH}x${HEIGHT}"
echo ""

# Download images
END=$((START + COUNT - 1))
for i in $(seq $START $END); do
    FILENAME="photo_$(printf '%03d' $i).jpg"
    URL="https://picsum.photos/seed/${SEED_PREFIX}${i}/${WIDTH}/${HEIGHT}"

    curl -sL "$URL" -o "$OUTPUT_DIR/$FILENAME"
    echo "Downloaded $((i - START + 1))/$COUNT: $FILENAME"

    # Rate limiting - be respectful to the free service
    sleep $DELAY
done

echo ""
echo "Done! Downloaded $COUNT images to $OUTPUT_DIR"
echo "Total size: $(du -sh "$OUTPUT_DIR" | cut -f1)"
