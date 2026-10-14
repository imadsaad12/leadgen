"""Rule-based job-title normalization and classification driven by taxonomy.yaml (+ overrides.yaml)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from leadgen.models import Department, Seniority, TitleClassification

TAXONOMY_PATH = Path(__file__).with_name("taxonomy.yaml")
OVERRIDES_PATH = Path(__file__).with_name("overrides.yaml")

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF⭐⬆↔-↪⌀-⏿■-◿✀-➿️‍]+"
)
_AT_RE = re.compile(r"\s+(?:at|@|bei|chez|en|presso|hos)\s+.+$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_CONNECT_RE = re.compile(r"\s*(?:&|\+|\band\b|\bund\b|\bet\b|\by\b)\s*")


@dataclass
class SeniorityRule:
    level: Seniority
    patterns: list[re.Pattern[str]]


@dataclass
class DepartmentRule:
    department: Department
    label: str
    patterns: list[tuple[int, re.Pattern[str]]]


@dataclass
class Taxonomy:
    separators: list[str]
    noise: list[re.Pattern[str]]
    abbreviations: list[tuple[re.Pattern[str], str]]
    seniority: list[SeniorityRule]
    departments: list[DepartmentRule]
    canonical: list[tuple[re.Pattern[str], str]]
    labels: dict[Department, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = TAXONOMY_PATH) -> Taxonomy:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        noise = [re.compile(p, re.IGNORECASE) for p in raw.get("noise_patterns", [])]
        abbreviations: list[tuple[re.Pattern[str], str]] = []
        for k, v in sorted(raw.get("abbreviations", {}).items(), key=lambda kv: -len(kv[0])):
            abbreviations.append((re.compile(rf"(?<![\w&]){re.escape(k)}(?![\w&])", re.IGNORECASE), v))
        seniority = [
            SeniorityRule(Seniority(s["level"]), [re.compile(p, re.IGNORECASE) for p in s["patterns"]])
            for s in raw.get("seniority", [])
        ]
        departments: list[DepartmentRule] = []
        labels: dict[Department, str] = {}
        for name, cfg in raw.get("departments", {}).items():
            dep = Department(name)
            labels[dep] = cfg.get("label", name.replace("_", " ").title())
            pats = [(int(w), re.compile(p, re.IGNORECASE)) for w, p in cfg.get("patterns", [])]
            departments.append(DepartmentRule(dep, labels[dep], pats))
        canonical = [(re.compile(p, re.IGNORECASE), label) for p, label in raw.get("canonical_titles", [])]
        return cls(raw.get("separators", ["|", ",", "/"]), noise, abbreviations, seniority, departments, canonical, labels)


@lru_cache(maxsize=1)
def taxonomy() -> Taxonomy:
    return Taxonomy.load()


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, Any]] = {}
    for key, val in (data.get("titles") or {}).items():
        out[normalize_key(str(key))] = val or {}
    return out


def save_override(normalized: str, department: str, seniority: str, canonical: str | None = None,
                  path: Path = OVERRIDES_PATH) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    data = data or {}
    titles = data.setdefault("titles", {}) or {}
    entry: dict[str, Any] = {"department": department, "seniority": seniority}
    if canonical:
        entry["canonical"] = canonical
    titles[normalized] = entry
    data["titles"] = titles
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")


# ----------------------------------------------------------------------------- normalization


def normalize_key(text: str) -> str:
    """Cheap normalization used as the lookup key for caches and overrides."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _EMOJI_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip(" .-_|•·")
    return text


def _expand(text: str, tax: Taxonomy) -> str:
    for pat, repl in tax.abbreviations:
        text = pat.sub(repl, text)
    return text


def split_segments(text: str, tax: Taxonomy) -> list[str]:
    parts = [text]
    for sep in tax.separators:
        nxt: list[str] = []
        for p in parts:
            nxt.extend(p.split(sep))
        parts = nxt
    return [p.strip(" .-_") for p in parts if p and p.strip(" .-_")]


