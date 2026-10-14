# LeadGen

A local, single-user web app that turns a lead specification (industry, location, company size, target titles, …)
into a list of companies and their employees from LinkedIn, grouped by job title (department × seniority and
canonical title), with CSV / JSONL / XLSX exports and a Markdown report.

- **Data source:** your own LinkedIn account, through a vendored, lightly patched copy of
  [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server) (Apache-2.0).
  No paid data APIs.
- **Read-only:** the app can only call read tools (search, profiles, people tab). Connect/message tools are blocked in code.
- **LLM assist (optional):** unclassifiable titles and unreadable pages go through the `claude` CLI using your
  Claude subscription login. No Anthropic API key is used or read.
- **Locations are countries:** company HQ and people filters take country names; LinkedIn HQ lines like "Berlin, Berlin" are mapped to their country through `leadgen/data/locations.yaml`.
- **Budgets:** jittered delays, daily page-load budget, monthly search counter, run-hours window and a circuit breaker.

> **Read before using.** Automated access breaches LinkedIn's User Agreement; accounts get restricted or banned.
> Use a dedicated account, keep the budgets low, and treat the output as personal data under GDPR/CCPA
> (legitimate interest, data minimisation, retention, erasure on request). The app asks you to acknowledge this once.
> Free LinkedIn accounts hit a commercial-use limit after roughly 300 people searches per month; every results
> page counts as one search.

## Setup (macOS)

```bash
brew install uv                      # once
cd LeadGeneration
uv sync --extra dev                  # installs the app and the vendored LinkedIn server into .venv
uv run patchright install chromium   # browser used by the LinkedIn server (also auto-installed on first use)
cp .env.example .env                 # optional; defaults are fine
uv run leadgen serve                 # open http://127.0.0.1:8080
```

Optional LLM assist: install [Claude Code](https://claude.com/claude-code) and run `claude login` once.

## First run

1. **Settings → Start** the LinkedIn sidecar, then **Import from browser** (Chrome/Brave/Edge/Arc where you are
   logged in to LinkedIn) or **Log in manually** (opens a Chromium window). **Run doctor** shows every check.
2. **New run**: fill the spec (or load a template), click **Dry run** to see the estimated page loads, acknowledge the
   notice, **Start run**. The run page streams the log; pause/resume/stop any time. Runs survive app restarts and resume
   from the last facet page without re-loading cached pages.
3. **Leads**: filter by company / department / seniority, group by department × level or by canonical title, export.
4. **Titles**: anything the rules could not classify shows here. Assign it once (saved to
   `leadgen/titles/overrides.yaml` and the title cache) or press **Classify with Claude**.

Try it without LinkedIn first: choose source **Fake data** on the New run page, or run
`uv run leadgen run specs/example.yaml --source fake`.

## CLI

```bash
uv run leadgen serve                              # web app
uv run leadgen doctor                             # toolchain, sidecar, session, claude CLI
uv run leadgen login --browser chrome             # import the session (or omit --browser to log in manually)
uv run leadgen run specs/example.yaml --dry-run   # queries, facets, page-load estimate (no network)
uv run leadgen run specs/example.yaml             # full run with exports under ~/.leadgen/out/<run_id>/
uv run leadgen run specs/example.yaml --resume run_...   # continue a paused run
uv run leadgen reclassify run_...                 # re-apply rules/overrides offline
uv run leadgen export run_... [--out DIR]
uv run leadgen status                             # runs, budget, cache
uv run leadgen purge --older-than-days 90         # retention
uv run leadgen forget <username-or-url>           # erase one person everywhere
```

Running the sidecar by hand (the web app does this for you):
`uv run mcp-server-linkedin --transport streamable-http --port 8000`.

## How it works

```
spec -> discover companies (keyword search / job search / seeds)
     -> profile + filter on the About page (industry, HQ, size, founded, type)
     -> enumerate employees: people tab, then faceted + paged people search by company URN
     -> parse raw page text anchored on the /in/ references -> dedupe
     -> classify titles: overrides -> title cache -> rules (taxonomy.yaml) -> claude CLI (optional)
     -> group, export (csv/jsonl/xlsx/report.md)
```

Every raw page is cached in SQLite (`~/.leadgen/leadgen.db`), so re-runs, resumes and re-parses cost no LinkedIn
page loads. The vendored server gains a `page` argument on `search_people` / `search_companies`
(`vendor/patches/0001-search-page-param.patch`), because upstream only reads the first results page.

### Spec fields

See `specs/example.yaml`. Locations are countries. LinkedIn honours **titles, departments, keywords, people countries, connection degree** and
**seed companies** natively; **industry, HQ, size, founded year and company type** are checked on each company's About
page after discovery (the dry run lists what is native vs post-filtered). Coverage per company
(people found / size-bucket midpoint) is reported; "all employees" is a target, not a guarantee.

## Layout

```
leadgen/            app package: models, store (SQLite), sources (linkedin_mcp client, linkedin adapter, fake),
                    extract (deterministic parsers), titles (taxonomy + rules + grouping), llm (claude CLI),
                    pipeline, export, web (FastAPI + HTMX), cli
vendor/             linkedin-mcp-server v4.26.1 + patches/ + update script (scripts/update_vendor.sh <tag>)
specs/              example.yaml, smoke.yaml
scripts/            record_fixture.py (scrubbed raw-page fixtures), update_vendor.sh
tests/              unit + web + pipeline tests on fake data (uv run pytest)
```

## Updating the parsers when LinkedIn changes its layout

Record a scrubbed fixture from the running sidecar and adjust `leadgen/extract/*`:

```bash
uv run python scripts/record_fixture.py search_people '{"keywords":"engineer","current_company":"1441","page":1}' people_search_1
uv run python scripts/record_fixture.py get_company_profile '{"company_name":"docker"}' company_about_docker
```

The run log warns ("page parse degraded") when the deterministic parser cannot align cards with references; with LLM
assist on, those pages are extracted by the `claude` CLI instead.
