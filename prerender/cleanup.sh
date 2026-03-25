#!/bin/sh
# Remove cached index pages older than 1 hour.
# Post pages (/@author/permlink) stay cached indefinitely.
#
# Run via entrypoint loop every hour inside the prerender container.

CACHE_DIR="${CACHE_ROOT_DIR:-/cache}"

[ -d "$CACHE_DIR" ] || exit 0

# Remove index page cache files older than 1 hour.
# Post pages (in posts/) stay cached indefinitely.
find "$CACHE_DIR/pages" -type f -name "*.html" -mmin +60 -delete 2>/dev/null

# Prune oldest cache files when cache exceeds 100GB
MAX_KB=$((100 * 1024 * 1024))  # 100GB in KB
used_kb=$(du -sk "$CACHE_DIR" 2>/dev/null | awk '{print $1}')
if [ "$used_kb" -gt "$MAX_KB" ] 2>/dev/null; then
  excess_kb=$((used_kb - MAX_KB))
  # List oldest files first with their size, delete until we've freed enough
  find "$CACHE_DIR/posts" -type f -name "*.html" -printf '%T+ %k %p\n' | sort | while read -r _ts size_kb f; do
    rm -f "$f"
    excess_kb=$((excess_kb - size_kb))
    [ "$excess_kb" -le 0 ] && break
  done
fi

# Clean up empty directories
find "$CACHE_DIR" -type d -empty -delete 2>/dev/null
