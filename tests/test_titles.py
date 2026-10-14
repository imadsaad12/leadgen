"""Golden-set and regression tests for the rule-based title classifier."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from leadgen.models import Department, Seniority
from leadgen.titles.rules import classify_rule

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "titles_golden.csv"

MIN_PAIR_ACCURACY = 0.90
MIN_DEPARTMENT_ACCURACY = 0.93

_TOP = {Seniority.CXO, Seniority.FOUNDER, Seniority.VP}
_BOTTOM = {Seniority.INTERN, Seniority.ENTRY}

# Fixed regression cases: these pass today and must keep passing.
FIXED_CASES: list[tuple[str, str, str]] = [
    ("SVP Growth & Partnerships", "sales", "vp"),
    ("Werkstudent Softwareentwicklung", "engineering", "intern"),
    ("Chief People Officer", "hr", "cxo"),
    ("CEO & Co-Founder", "executive", "founder"),
    ("CTO", "engineering", "cxo"),
    ("Senior Software Engineer at Acme", "engineering", "senior"),
    ("Account Executive, Enterprise", "sales", "mid"),
    ("Head of Marketing | ex-Google", "marketing", "director"),
    ("VP of Sales", "sales", "vp"),
    ("Product Manager", "product", "manager"),
    ("Engineering Manager", "engineering", "manager"),
    ("Customer Success Manager", "customer_success", "manager"),
    ("Financial Controller", "finance", "mid"),
    ("Talent Acquisition Partner", "hr", "mid"),
    ("Managing Director", "executive", "cxo"),
    ("Staff Data Scientist", "data", "lead"),
    ("UX Designer", "design", "mid"),
    ("IT Support Specialist", "it", "mid"),
    ("Legal Counsel", "legal", "mid"),
    ("Junior DevOps Engineer", "engineering", "entry"),
    ("Director, Operations", "operations", "director"),
    ("Sales Engineer", "sales", "mid"),
    ("Executive Assistant to the CEO", "admin", "entry"),
    ("Partner", "executive", "cxo"),
    ("HR Business Partner", "hr", "mid"),
    ("Vice President, Engineering", "engineering", "vp"),
    ("Research Scientist, NLP", "data", "mid"),
    ("Scrum Master", "operations", "mid"),
    ("Gründer & Geschäftsführer", "executive", "founder"),
    ("Software Engineer Intern", "engineering", "intern"),
    ("Head of Growth", "marketing", "director"),
    ("Client Partner", "customer_success", "mid"),
    ("Managing Partner", "executive", "cxo"),
    ("President", "executive", "cxo"),
    ("Chief of Staff to the CEO", "operations", "cxo"),
    ("Office Manager", "admin", "manager"),
    ("Backend Developer (Python)", "engineering", "mid"),
    ("Senior Account Manager", "sales", "manager"),
    ("Recruiter", "hr", "mid"),
    ("Marketing Specialist", "marketing", "mid"),
]


def load_golden() -> list[tuple[str, Department, Seniority]]:
    with GOLDEN_PATH.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, f"{GOLDEN_PATH} is empty"
    out: list[tuple[str, Department, Seniority]] = []
    for i, row in enumerate(rows, start=2):
        try:
            out.append((row["raw_title"], Department(row["department"]), Seniority(row["seniority"])))
        except (KeyError, ValueError) as exc:  # pragma: no cover - fixture sanity
            raise AssertionError(f"{GOLDEN_PATH}:{i}: bad row {row!r}: {exc}") from exc
    return out


def _format_table(rows: list[tuple[str, str, str, str, str]]) -> str:
    header = ("raw_title", "expected", "got", "normalized", "method")
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
    return "\n".join([line, sep, *body])


def _is_catastrophic(expected: Seniority, got: Seniority) -> bool:
    return (expected in _TOP and got in _BOTTOM) or (expected in _BOTTOM and got in _TOP)


def test_golden_set_accuracy() -> None:
    golden = load_golden()
    pair_hits = 0
    dep_hits = 0
    mismatches: list[tuple[str, str, str, str, str]] = []
    catastrophic: list[tuple[str, str, str, str, str]] = []
    for raw, exp_dep, exp_sen in golden:
        res = classify_rule(raw)
        dep_ok = res.department == exp_dep
        sen_ok = res.seniority == exp_sen
        dep_hits += dep_ok
        pair_hits += dep_ok and sen_ok
        row = (
            raw,
            f"{exp_dep.value}/{exp_sen.value}",
            f"{res.department.value}/{res.seniority.value}",
            res.normalized,
            res.method,
        )
        if not (dep_ok and sen_ok):
            mismatches.append(row)
        if _is_catastrophic(exp_sen, res.seniority):
            catastrophic.append(row)

    n = len(golden)
    pair_acc = pair_hits / n
    dep_acc = dep_hits / n
    report = (
        f"\nrows={n} pair_accuracy={pair_acc:.3f} (min {MIN_PAIR_ACCURACY}) "
        f"department_accuracy={dep_acc:.3f} (min {MIN_DEPARTMENT_ACCURACY}) "
        f"mismatches={len(mismatches)} catastrophic={len(catastrophic)}\n"
    )
    if mismatches:
        report += "\nMismatches:\n" + _format_table(mismatches) + "\n"
    if catastrophic:
        report += "\nCatastrophic seniority errors:\n" + _format_table(catastrophic) + "\n"
    print(report)

    assert not catastrophic, "catastrophic seniority errors (top-level <-> intern/entry):" + report
    assert pair_acc >= MIN_PAIR_ACCURACY, "exact (department, seniority) accuracy below threshold:" + report
    assert dep_acc >= MIN_DEPARTMENT_ACCURACY, "department accuracy below threshold:" + report


@pytest.mark.parametrize(("raw", "department", "seniority"), FIXED_CASES, ids=[c[0] for c in FIXED_CASES])
def test_fixed_cases(raw: str, department: str, seniority: str) -> None:
    res = classify_rule(raw)
    assert (res.department.value, res.seniority.value) == (department, seniority), (
        f"{raw!r} -> {res.department.value}/{res.seniority.value} (normalized={res.normalized!r})"
    )
