"""Канонический журнал и транзакционная очередь PostgreSQL.

Внешние вызовы не выполняются внутри транзакции. Lease имеет fencing token;
повтор доставки использует прикладной ключ, решения — optimistic revision.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from typing import Any
from uuid import UUID, uuid4

from language_review.memory.safety import digest, sanitize
from language_review.memory.settings import MemorySettings, project_uuid

MIGRATION_FILES = ("migrations/001.sql", "migrations/002.sql")


def _migration_scripts() -> list[tuple[int, str, str]]:
    package = files("language_review.memory")
    return [
        (version, script, digest(script))
        for version, path in enumerate(MIGRATION_FILES, start=1)
        if (script := package.joinpath(path).read_text("utf-8"))
    ]


class MemoryFeatureError(Exception):
    """Диагностический код без DSN, SQL, текста источников и ошибок сервера."""


class Store:
    def __init__(self, settings: MemorySettings) -> None:
        self.settings = settings

    @contextmanager
    def connect(self) -> Iterator[Any]:
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise MemoryFeatureError("memory_extra_missing") from exc
        dsn = os.environ.get(self.settings.dsn_env, "")
        if not dsn:
            raise MemoryFeatureError("memory_dsn_missing")
        try:
            parameters = conninfo_to_dict(dsn)
            if (
                parameters.get("host") not in {"127.0.0.1", "localhost", "::1"}
                or parameters.get("hostaddr", parameters["host"])
                not in {"127.0.0.1", "localhost", "::1"}
                or "service" in parameters
                or "options" in parameters
            ):
                raise MemoryFeatureError("memory_loopback_required")
            with psycopg.connect(
                dsn,
                connect_timeout=3,
                row_factory=dict_row,
                options="-c statement_timeout=15000 -c lock_timeout=5000",
            ) as connection:
                yield connection
        except psycopg.Error as exc:
            raise MemoryFeatureError("memory_database_failed") from exc

    def migrate(self) -> dict[str, Any]:
        migrations = _migration_scripts()
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(1630982761)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS lr_migrations (version integer PRIMARY KEY, "
                "checksum text NOT NULL, applied_at timestamptz DEFAULT clock_timestamp())"
            )
            rows = db.execute(
                "SELECT version, checksum FROM lr_migrations ORDER BY version"
            ).fetchall()
            if len(rows) > len(migrations):
                raise MemoryFeatureError("memory_migration_mismatch")
            for index, row in enumerate(rows):
                version, _script, checksum = migrations[index]
                if row != {"version": version, "checksum": checksum}:
                    raise MemoryFeatureError("memory_migration_mismatch")
            for version, script, checksum in migrations[len(rows) :]:
                db.execute(script)
                db.execute(
                    "INSERT INTO lr_migrations(version, checksum) VALUES (%s, %s)",
                    (version, checksum),
                )
        version, _script, checksum = migrations[-1]
        return {"status": "ready", "schema_version": version, "checksum": checksum}

    def health(self) -> dict[str, Any]:
        migrations = _migration_scripts()
        expected = [
            {"version": version, "checksum": checksum} for version, _script, checksum in migrations
        ]
        with self.connect() as db:
            exists = db.execute(
                "SELECT to_regclass('lr_migrations') IS NOT NULL AS present"
            ).fetchone()["present"]
            if not exists:
                raise MemoryFeatureError("memory_migration_required")
            migration = db.execute("SELECT version, checksum FROM lr_migrations").fetchall()
            if migration != expected:
                raise MemoryFeatureError("memory_migration_required")
            extension = db.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ).fetchone()
            if extension is None:
                raise MemoryFeatureError("memory_vector_missing")
            server = db.execute("SHOW server_version").fetchone()["server_version"]
        return {
            "status": "ready",
            "schema_version": migrations[-1][0],
            "pgvector": extension["extversion"],
            "postgresql": server,
        }

    def begin(self, run_id: str, bindings: dict[str, str], dataset: str, provenance: str) -> None:
        run_id = str(UUID(run_id))
        if dataset not in {"operational", "synthetic", "train", "dev", "test", "holdout"}:
            raise MemoryFeatureError("memory_dataset_invalid")
        if not bindings or provenance not in {"live", "historical"}:
            raise MemoryFeatureError("memory_provenance_required")
        bindings = {root: project_uuid(value) for root, value in bindings.items()}
        if len(set(bindings.values())) != len(bindings):
            raise MemoryFeatureError("memory_project_binding_ambiguous")
        with self.connect() as db:
            for project in sorted(set(bindings.values())):
                db.execute(
                    "INSERT INTO lr_projects(id) VALUES (%s) ON CONFLICT DO NOTHING", (project,)
                )
            db.execute(
                "INSERT INTO lr_runs(id,bindings,dataset,provenance) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (run_id, self.json(bindings), dataset, provenance),
            )
            row = db.execute("SELECT * FROM lr_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if (
                row["bindings"] != bindings
                or row["dataset"] != dataset
                or row["provenance"] != provenance
            ):
                raise MemoryFeatureError("memory_run_identity_conflict")

    @staticmethod
    def json(value: Any) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(value)

    @staticmethod
    def _enqueue(db: Any, finding: str, revision: int, generation: str = "1") -> None:
        identifier = digest([finding, revision, generation])
        db.execute(
            "INSERT INTO lr_jobs(id,finding_id,revision) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (identifier, finding, revision),
        )

    def ingest(
        self, run_id: str, report: dict[str, Any], diagnostics: dict[str, Any]
    ) -> dict[str, Any]:
        # Полный отчёт сохраняется с собственными отказами и покрытием; секретные скаляры удалены.
        cleaned = sanitize(report)
        canonical = dict(report)
        canonical["analysis"] = dict(report["analysis"])
        canonical["analysis"]["engines"] = [
            {key: value for key, value in engine.items() if key not in {"elapsed_ms", "tokens"}}
            for engine in report["analysis"]["engines"]
        ]
        fingerprint = digest(canonical)
        with self.connect() as db:
            run = db.execute("SELECT * FROM lr_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if run is None:
                raise MemoryFeatureError("memory_run_unknown")
            if run["report_hash"] is not None:
                if run["report_hash"] != fingerprint:
                    raise MemoryFeatureError("memory_delivery_conflict")
                return {
                    "status": "committed",
                    "run_id": run_id,
                    "duplicate": True,
                    "findings": len(report["items"]),
                }
            bindings = run["bindings"]
            documents = {doc["id"]: doc for doc in report["analysis"]["documents"]}
            if set(bindings) != {root["id"] for root in report["analysis"]["roots"]}:
                raise MemoryFeatureError("memory_project_binding_mismatch")
            for doc in cleaned["analysis"]["documents"]:
                db.execute(
                    "INSERT INTO lr_documents(run_id,id,project_id,payload) VALUES (%s,%s,%s,%s)",
                    (run_id, doc["id"], bindings[doc["root"]], self.json(doc)),
                )
            for original, item in zip(report["items"], cleaned["items"], strict=True):
                doc = documents[original["document_id"]]
                identity = {
                    "project": bindings[doc["root"]],
                    "path": doc["path"],
                    "source_sha256": doc["source_sha256"],
                    "range": original["range"],
                    "category": original["category"],
                    "excerpt": original["excerpt"],
                    "evidence": sorted(
                        [
                            {
                                key: evidence[key]
                                for key in ("engine", "rule_id", "category", "range")
                            }
                            for evidence in original["evidence"]
                        ],
                        key=digest,
                    ),
                    "configuration": doc["configuration"],
                    "policy_sha256": diagnostics.get("policy_sha256", {}).get(doc["id"]),
                    "engines": [engine["details"] for engine in canonical["analysis"]["engines"]],
                    "dataset": run["dataset"],
                }
                finding = digest(identity)
                db.execute(
                    "INSERT INTO lr_findings(id,project_id,identity,payload,dataset) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        finding,
                        bindings[doc["root"]],
                        self.json(sanitize(identity)),
                        self.json(item),
                        run["dataset"],
                    ),
                )
                db.execute(
                    "INSERT INTO lr_occurrences(run_id,item_id,finding_id,payload) "
                    "VALUES (%s,%s,%s,%s)",
                    (run_id, item["id"], finding, self.json(item)),
                )
                db.execute(
                    "INSERT INTO lr_events(id,run_id,finding_id,kind,payload) "
                    "VALUES (%s,%s,%s,'observation',%s) ON CONFLICT DO NOTHING",
                    (digest([run_id, finding]), run_id, finding, self.json(item)),
                )
                for index, evidence in enumerate(item["evidence"]):
                    db.execute(
                        "INSERT INTO lr_events(id,run_id,finding_id,kind,payload) "
                        "VALUES (%s,%s,%s,'engine_finding',%s) ON CONFLICT DO NOTHING",
                        (
                            digest([run_id, finding, "engine", index]),
                            run_id,
                            finding,
                            self.json(evidence),
                        ),
                    )
                if run["dataset"] not in {"test", "holdout"}:
                    self._enqueue(db, finding, 0)
            for kind in ("filtered", "unlocalized"):
                for index, event in enumerate(cleaned[kind]):
                    db.execute(
                        "INSERT INTO lr_events(id,run_id,kind,payload) VALUES (%s,%s,%s,%s)",
                        (digest([run_id, kind, index]), run_id, kind, self.json(event)),
                    )
            db.execute(
                "INSERT INTO lr_events(id,run_id,kind,payload) VALUES (%s,%s,'analysis',%s)",
                (digest([run_id, "analysis"]), run_id, self.json(cleaned)),
            )
            db.execute(
                "UPDATE lr_runs SET report_hash=%s,report=%s,status='committed',"
                "diagnostics=%s,completed_at=clock_timestamp() WHERE id=%s",
                (fingerprint, self.json(cleaned), self.json(sanitize(diagnostics)), run_id),
            )
        return {
            "status": "committed",
            "run_id": run_id,
            "duplicate": False,
            "findings": len(report["items"]),
        }

    def decide(
        self, project: str, run_id: str, item_id: str, decision: dict[str, Any]
    ) -> dict[str, Any]:
        required = {
            "id",
            "expected_revision",
            "status",
            "actor",
            "reason",
            "suggestion",
            "scope",
            "public_rule",
        }
        if set(decision) != required or decision["status"] not in {
            "accepted",
            "rejected",
            "disputed",
        }:
            raise MemoryFeatureError("memory_decision_invalid")
        if decision["scope"] not in {"project", "global"}:
            raise MemoryFeatureError("memory_decision_scope_invalid")
        try:
            decision["id"] = str(UUID(decision["id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise MemoryFeatureError("memory_decision_id_invalid") from exc
        for key in ("actor", "reason"):
            value = decision[key]
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 2000
                or sanitize(value) != value
            ):
                raise MemoryFeatureError("memory_decision_evidence_required")
        if type(decision["expected_revision"]) is not int or decision["expected_revision"] < 0:
            raise MemoryFeatureError("memory_decision_revision_required")
        public = decision["public_rule"]
        if decision["scope"] == "global" and (
            decision["status"] != "accepted"
            or not isinstance(public, str)
            or not 1 <= len(public) <= 1000
            or sanitize(public) != public
        ):
            raise MemoryFeatureError("memory_public_rule_required")
        if decision["scope"] == "project" and public is not None:
            raise MemoryFeatureError("memory_public_rule_scope_conflict")
        with self.connect() as db:
            row = db.execute(
                "SELECT f.*,o.payload AS observed_payload FROM lr_findings f "
                "JOIN lr_occurrences o ON f.id=o.finding_id "
                "WHERE o.run_id=%s AND o.item_id=%s AND f.project_id=%s FOR UPDATE OF f",
                (run_id, item_id, project_uuid(project)),
            ).fetchone()
            if row is None:
                raise MemoryFeatureError("memory_finding_not_in_project")
            if row["dataset"] in {"test", "holdout"}:
                raise MemoryFeatureError("memory_evaluation_leakage_refused")
            if row["dataset"] != "operational" and decision["scope"] == "global":
                raise MemoryFeatureError("memory_global_requires_operational_evidence")
            event = {**decision, "finding_id": row["id"], "run_id": run_id, "item_id": item_id}
            previous = db.execute(
                "SELECT payload FROM lr_events WHERE id=%s", (decision["id"],)
            ).fetchone()
            if previous:
                if previous["payload"] != event:
                    raise MemoryFeatureError("memory_decision_delivery_conflict")
                return {"status": "committed", "duplicate": True, "revision": row["revision"]}
            if row["revision"] != decision["expected_revision"]:
                raise MemoryFeatureError("memory_decision_revision_conflict")
            suggestion = decision["suggestion"]
            if suggestion is not None and (
                type(suggestion) is not int
                or decision["status"] != "accepted"
                or not 0 <= suggestion < len(row["observed_payload"]["suggestions"])
                or row["observed_payload"]["suggestions"][suggestion]["status"] != "candidate"
            ):
                raise MemoryFeatureError("memory_decision_suggestion_invalid")
            revision = row["revision"] + 1
            db.execute(
                "INSERT INTO lr_decisions(id,finding_id,revision,status,actor,reason,"
                "suggestion,scope) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    decision["id"],
                    row["id"],
                    revision,
                    decision["status"],
                    decision["actor"],
                    decision["reason"],
                    suggestion,
                    decision["scope"],
                ),
            )
            db.execute(
                "INSERT INTO lr_events(id,finding_id,kind,payload) VALUES (%s,%s,'decision',%s)",
                (decision["id"], row["id"], self.json(event)),
            )
            db.execute(
                "UPDATE lr_findings SET state=%s,revision=%s WHERE id=%s",
                (decision["status"], revision, row["id"]),
            )
            # Старые производные знания сразу перестают участвовать в recall, до LLM.
            db.execute(
                "UPDATE lr_rules SET state='superseded' WHERE finding_id=%s OR "
                "payload->'evidence' @> %s::jsonb",
                (row["id"], self.json([row["id"]])),
            )
            item = row["observed_payload"]
            kinds = sorted({entry["kind"] for entry in item["context"]})
            payload = {
                "excerpt": item["excerpt"],
                "reason": decision["reason"],
                "evidence": [row["id"]],
                "decision_id": decision["id"],
                "suggestion": item["suggestions"][suggestion] if suggestion is not None else None,
                "scope": "project",
            }
            self._rule(
                db,
                digest([row["id"], "case"]),
                row["project_id"],
                row["id"],
                decision["id"],
                "case",
                decision["status"],
                revision,
                item["category"],
                ",".join(kinds),
                f"{item['excerpt']} — {decision['reason']}",
                payload,
                row["dataset"],
            )
            if decision["scope"] == "global":
                self._rule(
                    db,
                    digest([row["id"], "global"]),
                    None,
                    row["id"],
                    decision["id"],
                    "case",
                    "accepted",
                    revision,
                    item["category"],
                    ",".join(kinds),
                    public,
                    {
                        "scope": "global",
                        "evidence": [row["id"]],
                        "decision_id": decision["id"],
                        "public_rule": public,
                    },
                    row["dataset"],
                )
            self._enqueue(db, row["id"], revision)
        return {"status": "committed", "duplicate": False, "revision": revision}

    def _rule(
        self,
        db: Any,
        identifier: str,
        project: Any,
        finding: str,
        decision: str | None,
        kind: str,
        state: str,
        revision: int,
        category: str,
        context: str,
        text: str,
        payload: dict[str, Any],
        dataset: str,
    ) -> None:
        db.execute(
            "INSERT INTO lr_rules(id,project_id,finding_id,decision_id,kind,state,revision,"
            "category,"
            "context_kind,text,payload,dataset) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET state=EXCLUDED.state,revision=EXCLUDED.revision,"
            "text=EXCLUDED.text,payload=EXCLUDED.payload,decision_id=EXCLUDED.decision_id,"
            "updated_at=clock_timestamp()",
            (
                identifier,
                project,
                finding,
                decision,
                kind,
                state,
                revision,
                category,
                context,
                text,
                self.json(payload),
                dataset,
            ),
        )
        db.execute(
            "INSERT INTO lr_rule_history(rule_id,revision,payload) VALUES (%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (identifier, revision, self.json({"state": state, "text": text, "payload": payload})),
        )

    def status(self, project: str) -> dict[str, Any]:
        project = project_uuid(project)
        with self.connect() as db:
            counts = db.execute(
                "SELECT state,count(*) AS count FROM lr_findings "
                "WHERE project_id=%s GROUP BY state",
                (project,),
            ).fetchall()
            jobs = db.execute(
                "SELECT j.state,count(*) AS count,sum(j.attempts) AS attempts "
                "FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
                "WHERE f.project_id=%s GROUP BY j.state",
                (project,),
            ).fetchall()
            rules = db.execute(
                "SELECT state,count(*) AS count FROM lr_rules WHERE project_id=%s GROUP BY state",
                (project,),
            ).fetchall()
            runs = db.execute(
                "SELECT r.status,count(*) AS count FROM lr_runs r WHERE "
                "EXISTS (SELECT 1 FROM jsonb_each_text(r.bindings) b WHERE b.value=%s) "
                "GROUP BY r.status",
                (project,),
            ).fetchall()
            events = db.execute(
                "SELECT count(*) AS count FROM lr_events e WHERE "
                "e.finding_id IN (SELECT id FROM lr_findings WHERE project_id=%s) OR "
                "(e.kind='analysis' AND e.run_id IN (SELECT id FROM lr_runs WHERE EXISTS "
                "(SELECT 1 FROM jsonb_each_text(bindings) b WHERE b.value=%s))) OR "
                "EXISTS (SELECT 1 FROM lr_documents d WHERE d.run_id=e.run_id "
                "AND d.id=e.payload->>'document_id' AND d.project_id=%s)",
                (project, project, project),
            ).fetchone()["count"]
            errors = db.execute(
                "SELECT j.id,j.state,j.error,j.attempts FROM lr_jobs j "
                "JOIN lr_findings f ON f.id=j.finding_id WHERE f.project_id=%s "
                "AND j.error IS NOT NULL ORDER BY j.created_at DESC LIMIT 20",
                (project,),
            ).fetchall()
            indexing = db.execute(
                "SELECT count(*) AS rules, count(e.rule_id) AS indexed "
                "FROM lr_rules r LEFT JOIN lr_embeddings e ON e.rule_id=r.id "
                "AND e.space_id=%s AND e.text_hash=md5(r.text) "
                "WHERE r.project_id=%s AND r.state!='superseded'",
                (self.space_id, project),
            ).fetchone()
            processing = db.execute(
                "SELECT count(*) AS total, count(*) FILTER (WHERE EXISTS "
                "(SELECT 1 FROM lr_jobs j WHERE j.finding_id=f.id AND j.revision=f.revision "
                "AND j.state='done')) AS processed FROM lr_findings f WHERE f.project_id=%s",
                (project,),
            ).fetchone()
            latest_llm = db.execute(
                "SELECT j.result FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
                "WHERE f.project_id=%s AND j.result->>'model' IS NOT NULL "
                "ORDER BY j.created_at DESC LIMIT 1",
                (project,),
            ).fetchone()
        return {
            "status": "ready",
            "runs": runs,
            "durable_events": events,
            "findings": counts,
            "jobs": jobs,
            "rules": rules,
            "recent_errors": errors,
            "index": indexing,
            "processing": processing,
            "latest_consolidation": latest_llm["result"] if latest_llm else None,
            "embedding": {
                "space": self.space_id,
                "model": self.settings.embedding_model,
                "revision": self.settings.embedding_revision,
                "dimensions": self.settings.embedding_dimensions,
            },
        }

    @property
    def space_id(self) -> str:
        return digest(
            [
                self.settings.embedding_model,
                self.settings.embedding_revision,
                self.settings.embedding_dimensions,
                "memory-embedding/1",
            ]
        )

    def claim(self, project: str | None, *, lease_seconds: int = 300) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute(
                "UPDATE lr_jobs j SET state='failed',error='attempts_exhausted' FROM lr_findings f "
                "WHERE j.finding_id=f.id AND (%s::uuid IS NULL OR f.project_id=%s) "
                "AND j.state='running' "
                "AND j.lease_until<clock_timestamp() AND j.attempts>=%s",
                (project, project, self.settings.max_attempts),
            )
            row = db.execute(
                "SELECT j.*,f.project_id FROM lr_jobs j JOIN lr_findings f ON f.id=j.finding_id "
                "WHERE (%s::uuid IS NULL OR f.project_id=%s) AND j.attempts<%s AND ("
                "(j.state='pending' AND j.available_at<=clock_timestamp()) OR "
                "(j.state='running' AND j.lease_until<clock_timestamp())) "
                "ORDER BY j.created_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1",
                (project, project, self.settings.max_attempts),
            ).fetchone()
            if row is None:
                return None
            owner = uuid4()
            # Lease учитывает время probe, completion и запас, заданный worker.
            db.execute(
                "UPDATE lr_jobs SET state='running',attempts=attempts+1,owner=%s,"
                "lease_until=clock_timestamp() + %s * interval '1 second' WHERE id=%s",
                (owner, lease_seconds, row["id"]),
            )
            row.update(owner=owner, attempts=row["attempts"] + 1)
            return dict(row)

    def retry(self, project: str) -> int:
        with self.connect() as db:
            return int(
                db.execute(
                    "UPDATE lr_jobs j SET state='pending',attempts=0,error=NULL,"
                    "available_at=clock_timestamp() FROM lr_findings f "
                    "WHERE f.id=j.finding_id AND f.project_id=%s AND j.state='failed'",
                    (project_uuid(project),),
                ).rowcount
            )

    def fail(self, job: dict[str, Any], code: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE lr_jobs SET state=%s,error=%s,owner=NULL,lease_until=NULL,"
                "available_at=clock_timestamp() + %s * interval '1 second' "
                "WHERE id=%s AND owner=%s AND state='running'",
                (
                    "failed" if job["attempts"] >= self.settings.max_attempts else "pending",
                    code,
                    min(300, 2 ** job["attempts"]),
                    job["id"],
                    job["owner"],
                ),
            )
