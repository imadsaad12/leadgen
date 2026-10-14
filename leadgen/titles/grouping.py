"""Group classified people by department x seniority and by canonical title; build report pivots."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable

from leadgen.models import SENIORITY_ORDER, Department, Person, Seniority, TitleClassification, TitleGroup


def _sen_rank(s: Seniority) -> int:
    return SENIORITY_ORDER.index(s) if s in SENIORITY_ORDER else len(SENIORITY_ORDER)


def group_people(
    rows: Iterable[tuple[Person, TitleClassification | None]],
    by: str = "department_seniority",
) -> list[TitleGroup]:
    groups: dict[str, TitleGroup] = {}
    raw_counter: dict[str, Counter[str]] = defaultdict(Counter)
    for person, cls in rows:
        cls = cls or TitleClassification(raw_title=person.raw_title or "", normalized="")
        if by == "canonical_title":
            key = cls.canonical_title or cls.normalized or "(no title)"
            g = groups.get(key) or TitleGroup(
                group_key=key, department=cls.department, seniority=cls.seniority, canonical_title=key
            )
        elif by == "department":
            key = cls.department.value
            g = groups.get(key) or TitleGroup(group_key=key, department=cls.department)
        else:
            key = cls.group_key
            g = groups.get(key) or TitleGroup(group_key=key, department=cls.department, seniority=cls.seniority)
        g.count += 1
        g.by_company[person.company_key] = g.by_company.get(person.company_key, 0) + 1
        g.lead_keys.append(person.key)
        raw_counter[key][person.raw_title or ""] += 1
        groups[key] = g
    for key, g in groups.items():
        g.top_raw_titles = raw_counter[key].most_common(5)
    ordered = sorted(
        groups.values(),
        key=lambda g: (
            g.department.value if g.department else "zzz",
            -_sen_rank(g.seniority) if g.seniority else 0,
            -g.count,
        ),
    )
    return ordered


def pivot(rows: Iterable[tuple[Person, TitleClassification | None]], row_key: str, col_key: str) -> dict:
    """Generic pivot for the report: row_key/col_key in {company, department, seniority}."""
    table: dict[str, Counter[str]] = defaultdict(Counter)
    cols: set[str] = set()

    def val(person: Person, cls: TitleClassification | None, which: str) -> str:
        if which == "company":
            return person.company_name or person.company_key
        if cls is None:
            return "unclassified" if which == "department" else "unknown"
        return getattr(cls, which).value

    for person, cls in rows:
        r, c = val(person, cls, row_key), val(person, cls, col_key)
        table[r][c] += 1
        cols.add(c)
    if col_key == "seniority":
        col_order = [s.value for s in SENIORITY_ORDER if s.value in cols]
    elif col_key == "department":
        col_order = [d.value for d in Department if d.value in cols]
    else:
        col_order = sorted(cols)
    return {
        "columns": col_order,
        "rows": [
            {"key": r, "cells": [table[r].get(c, 0) for c in col_order], "total": sum(table[r].values())}
            for r in sorted(table, key=lambda k: -sum(table[k].values()))
        ],
        "totals": [sum(table[r].get(c, 0) for r in table) for c in col_order],
    }