def normalize_title(raw: str, tax: Taxonomy | None = None) -> tuple[str, list[str]]:
    """Return (primary normalized title, function tags)."""
    tax = tax or taxonomy()
    text = unicodedata.normalize("NFKC", raw or "").lower()
    text = _EMOJI_RE.sub(" ", text)
    for pat in tax.noise:
        text = pat.sub(" ", text)
    text = _AT_RE.sub("", text)
    text = text.replace("&amp;", "&")
    text = _WS_RE.sub(" ", text).strip()
    segments = split_segments(text, tax) or [text]
    segments = [_WS_RE.sub(" ", _expand(s, tax)).strip() for s in segments]
    segments = [s for s in segments if s]
    if not segments:
        return "", []
    primary = segments[0]
    tags = segments[1:]
    # "Director, Operations" / "Vice President, Engineering": the first segment is a bare level and the
    # department lives in the next one -> merge them so the primary title carries both.
    if tags and not _department_scores(primary, tax) and _department_scores(tags[0], tax):
        primary = f"{primary} {tags[0]}"
        tags = tags[1:]
    # "Founder & CEO" style: keep the whole conjunction in primary, but also expose each half as a tag.
    for half in _CONNECT_RE.split(primary):
        half = half.strip()
        if half and half != primary and half not in tags:
            tags.append(half)
    return primary, tags


# ----------------------------------------------------------------------------- classification


def _seniority(text: str, tax: Taxonomy) -> tuple[Seniority, bool]:
    for rule in tax.seniority:
        if any(p.search(text) for p in rule.patterns):
            return rule.level, True
    return Seniority.MID, False


def _department_scores(text: str, tax: Taxonomy) -> dict[Department, int]:
    scores: dict[Department, int] = {}
    for rule in tax.departments:
        s = 0
        for weight, pat in rule.patterns:
            if pat.search(text):
                s += weight
        if s:
            scores[rule.department] = s
    return scores


def _pick_department(primary: str, tags: list[str], seniority: Seniority, tax: Taxonomy) -> tuple[Department, int]:
    scores = _department_scores(primary, tax)
    if not scores:
        full = " | ".join([primary, *tags])
        scores = _department_scores(full, tax)
    if not scores:
        if seniority in (Seniority.CXO, Seniority.FOUNDER):
            return Department.EXECUTIVE, 1
        return Department.UNCLASSIFIED, 0
    best = max(scores.values())
    winners = [d for d, s in scores.items() if s == best]
    if len(winners) > 1 and Department.EXECUTIVE in winners:
        winners = [d for d in winners if d != Department.EXECUTIVE]
    if len(winners) > 1:
        # break the tie with the tags (e.g. "CTO | software architecture" -> engineering)
        tag_scores = _department_scores(" | ".join(tags), tax) if tags else {}
        ranked = sorted(winners, key=lambda d: tag_scores.get(d, 0), reverse=True)
        if len(ranked) > 1 and tag_scores.get(ranked[0], 0) > tag_scores.get(ranked[1], 0):
            return ranked[0], best
        return Department.UNCLASSIFIED, best
    return winners[0], best


def _canonical(primary: str, tax: Taxonomy) -> str | None:
    for pat, label in tax.canonical:
        if pat.search(primary):
            return label
    return None


def classify_rule(raw_title: str, tax: Taxonomy | None = None) -> TitleClassification:
    tax = tax or taxonomy()
    primary, tags = normalize_title(raw_title, tax)
    if not primary:
        return TitleClassification(raw_title=raw_title, normalized="", method="none")
    seniority, explicit = _seniority(primary, tax)
    department, score = _pick_department(primary, tags, seniority, tax)
    if department == Department.UNCLASSIFIED:
        confidence = 0.0
        method = "none"
    else:
        confidence = 0.9 if score >= 3 else 0.6
        if not explicit:
            confidence -= 0.15
        if department == Department.EXECUTIVE and score == 1:
            confidence = 0.7
        method = "rule"
    return TitleClassification(
        raw_title=raw_title,
        normalized=primary,
        canonical_title=_canonical(primary, tax) or primary.title(),
        department=department,
        seniority=seniority,
        function_tags=tags,
        confidence=round(max(confidence, 0.0), 2),
        method=method,
    )


def classify_with_overrides(raw_title: str, overrides: dict[str, dict[str, Any]] | None = None,
                            tax: Taxonomy | None = None) -> TitleClassification:
    """Overrides (by normalized key or by normalized primary) beat rules."""
    tax = tax or taxonomy()
    overrides = overrides if overrides is not None else load_overrides()
    result = classify_rule(raw_title, tax)
    for key in (normalize_key(raw_title), result.normalized):
        ov = overrides.get(key)
        if ov:
            try:
                dep = Department(ov.get("department", result.department.value))
                sen = Seniority(ov.get("seniority", result.seniority.value))
            except ValueError:
                continue
            return result.model_copy(
                update={
                    "department": dep,
                    "seniority": sen,
                    "canonical_title": ov.get("canonical") or result.canonical_title,
                    "confidence": 1.0,
                    "method": "override",
                }
            )
    return result


def department_label(dep: Department) -> str:
    return taxonomy().labels.get(dep, dep.value.replace("_", " ").title())
