#!/bin/sh
# Remove cached index pages older than 1 hour.
# Post pages (/@author/permlink) stay cached indefinitely.
#
# Run via entrypoint loop every hour inside the prerender container.

CACHE_DIR="${CACHE_ROOT_DIR:-/cache}"

[ -d "$CACHE_DIR" ] || exit 0

# Remove old cache files that are NOT post pages.
# Post URLs match /@author/permlink (two path segments after /).
# Index pages (/, /@author) get stale and need refresh.
find "$CACHE_DIR" -type f -name "*.html" -mmin +60 | while read -r f; do
  url=$(head -1 "$f")
  # Post URLs: <!-- https://domain/@author/permlink -->
  # Count path segments after the domain
  case "$url" in
    */@*/*) ;; # post page — keep it
    *) rm -f "$f" ;;
  esac
done

# Prune oldest post cache files when cache exceeds 100GB
MAX_KB=$((100 * 1024 * 1024))  # 100GB in KB
used_kb=$(du -sk "$CACHE_DIR" 2>/dev/null | awk '{print $1}')
if [ "$used_kb" -gt "$MAX_KB" ] 2>/dev/null; then
  # Delete oldest files first until under limit
  find "$CACHE_DIR" -type f -name "*.html" -printf '%T+ %p\n' | sort | while read -r _ts f; do
    rm -f "$f"
    used_kb=$(du -sk "$CACHE_DIR" 2>/dev/null | awk '{print $1}')
    [ "$used_kb" -le "$MAX_KB" ] && break
  done
fi

# Clean up empty directories
find "$CACHE_DIR" -type d -empty -delete 2>/dev/null
