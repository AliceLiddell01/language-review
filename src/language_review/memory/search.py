from __future__ import annotations

import json
import math
import re
from typing import Any

from language_review.engine_http import EngineHttpError, join_url, request_json
from language_review.memory.safety import safe_text
from language_review.memory.store import MemoryError, Store


class Embedder:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.verified = False

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        settings = self.store.settings
        if not settings.embedding_model or not settings.embedding_revision:
            raise MemoryError("memory_embedding_not_configured")
        try:
            if not self.verified:
                tags = request_json(
                    join_url(settings.embedding_endpoint, "/api/tags"),
                    timeout=settings.timeout_seconds,
                ).body
                models = tags.get("models", []) if isinstance(tags, dict) else []
                if not isinstance(models, list):
                    raise MemoryError("memory_embedding_schema_invalid")
                if not any(
                    isinstance(item, dict)
                    and item.get("name") == settings.embedding_model
                    and item.get("digest") == settings.embedding_revision
                    for item in models
                ):
                    raise MemoryError("memory_embedding_revision_mismatch")
                self.verified = True
            # Официальные asymmetric retrieval-префиксы EmbeddingGemma и Qwen3.
            if settings.embedding_model.startswith("embeddinggemma"):
                prefix = "task: search result | query: " if query else "title: none | text: "
            elif settings.embedding_model.startswith("qwen3-embedding"):
                prefix = (
                    "Instruct: Retrieve relevant Russian technical language decisions.\nQuery: "
                    if query
                    else ""
                )
            else:
                raise MemoryError("memory_embedding_model_not_evaluated")
            body = request_json(
                join_url(settings.embedding_endpoint, "/api/embed"),
                payload={
                    "model": settings.embedding_model,
                    "input": [prefix + safe_text(text)[:6000] for text in texts],
                    "truncate": False,
                },
                timeout=settings.timeout_seconds,
            ).body
        except EngineHttpError as exc:
            raise MemoryError(f"memory_embedding_{exc.code}") from exc
        vectors = body.get("embeddings") if isinstance(body, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise MemoryError("memory_embedding_schema_invalid")
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != settings.embedding_dimensions
                or not all(
                    type(number) in {float, int} and math.isfinite(number) for number in vector
                )
                or not any(vector)
            ):
                raise MemoryError("memory_embedding_schema_invalid")
        return [[float(number) for number in vector] for vector in vectors]


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


def index_rules(
    store: Store, project: str, *, limit: int = 32, rebuild: bool = False
) -> dict[str, Any]:
    if not 1 <= limit <= 256:
        raise MemoryError("memory_index_limit_invalid")
    with store.connect() as db:
        if rebuild:
            db.execute(
                "DELETE FROM lr_embeddings e USING lr_rules r WHERE r.id=e.rule_id "
                "AND (r.project_id=%s OR (r.project_id IS NULL AND EXISTS "
                "(SELECT 1 FROM lr_findings f WHERE f.id=r.finding_id AND f.project_id=%s))) "
                "AND e.space_id=%s",
                (project, project, store.space_id),
            )
        rules = db.execute(
            "SELECT r.* FROM lr_rules r LEFT JOIN lr_embeddings e ON e.rule_id=r.id "
            "AND e.space_id=%s WHERE (r.project_id=%s OR (r.project_id IS NULL AND EXISTS "
            "(SELECT 1 FROM lr_findings f WHERE f.id=r.finding_id AND f.project_id=%s))) "
            "AND r.state!='superseded' "
            "AND r.dataset NOT IN ('test','holdout') AND "
            "(e.rule_id IS NULL OR e.text_hash!=md5(r.text)) "
            "ORDER BY r.updated_at,r.id LIMIT %s",
            (store.space_id, project, project, limit),
        ).fetchall()
    if not rules:
        return {"indexed": 0, "space": store.space_id}
    embedder = Embedder(store)
    vectors = embedder.embed([row["text"] for row in rules])
    with store.connect() as db:
        settings = store.settings
        db.execute(
            "INSERT INTO lr_embedding_spaces(id,model,revision,dimensions) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (
                store.space_id,
                settings.embedding_model,
                settings.embedding_revision,
                settings.embedding_dimensions,
            ),
        )
        indexed = 0
        for row, vector in zip(rules, vectors, strict=True):
            # Revision fence: решение могло измениться во время запроса embedding.
            current = db.execute(
                "SELECT * FROM lr_rules WHERE id=%s FOR UPDATE", (row["id"],)
            ).fetchone()
            if current["state"] == "superseded" or current["text"] != row["text"]:
                continue
            literal = vector_literal(vector)
            db.execute(
                "INSERT INTO lr_embeddings(rule_id,space_id,text_hash,embedding) "
                "VALUES (%s,%s,md5(%s),%s::vector) ON CONFLICT(rule_id,space_id) "
                "DO UPDATE SET text_hash=EXCLUDED.text_hash,embedding=EXCLUDED.embedding",
                (row["id"], store.space_id, row["text"], literal),
            )
            # Вектор должен действительно находиться через pgvector, до подтверждения индексации.
            found = db.execute(
                "SELECT 1 FROM lr_embeddings WHERE rule_id=%s AND space_id=%s "
                "AND embedding <=> %s::vector < 0.0001",
                (row["id"], store.space_id, literal),
            ).fetchone()
            if found is None:
                raise MemoryError("memory_index_readback_failed")
            indexed += 1
    return {"indexed": indexed, "space": store.space_id}


