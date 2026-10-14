# Vendored: stickerdaniel/linkedin-mcp-server

- Upstream: https://github.com/stickerdaniel/linkedin-mcp-server (Apache-2.0)
- Pinned at commit `6eb4d1f` on `main` (dated 2026-09-30, just after the `v4.26.1` tag).
- Local change: `patches/0001-search-page-and-geo.patch` adds an optional `page` (1-100) argument to the
  `search_people` and `search_companies` tools so a client can read beyond the first results page. It threads
  `page` through the MCP tool layer (`tools/person.py`, `tools/company.py`), the extractor facade
  (`scraping/extractor.py`), the scrapers (`scraping/person.py`, `scraping/company.py`) and the URL builders
  (`scraping/search_urls.py`), and updates the tool-contract fixture. Page 1 / omitted produces the exact
  upstream URL, so existing behaviour is unchanged.

The patch is a minimal unified diff against that base commit and applies with `patch -p1` or `git apply --3way`.
Upgrade with `scripts/update_vendor.sh <tag-or-branch>`; it re-clones, re-applies this patch and runs the upstream
tests that cover the change.
