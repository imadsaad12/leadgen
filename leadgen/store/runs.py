"""Persistence for specs, runs, companies, facet progress, people, title cache and the LLM cache."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from leadgen.models import (
    Company,
    Person,
    RunState,
    RunStats,
    Spec,
    TitleClassification,
)
from leadgen.store.db import Database, dumps, utcnow


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"


class RunStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ specs
    def save_spec(self, spec: Spec, spec_id: str | None = None) -> str:
        now = utcnow()
        if spec_id is None:
            spec_id = new_id("spec")
            self.db.execute(
                "INSERT INTO specs(id, name, spec_json, created_at, updated_at) VALUES(?,?,?,?,?)",
                (spec_id, spec.name, spec.model_dump_json(), now, now),
            )
        else:
            self.db.execute(
                "UPDATE specs SET name=?, spec_json=?, updated_at=? WHERE id=?",
                (spec.name, spec.model_dump_json(), now, spec_id),
            )
        return spec_id

    def get_spec(self, spec_id: str) -> Spec | None:
        row = self.db.one("SELECT spec_json FROM specs WHERE id=?", (spec_id,))
        return Spec.model_validate_json(row["spec_json"]) if row else None

    def list_specs(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id, name, created_at, updated_at FROM specs ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def delete_spec(self, spec_id: str) -> None:
        self.db.execute("DELETE FROM specs WHERE id=?", (spec_id,))

    # ------------------------------------------------------------------- runs
    def create_run(self, spec: Spec, spec_id: str | None = None) -> RunState:
        run = RunState(id=new_id("run"), spec=spec, status="queued")
        self.db.execute(
            """INSERT INTO runs(id, spec_id, name, spec_json, status, stats_json, cursor_json, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (run.id, spec_id, spec.name, spec.model_dump_json(), run.status, "{}", "{}", utcnow()),
        )
        return run

    def _row_to_run(self, r) -> RunState:
        return RunState(
            id=r["id"],
            spec=Spec.model_validate_json(r["spec_json"]),
            status=r["status"],
            started_at=datetime.fromisoformat(r["started_at"]) if r["started_at"] else None,
            finished_at=datetime.fromisoformat(r["finished_at"]) if r["finished_at"] else None,
            stats=RunStats.model_validate_json(r["stats_json"] or "{}"),
            cursor=json.loads(r["cursor_json"] or "{}"),
            message=r["message"],
        )

    def get_run(self, run_id: str) -> RunState | None:
        row = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        return self._row_to_run(row) if row else None

    def list_runs(self, limit: int = 50) -> list[RunState]:
        rows = self.db.query("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._row_to_run(r) for r in rows]

    def save_run(self, run: RunState) -> None:
        self.db.execute(
            """UPDATE runs SET status=?, started_at=?, finished_at=?, stats_json=?, cursor_json=?, message=?,
               spec_json=? WHERE id=?""",
            (
                run.status,
                run.started_at.isoformat(timespec="seconds") if run.started_at else None,
                run.finished_at.isoformat(timespec="seconds") if run.finished_at else None,
                run.stats.model_dump_json(),
                dumps(run.cursor),
                run.message,
                run.spec.model_dump_json(),
                run.id,
            ),
        )

    def save_stats(self, run_id: str, stats: RunStats) -> None:
        self.db.execute("UPDATE runs SET stats_json=? WHERE id=?", (stats.model_dump_json(), run_id))

    def set_status(self, run_id: str, status: str, message: str | None = None) -> None:
        finished = utcnow() if status in ("done", "failed", "stopped") else None
        self.db.execute(
            "UPDATE runs SET status=?, message=COALESCE(?, message), finished_at=COALESCE(?, finished_at) WHERE id=?",
            (status, message, finished, run_id),
        )

    def running_runs(self) -> list[RunState]:
        rows = self.db.query("SELECT * FROM runs WHERE status IN ('running','queued')")
        return [self._row_to_run(r) for r in rows]

    def delete_run(self, run_id: str) -> None:
        with self.db.transaction() as db:
            for table in ("run_people", "run_facets", "run_companies", "events"):
                db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))

    # -------------------------------------------------------------- companies
    def upsert_company(self, run_id: str, company: Company) -> None:
        self.db.execute(
            """INSERT INTO run_companies(run_id, company_key, status, company_json, updated_at) VALUES(?,?,?,?,?)
               ON CONFLICT(run_id, company_key) DO UPDATE SET status=excluded.status,
                   company_json=excluded.company_json, updated_at=excluded.updated_at""",
            (run_id, company.key, company.status, company.model_dump_json(), utcnow()),
        )

    def get_company(self, run_id: str, key: str) -> Company | None:
        row = self.db.one("SELECT company_json FROM run_companies WHERE run_id=? AND company_key=?", (run_id, key))
        return Company.model_validate_json(row["company_json"]) if row else None

    def list_companies(self, run_id: str, status: str | None = None) -> list[Company]:
        if status:
            rows = self.db.query(
                "SELECT company_json FROM run_companies WHERE run_id=? AND status=? ORDER BY updated_at",
                (run_id, status),
            )
        else:
            rows = self.db.query("SELECT company_json FROM run_companies WHERE run_id=? ORDER BY updated_at", (run_id,))
        return [Company.model_validate_json(r["company_json"]) for r in rows]

    def company_status_counts(self, run_id: str) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) n FROM run_companies WHERE run_id=? GROUP BY status", (run_id,))
        return {r["status"]: r["n"] for r in rows}

    # ----------------------------------------------------------------- facets
    def facet(self, run_id: str, company_key: str, facet_id: str) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT * FROM run_facets WHERE run_id=? AND company_key=? AND facet_id=?", (run_id, company_key, facet_id)
        )
        return dict(row) if row else None

    def update_facet(
        self, run_id: str, company_key: str, facet_id: str, *, last_page: int, status: str, new_found: int
    ) -> None:
        self.db.execute(
            """INSERT INTO run_facets(run_id, company_key, facet_id, last_page, status, new_found, updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(run_id, company_key, facet_id) DO UPDATE SET last_page=excluded.last_page,
                   status=excluded.status, new_found=run_facets.new_found + excluded.new_found,
                   updated_at=excluded.updated_at""",
            (run_id, company_key, facet_id, last_page, status, new_found, utcnow()),
        )

    def list_facets(self, run_id: str, company_key: str | None = None) -> list[dict[str, Any]]:
        if company_key:
            rows = self.db.query(
                "SELECT * FROM run_facets WHERE run_id=? AND company_key=? ORDER BY updated_at", (run_id, company_key)
            )
        else:
            rows = self.db.query("SELECT * FROM run_facets WHERE run_id=? ORDER BY updated_at", (run_id,))
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- people
    def upsert_person(self, run_id: str, person: Person, classification: TitleClassification | None = None) -> None:
        self.db.execute(
            """INSERT INTO run_people(run_id, person_key, company_key, person_json, classification_json, department,
                   seniority, raw_title, needs_review, relevant, fit_score, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id, person_key) DO UPDATE SET company_key=excluded.company_key,
                   person_json=excluded.person_json,
                   classification_json=COALESCE(excluded.classification_json, run_people.classification_json),
                   department=COALESCE(excluded.department, run_people.department),
                   seniority=COALESCE(excluded.seniority, run_people.seniority),
                   raw_title=excluded.raw_title, needs_review=excluded.needs_review,
                   relevant=COALESCE(excluded.relevant, run_people.relevant),
                   fit_score=COALESCE(excluded.fit_score, run_people.fit_score),
                   updated_at=excluded.updated_at""",
            (
                run_id,
                person.key,
                person.company_key,
                person.model_dump_json(),
                classification.model_dump_json() if classification else None,
                classification.department.value if classification else None,
                classification.seniority.value if classification else None,
                person.raw_title,
                1 if person.needs_review else 0,
                (None if person.relevant is None else (1 if person.relevant else 0)),
                person.fit_score,
                utcnow(),
            ),
        )

    def set_classification(self, run_id: str, person_key: str, classification: TitleClassification) -> None:
        self.db.execute(
            "UPDATE run_people SET classification_json=?, department=?, seniority=? WHERE run_id=? AND person_key=?",
            (classification.model_dump_json(), classification.department.value, classification.seniority.value,
             run_id, person_key),
        )

    def set_relevance(self, run_id: str, person_key: str, relevant: bool, fit_score: int, reason: str) -> None:
        row = self.db.one("SELECT person_json FROM run_people WHERE run_id=? AND person_key=?", (run_id, person_key))
        if row is None:
            return
        person = Person.model_validate_json(row["person_json"])
        person.relevant, person.fit_score, person.relevance_reason = relevant, fit_score, reason
        self.db.execute(
            "UPDATE run_people SET person_json=?, relevant=?, fit_score=? WHERE run_id=? AND person_key=?",
            (person.model_dump_json(), 1 if relevant else 0, fit_score, run_id, person_key),
        )

    def leads_for_relevance(self, run_id: str) -> list[Person]:
        rows = self.db.query("SELECT person_json FROM run_people WHERE run_id=? AND relevant IS NULL", (run_id,))
        return [Person.model_validate_json(r["person_json"]) for r in rows]

    def relevance_counts(self, run_id: str) -> dict[str, int]:
        rows = self.db.query(
            "SELECT CASE WHEN relevant IS NULL THEN 'unscored' WHEN relevant=1 THEN 'relevant' ELSE 'excluded' END k,"
            " COUNT(*) n FROM run_people WHERE run_id=? GROUP BY k", (run_id,))
        return {r["k"]: r["n"] for r in rows}

    def people_keys(self, run_id: str, company_key: str | None = None) -> set[str]:
        if company_key:
            rows = self.db.query(
                "SELECT person_key FROM run_people WHERE run_id=? AND company_key=?", (run_id, company_key)
            )
        else:
            rows = self.db.query("SELECT person_key FROM run_people WHERE run_id=?", (run_id,))
        return {r["person_key"] for r in rows}

    @staticmethod
    def _people_filters(
        run_id: str,
        company_key: str | None,
        department: str | None,
        seniority: str | None,
        q: str | None,
        needs_review: bool | None,
        relevant: bool | None = None,
    ) -> tuple[str, list[Any]]:
        sql = " WHERE run_id=?"
        params: list[Any] = [run_id]
        if relevant is not None:
            sql += " AND relevant=?"
            params.append(1 if relevant else 0)
        if company_key:
            sql += " AND company_key=?"
            params.append(company_key)
        if department:
            sql += " AND department=?"
            params.append(department)
        if seniority:
            sql += " AND seniority=?"
            params.append(seniority)
        if q:
            sql += " AND (raw_title LIKE ? OR person_json LIKE ?)"
            params += [f"%{q}%", f"%{q}%"]
        if needs_review is not None:
            sql += " AND needs_review=?"
            params.append(1 if needs_review else 0)
        return sql, params

    def list_people(
        self,
        run_id: str,
        *,
        company_key: str | None = None,
        department: str | None = None,
        seniority: str | None = None,
        q: str | None = None,
        needs_review: bool | None = None,
        relevant: bool | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[tuple[Person, TitleClassification | None]]:
        where, params = self._people_filters(run_id, company_key, department, seniority, q, needs_review, relevant)
        rows = self.db.query(
            "SELECT person_json, classification_json FROM run_people" + where
            + " ORDER BY company_key, department, seniority, raw_title LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        out = []
        for r in rows:
            person = Person.model_validate_json(r["person_json"])
            cls = TitleClassification.model_validate_json(r["classification_json"]) if r["classification_json"] else None
            out.append((person, cls))
        return out

    def count_people(self, run_id: str, **filters: Any) -> int:
        where, params = self._people_filters(
            run_id,
            filters.get("company_key"),
            filters.get("department"),
            filters.get("seniority"),
            filters.get("q"),
            filters.get("needs_review"),
            filters.get("relevant"),
        )
        return int(self.db.scalar("SELECT COUNT(*) FROM run_people" + where, params, 0))

    def group_counts(self, run_id: str, by: Iterable[str] = ("department", "seniority")) -> list[dict[str, Any]]:
        cols = [c for c in by if c in ("department", "seniority", "company_key", "raw_title")]
        if not cols:
            cols = ["department", "seniority"]
        col_sql = ", ".join(cols)
        rows = self.db.query(
            f"SELECT {col_sql}, COUNT(*) AS n FROM run_people WHERE run_id=? GROUP BY {col_sql} ORDER BY n DESC",
            (run_id,),
        )
        return [dict(r) for r in rows]

    def company_people_counts(self, run_id: str) -> dict[str, int]:
        rows = self.db.query(
            "SELECT company_key, COUNT(*) n FROM run_people WHERE run_id=? GROUP BY company_key", (run_id,)
        )
        return {r["company_key"]: r["n"] for r in rows}

    def unclassified_titles(self, run_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = """SELECT raw_title, COUNT(*) AS n FROM run_people
                 WHERE raw_title IS NOT NULL AND raw_title <> '' AND (department IS NULL OR department='unclassified')"""
        params: list[Any] = []
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        sql += " GROUP BY raw_title ORDER BY n DESC, raw_title LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.query(sql, params)]

    def people_with_title(self, raw_title: str, run_id: str | None = None) -> list[tuple[str, str]]:
        sql = "SELECT run_id, person_key FROM run_people WHERE raw_title=?"
        params: list[Any] = [raw_title]
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        return [(r["run_id"], r["person_key"]) for r in self.db.query(sql, params)]

    def all_people_rows(self, run_id: str) -> list[tuple[str, str, str | None]]:
        rows = self.db.query("SELECT person_key, raw_title, classification_json FROM run_people WHERE run_id=?", (run_id,))
        return [(r["person_key"], r["raw_title"], r["classification_json"]) for r in rows]

    # ------------------------------------------------------------ title cache
    def get_title(self, normalized: str) -> TitleClassification | None:
        row = self.db.one("SELECT classification_json FROM title_cache WHERE normalized_title=?", (normalized,))
        return TitleClassification.model_validate_json(row["classification_json"]) if row else None

    def put_title(self, classification: TitleClassification) -> None:
        self.db.execute(
            """INSERT INTO title_cache(normalized_title, classification_json, method, updated_at) VALUES(?,?,?,?)
               ON CONFLICT(normalized_title) DO UPDATE SET classification_json=excluded.classification_json,
                   method=excluded.method, updated_at=excluded.updated_at""",
            (classification.normalized, classification.model_dump_json(), classification.method, utcnow()),
        )

    def title_cache_stats(self) -> dict[str, int]:
        rows = self.db.query("SELECT method, COUNT(*) n FROM title_cache GROUP BY method")
        return {r["method"]: r["n"] for r in rows}

    # --------------------------------------------------------------- llm cache
    def llm_get(self, key: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT response_json FROM llm_cache WHERE key=?", (key,))
        return json.loads(row["response_json"]) if row else None

    def llm_put(self, key: str, kind: str, response: dict[str, Any], usage: dict[str, Any] | None) -> None:
        self.db.execute(
            """INSERT INTO llm_cache(key, kind, response_json, usage_json, created_at) VALUES(?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET response_json=excluded.response_json, usage_json=excluded.usage_json,
                   created_at=excluded.created_at""",
            (key, kind, dumps(response), dumps(usage) if usage else None, utcnow()),
        )

    # ------------------------------------------------------------ maintenance
    def forget_person(self, username: str) -> int:
        n = self.db.execute("DELETE FROM run_people WHERE person_key=?", (username,)).rowcount
        n += self.db.execute("DELETE FROM tool_cache WHERE tool='get_person_profile' AND args_json LIKE ?",
                             (f'%"{username}"%',)).rowcount
        return n

    def purge_older_than(self, days: int) -> dict[str, int]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        old_runs = [r["id"] for r in self.db.query("SELECT id FROM runs WHERE created_at < ?", (cutoff,))]
        for rid in old_runs:
            self.delete_run(rid)
        cache = self.db.execute("DELETE FROM tool_cache WHERE created_at < ?", (cutoff,)).rowcount
        return {"runs": len(old_runs), "cache_entries": cache}
