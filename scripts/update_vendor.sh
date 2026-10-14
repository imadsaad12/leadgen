#!/usr/bin/env bash
# Re-vendor linkedin-mcp-server at a given tag and re-apply our page-parameter patch.
# Usage: scripts/update_vendor.sh v4.27.0
set -euo pipefail
TAG="${1:?usage: $0 <upstream-tag>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/vendor/linkedin-mcp-server"
PATCH="$ROOT/vendor/patches/0001-search-page-and-geo.patch"
TMP="$(mktemp -d)"
git clone --quiet --depth 1 --branch "$TAG" https://github.com/stickerdaniel/linkedin-mcp-server "$TMP/src"
( cd "$TMP/src" && git apply --3way "$PATCH" && rm -rf .git )
rm -rf "$DEST"
mv "$TMP/src" "$DEST"
rm -rf "$TMP"
sed -i '' "s/^- Pinned tag: .*/- Pinned tag: $TAG/" "$ROOT/vendor/VENDORED.md"
echo "vendored $TAG with patch applied; running upstream tests touching the patch..."
( cd "$DEST" && uv run --group dev pytest tests/test_server.py tests/scraping/test_search_urls.py -q -p no:cacheprovider )
( cd "$ROOT" && uv sync --extra dev >/dev/null && echo "project re-synced" )