def recall(
    store: Store,
    project: str,
    text: str,
    *,
    kind: str | None = None,
    dataset: str = "operational",
    hybrid: bool = True,
    limit: int = 5,
    include_hypotheses: bool = False,
) -> dict[str, Any]:
    if not 1 <= limit <= 20:
        raise MemoryError("memory_recall_limit_invalid")
    text = safe_text(text)[:6000]
    if text == "[REDACTED]" or not text.strip():
        return {"results": [], "reason": "sensitive_or_empty_query", "mode": "none"}
    # Dev/test не учатся на предыдущих повторах своего split. Синтетический
    # опыт доступен только отдельному явно выбранному диагностическому dataset.
    datasets = ["synthetic"] if dataset == "synthetic" else ["operational", "train"]
    lexical_query = " OR ".join(re.findall(r"\w+", text)[:80])
    query_vector = None
    reason = None
    if hybrid:
        try:
            query_vector = vector_literal(Embedder(store).embed([text], query=True)[0])
        except MemoryError as exc:
            reason = str(exc)
    states = ["accepted", "rejected", "disputed"] + (["hypothesis"] if include_hypotheses else [])
    # Оба ранжирования ограничены одной и той же областью до вычисления similarity.
    with store.connect() as db:
        rows = db.execute(
            "WITH visible AS NOT MATERIALIZED (SELECT r.*,e.embedding FROM lr_rules r "
            "LEFT JOIN lr_embeddings e ON e.rule_id=r.id AND e.space_id=%(space)s "
            "AND e.text_hash=md5(r.text) WHERE (r.project_id=%(project)s OR r.project_id IS NULL) "
            "AND r.state=ANY(%(states)s) AND r.dataset=ANY(%(datasets)s) "
            "AND (%(kind)s::text IS NULL OR %(kind)s=ANY(string_to_array(r.context_kind,',')))), "
            "keywords AS (SELECT id FROM visible WHERE search @@ "
            "(websearch_to_tsquery('russian',%(query)s) || "
            "websearch_to_tsquery('simple',%(query)s)) ORDER BY ts_rank_cd(search, "
            "websearch_to_tsquery('russian',%(query)s) || "
            "websearch_to_tsquery('simple',%(query)s)) DESC,id LIMIT 50), "
            "vectors AS (SELECT id FROM visible WHERE %(vector)s::vector IS NOT NULL "
            "AND embedding IS NOT NULL AND embedding <=> %(vector)s::vector < 0.4 "
            "ORDER BY embedding <=> %(vector)s::vector,id LIMIT 50), "
            "candidates AS (SELECT id FROM keywords UNION SELECT id FROM vectors) "
            "SELECT v.*,ts_rank_cd(v.search,websearch_to_tsquery('russian',%(query)s) || "
            "websearch_to_tsquery('simple',%(query)s)) AS lexical, "
            "CASE WHEN %(vector)s::vector IS NOT NULL AND v.embedding IS NOT NULL "
            "THEN 1-(v.embedding <=> %(vector)s::vector) ELSE NULL END AS semantic "
            "FROM visible v JOIN candidates c USING(id) "
            "ORDER BY lexical DESC,semantic DESC NULLS LAST,v.id",
            {
                "query": lexical_query,
                "vector": query_vector,
                "space": store.space_id,
                "project": project,
                "states": states,
                "datasets": datasets,
                "kind": kind,
            },
        ).fetchall()
    # Exact terms (CLI/HTTP/код) дополняют русскую морфологию; RRF объединяет ранги.
    tokens = set(re.findall(r"[\w.-]+", text.casefold()))
    exact = sorted(
        rows,
        key=lambda row: (
            -len(tokens & set(re.findall(r"[\w.-]+", row["text"].casefold()))),
            row["id"],
        ),
    )
    rankings = [
        [row for row in rows if row["lexical"] > 0],
        [row for row in exact if tokens & set(re.findall(r"[\w.-]+", row["text"].casefold()))],
    ]
    if query_vector:
        rankings.append(
            sorted(
                [row for row in rows if (row["semantic"] or 0) > 0.6],
                key=lambda row: (-row["semantic"], row["id"]),
            )
        )
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, 1):
            scores[row["id"]] = scores.get(row["id"], 0) + 1 / (60 + rank)
    ordered = sorted(rows, key=lambda row: (-scores[row["id"]], row["id"]))[:limit]
    results = []
    budget = store.settings.context_chars
    for row in ordered:
        # Global: только явно опубликованный человеком текст, без частного payload/истории.
        public = row["project_id"] is None
        evidence = row["payload"].get("evidence", [])
        entry = {
            "id": row["id"],
            "state": row["state"],
            "kind": row["kind"],
            "scope": "global" if public else "project",
            "category": row["category"],
            "text": safe_text(row["text"]),
            "evidence": evidence,
            "decision_id": str(row["decision_id"]) if row["decision_id"] else None,
            "revision": row["revision"],
            "score": scores[row["id"]],
            "semantic": row["semantic"],
            "advisory_only": True,
        }
        size = len(json.dumps(entry, ensure_ascii=False))
        if size > budget:
            continue
        results.append(entry)
        budget -= size
    return {
        "results": results,
        "mode": "hybrid" if query_vector else "structured",
        "reason": reason or ("no_applicable_memory" if not results else None),
        "space": store.space_id if query_vector else None,
    }
