#!/bin/sh
# Remove cached files older than 1 hour.
# Post pages are long-lived but still expire — bots re-crawl periodically.
#
# Run via entrypoint loop every hour inside the prerender container.

CACHE_DIR="${CACHE_ROOT_DIR:-/cache}"

[ -d "$CACHE_DIR" ] || exit 0

# Remove HTML files not modified in the last 60 minutes
find "$CACHE_DIR" -type f -name "*.html" -mmin +60 -delete

# Clean up empty directories
find "$CACHE_DIR" -type d -empty -delete 2>/dev/null
