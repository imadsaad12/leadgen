"""Exports: CSV, JSONL/JSON, XLSX and a Markdown report for a run."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from leadgen.models import Department, RunStats, TitleClassification
from leadgen.pipeline.classify import matches_spec
from leadgen.store.runs import RunStore
from leadgen.titles.grouping import group_people, pivot
from leadgen.titles.rules import department_label

LEAD_COLUMNS = [
    "full_name", "raw_title", "canonical_title", "department", "seniority", "company_name", "company_industry",
    "company_hq", "company_size", "phone", "email", "website", "location", "degree", "headline", "profile_url", "company_linkedin_url",
    "matched_spec", "fit_score", "relevance_reason", "relevant", "confidence", "method", "needs_review",
    "found_via", "person_key", "company_key",
]


def lead_rows(run_id: str, store: RunStore) -> list[dict[str, Any]]:
    run = store.get_run(run_id)
    spec = run.spec if run else None
    companies = {c.key: c for c in store.list_companies(run_id)}
    rows: list[dict[str, Any]] = []
    for person, cls in store.list_people(run_id, limit=100000):
        c = companies.get(person.company_key)
        cls = cls or TitleClassification(raw_title=person.raw_title or "", normalized="")
        rows.append({
            "full_name": person.full_name,
            "raw_title": person.raw_title or "",
            "canonical_title": cls.canonical_title or "",
            "department": cls.department.value,
            "seniority": cls.seniority.value,
            "company_name": (c.name if c else person.company_name) or person.company_key,
            "company_industry": c.industry if c else "",
            "company_hq": c.hq if c else "",
            "company_size": c.size_bucket if c else "",
            "phone": person.phone or "",
            "email": person.email or "",
            "website": person.website or "",
            "location": person.location or "",
            "degree": person.degree or "",
            "headline": person.headline or "",
            "profile_url": person.profile_url,
            "company_linkedin_url": c.linkedin_url if c else "",
            "matched_spec": matches_spec(cls, spec) if spec else "",
            "fit_score": person.fit_score if person.fit_score is not None else "",
            "relevance_reason": person.relevance_reason or "",
            "relevant": "" if person.relevant is None else person.relevant,
            "confidence": cls.confidence,
            "method": cls.method,
            "needs_review": person.needs_review,
            "found_via": "; ".join(person.found_via),
            "person_key": person.key,
            "company_key": person.company_key,
        })
    return rows


def company_rows(run_id: str, store: RunStore) -> list[dict[str, Any]]:
    counts = store.company_people_counts(run_id)
    rows = []
    for c in store.list_companies(run_id):
        found = counts.get(c.key, 0)
        mid = c.size_midpoint
        rows.append({
            "name": c.name, "slug": c.slug or "", "linkedin_url": c.linkedin_url or "", "industry": c.industry or "",
            "hq": c.hq or "", "size": c.size_bucket or "", "employees_hint": c.employee_count_hint or "",
            "founded": c.founded or "", "type": c.company_type or "", "website": c.website or "", "status": c.status,
            "people_found": found, "coverage": (round(found / mid, 2) if mid else ""),
            "filter_reasons": "; ".join(c.filter_reasons), "unknown_fields": ", ".join(c.unknown_fields),
            "discovered_via": "; ".join(c.discovered_via), "urn": c.urn or "",
        })
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> Path:
    columns = columns or (list(rows[0].keys()) if rows else [])
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def write_json(run_id: str, store: RunStore, out_dir: Path) -> tuple[Path, Path]:
    leads = lead_rows(run_id, store)
    run = store.get_run(run_id)
    payload = {
        "run": run.model_dump(mode="json") if run else {"id": run_id},
        "companies": company_rows(run_id, store),
        "leads": leads,
        "groups": [g.model_dump(mode="json") for g in group_people(store.list_people(run_id, limit=100000))],
    }
    jpath = out_dir / "run.json"
    jpath.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lpath = out_dir / "leads.jsonl"
    with lpath.open("w", encoding="utf-8") as f:
        for r in leads:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return jpath, lpath


def write_xlsx(run_id: str, store: RunStore, out_dir: Path, stats: RunStats | None = None,
               budget: dict[str, Any] | None = None) -> Path:
    import pandas as pd

    leads = lead_rows(run_id, store)
    companies = company_rows(run_id, store)
    rows = store.list_people(run_id, limit=100000)
    path = out_dir / "leads.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(leads, columns=LEAD_COLUMNS).to_excel(xw, sheet_name="Leads", index=False)
        pd.DataFrame(companies).to_excel(xw, sheet_name="Companies", index=False)
        by_title = [
            {"canonical_title": g.canonical_title, "department": g.department.value if g.department else "",
             "seniority": g.seniority.value if g.seniority else "", "count": g.count,
             "companies": len(g.by_company), "examples": "; ".join(t for t, _ in g.top_raw_titles[:3])}
            for g in group_people(rows, by="canonical_title")
        ]
        pd.DataFrame(by_title).to_excel(xw, sheet_name="By Title", index=False)
        pv = pivot(rows, "department", "seniority")
        dept_tbl = [{"department": r["key"], **dict(zip(pv["columns"], r["cells"], strict=False)), "total": r["total"]} for r in pv["rows"]]
        pd.DataFrame(dept_tbl).to_excel(xw, sheet_name="By Department", index=False)
        cp = pivot(rows, "company", "department")
        comp_tbl = [{"company": r["key"], **dict(zip(cp["columns"], r["cells"], strict=False)), "total": r["total"]} for r in cp["rows"]]
        pd.DataFrame(comp_tbl).to_excel(xw, sheet_name="Company x Dept", index=False)
        pd.DataFrame(store.unclassified_titles(run_id)).to_excel(xw, sheet_name="Unclassified", index=False)
        run = store.get_run(run_id)
        info = {"run_id": run_id, "name": run.spec.name if run else "", "status": run.status if run else ""}
        if stats:
            info.update({f"stats.{k}": v for k, v in stats.model_dump().items()})
        if budget:
            info.update({f"budget.{k}": v for k, v in budget.items()})
        if run:
            info["spec"] = run.spec.model_dump_json()
        pd.DataFrame([{"key": k, "value": v} for k, v in info.items()]).to_excel(xw, sheet_name="Run Info", index=False)
    return path


def build_report(run_id: str, store: RunStore, stats: RunStats | None = None, budget: dict[str, Any] | None = None) -> str:
    run = store.get_run(run_id)
    rows = store.list_people(run_id, limit=100000)
    companies = store.list_companies(run_id)
    counts = store.company_people_counts(run_id)
    lines: list[str] = []
    name = run.spec.name if run else run_id
    lines.append(f"# LeadGen report: {name}\n")
    lines.append(f"Run `{run_id}` status **{run.status if run else '?'}**")
    if stats:
        s = stats
        lines.append(
            f"- Companies: {s.companies_discovered} discovered, {s.companies_passed} passed filters, "
            f"{s.companies_enumerated} enumerated\n- People: {s.people_found} found, {s.people_classified} classified, "
            f"{s.people_unclassified} unclassified\n- LinkedIn page loads this run: {s.page_loads} (cache hits {s.cache_hits}); "
            f"claude calls: {s.llm_calls}"
        )
    if budget:
        lines.append(f"- Budget: today {budget.get('today_page_loads')}/{budget.get('daily_budget')} page loads, "
                     f"month {budget.get('month_searches')}/{budget.get('monthly_search_cap')} searches")
    lines.append("\n## Companies\n")
    lines.append("| Company | Industry | HQ | Size | Status | People | Coverage | Notes |")
    lines.append("|---|---|---|---|---|---:|---:|---|")
    for c in companies:
        found = counts.get(c.key, 0)
        mid = c.size_midpoint
        cov = f"{found / mid:.0%}" if mid else "?"
        notes = "; ".join(c.filter_reasons) or (f"unknown: {', '.join(c.unknown_fields)}" if c.unknown_fields else "")
        lines.append(f"| {c.name} | {c.industry or '?'} | {c.hq or '?'} | {c.size_bucket or '?'} | {c.status} | {found} | {cov} | {notes} |")
    pv = pivot(rows, "department", "seniority")
    if pv["rows"]:
        lines.append("\n## Department x seniority\n")
        lines.append("| Department | " + " | ".join(pv["columns"]) + " | Total |")
        lines.append("|---|" + "---:|" * (len(pv["columns"]) + 1))
        for r in pv["rows"]:
            label = department_label(Department(r["key"])) if r["key"] in Department._value2member_map_ else r["key"]
            lines.append(f"| {label} | " + " | ".join(str(x) for x in r["cells"]) + f" | {r['total']} |")
    groups = group_people(rows, by="department")
    if groups:
        lines.append("\n## Top titles per department\n")
        for g in groups:
            label = department_label(g.department) if g.department else g.group_key
            top = ", ".join(f"{t} ({n})" for t, n in g.top_raw_titles[:5])
            lines.append(f"- **{label}** ({g.count}): {top}")
    unclassified = store.unclassified_titles(run_id, limit=50)
    if unclassified:
        lines.append("\n## Unclassified titles (assign them on the Titles page)\n")
        for u in unclassified:
            lines.append(f"- {u['raw_title']} ({u['n']})")
    review = store.count_people(run_id, needs_review=True)
    if review:
        lines.append(f"\n{review} people need review (no title could be read from the page).")
    return "\n".join(lines) + "\n"


def write_all_exports(run_id: str, store: RunStore, out_dir: Path, stats: RunStats | None = None,
                      budget: dict[str, Any] | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    all_rows = lead_rows(run_id, store)
    scored = [r for r in all_rows if r["relevant"] != ""]
    if scored:
        kept = [r for r in all_rows if r["relevant"] in (True, 1)]
        excluded = [r for r in all_rows if r["relevant"] in (False, 0)]
        paths["leads_csv"] = write_csv(out_dir / "leads.csv", kept, LEAD_COLUMNS)
        paths["excluded_csv"] = write_csv(out_dir / "excluded.csv", excluded, LEAD_COLUMNS)
    else:
        paths["leads_csv"] = write_csv(out_dir / "leads.csv", all_rows, LEAD_COLUMNS)
    paths["companies_csv"] = write_csv(out_dir / "companies.csv", company_rows(run_id, store))
    paths["run_json"], paths["leads_jsonl"] = write_json(run_id, store, out_dir)
    paths["leads_xlsx"] = write_xlsx(run_id, store, out_dir, stats, budget)
    report = out_dir / "report.md"
    report.write_text(build_report(run_id, store, stats, budget), encoding="utf-8")
    paths["report_md"] = report
    return paths
