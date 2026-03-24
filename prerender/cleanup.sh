#!/bin/sh
# Remove cached listing pages older than 1 hour.
# Post pages (/@author/permlink) are cached forever.
# Listing pages are the root index and author pages (/@author with no permlink).
#
# Run via cron every hour inside the prerender container.

CACHE_DIR="${CACHE_ROOT_DIR:-/cache}"

# Skip if cache dir doesn't exist yet
[ -d "$CACHE_DIR" ] || exit 0

# Remove files modified more than 60 minutes ago that are NOT post pages.
# Post pages have a permlink segment: .../@author/permlink/
# Listing pages are either the root or .../@author/ with no deeper content.
#
# Strategy: find all cached HTML files, check if they look like post pages
# (have at least 2 path segments after the domain that start with @),
# and delete the rest if older than 1 hour.

find "$CACHE_DIR" -type f -name "*.html" -mmin +60 | while read -r file; do
  # Extract path after the domain directory
  # Cache structure: /cache/<scheme>/<host>/<path>/index.html
  rel="${file#"$CACHE_DIR"/}"

  # Count path segments after scheme/host (first 2 segments)
  # A post page has /@author/permlink = 2+ segments after host
  depth=$(echo "$rel" | tr '/' '\n' | tail -n +3 | grep -c .)

  # Post pages have depth >= 3 (/@author/permlink/index.html = @author + permlink + index.html)
  # Listing pages have depth <= 2 (/ = index.html, /@author/ = @author + index.html)
  if [ "$depth" -le 2 ]; then
    rm -f "$file"
  fi
done

# Clean up empty directories
find "$CACHE_DIR" -type d -empty -delete 2>/dev/null
