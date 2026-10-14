"""Helpers shared by route modules: spec form parsing and status helpers."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from leadgen.models import Department, Seniority, Spec


def _list(form: Any, key: str) -> list[str]:
    raw = form.get(key) or ""
    return [p.strip() for p in str(raw).replace("\n", ",").split(",") if p.strip()]


def _multi(form: Any, key: str) -> list[str]:
    return [v for v in form.getlist(key) if v]


def _int(form: Any, key: str, default: int) -> int:
    try:
        return int(form.get(key) or default)
    except (TypeError, ValueError):
        return default


def _float(form: Any, key: str, default: float) -> float:
    try:
        return float(form.get(key) or default)
    except (TypeError, ValueError):
        return default


def spec_from_form(form: Any) -> Spec:
    founded_min = form.get("founded_min")
    founded_max = form.get("founded_max")
    run_hours = None
    if form.get("run_hours_start") and form.get("run_hours_end"):
        run_hours = (str(form.get("run_hours_start")), str(form.get("run_hours_end")))
    data = {
        "name": (form.get("name") or "untitled").strip(),
        "offer": (form.get("offer") or "").strip(),
        "company": {
            "industries": _list(form, "industries"),
            "keywords": _list(form, "keywords"),
            "exclude_keywords": _list(form, "exclude_keywords"),
            "locations": _multi(form, "locations") or _list(form, "locations_text"),
            "employee_size": _multi(form, "employee_size"),
            "founded_year": {"min": int(founded_min) if founded_min else None, "max": int(founded_max) if founded_max else None},
            "company_types": _list(form, "company_types"),
            "discover_via_jobs": bool(form.get("discover_via_jobs")),
            "seed_companies": _list(form, "seed_companies"),
            "exclude_companies": _list(form, "exclude_companies"),
            "max_companies": _int(form, "max_companies", 20),
            "max_company_queries": _int(form, "max_company_queries", 10),
            "max_search_pages_per_query": _int(form, "max_search_pages_per_query", 2),
        },
        "people": {
            "titles": _list(form, "titles"),
            "exclude_titles": _list(form, "exclude_titles"),
            "departments": [Department(d) for d in _multi(form, "departments")],
            "seniorities": [Seniority(s) for s in _multi(form, "seniorities")],
            "locations": _multi(form, "people_locations") or _list(form, "people_locations_text"),
            "keywords": _list(form, "people_keywords"),
            "network": _multi(form, "network"),
            "include_anonymized": bool(form.get("include_anonymized")),
            "max_per_company": _int(form, "max_per_company", 100),
            "max_pages_per_facet": _int(form, "max_pages_per_facet", 10),
            "broad_sweep": bool(form.get("broad_sweep")),
            "enrich": {"enabled": bool(form.get("enrich_enabled")), "max_per_company": _int(form, "enrich_max", 10)},
        },
        "run": {
            "source": form.get("source") or "linkedin",
            "strategy": form.get("strategy") or "people_first",
            "daily_budget": _int(form, "daily_budget", 100),
            "monthly_search_cap": _int(form, "monthly_search_cap", 250),
            "min_delay_s": _float(form, "min_delay_s", 8.0),
            "max_delay_s": _float(form, "max_delay_s", 20.0),
            "break_every": _int(form, "break_every", 25),
            "break_min_s": _float(form, "break_min_s", 120.0),
            "break_max_s": _float(form, "break_max_s", 300.0),
            "run_hours": run_hours,
            "llm_assist": None if form.get("llm_assist") in (None, "", "auto") else form.get("llm_assist") == "on",
        },
    }
    return Spec.model_validate(data)


def status_badge(status: str) -> str:
    return {
        "running": "bg-blue-100 text-blue-800", "done": "bg-green-100 text-green-800",
        "paused": "bg-yellow-100 text-yellow-800", "failed": "bg-red-100 text-red-800",
        "stopped": "bg-gray-200 text-gray-700", "queued": "bg-purple-100 text-purple-800",
    }.get(status, "bg-gray-100 text-gray-700")


def state(request: Request):
    return request.app.state
