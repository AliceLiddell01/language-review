"""Digest всех таблиц изолированной БД для проверки реального backup/restore."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from psycopg import sql

from language_review.memory.safety import digest
from language_review.memory.settings import MemorySettings
from language_review.memory.store import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect", type=Path)
    arguments = parser.parse_args()
    store = Store(MemorySettings(dsn_env="LANGUAGE_REVIEW_MEMORY_TEST_DSN"))
    with store.connect() as db:
        tables = db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename LIKE 'lr_%' ORDER BY tablename"
        ).fetchall()
        result = {}
        for table in tables:
            name = table["tablename"]
            rows = db.execute(
                sql.SQL("SELECT to_jsonb(t) AS data FROM {} t").format(sql.Identifier(name))
            ).fetchall()
            values = sorted((row["data"] for row in rows), key=digest)
            result[name] = {"rows": len(values), "sha256": digest(values)}
        # Действительный pgvector distance read-back, не только наличие строк индекса.
        vectors = db.execute(
            "SELECT count(*) AS count FROM lr_embeddings WHERE embedding <=> embedding < 0.0001"
        ).fetchone()["count"]
        result["vector_self_retrieval"] = vectors
    if arguments.expect:
        expected = json.loads(arguments.expect.read_text("utf-8"))
        if result != expected:
            raise SystemExit("Snapshot не совпал после restart/recreate/restore.")
    with arguments.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
    print(json.dumps({"status": "PASS", "tables": len(tables), "vectors_retrieved": vectors}))


if __name__ == "__main__":
    main()
