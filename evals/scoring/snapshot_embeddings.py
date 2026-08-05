"""dev 전용 임베딩 fixture 생성기(DB·Google API 필요).

실행: ``uv run python -m evals.scoring.snapshot_embeddings``
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from app.core.config import get_settings
from app.pipelines.embedding import embed_texts
from evals.goldenset.loader import load_cases
from evals.metrics.runner import load_evaluation_fixtures
from evals.scoring.embeddings import FIXTURE_PATH


_EMBED_BATCH_MAX = 100  # Google BatchEmbedContentsRequest 상한(초과 시 400 INVALID_ARGUMENT) —
# app/pipelines/embedding.py의 embed_texts는 청크 분할이 없다(app/** 변경은 이 이슈 소관 밖,
# 결함은 발견·보고만 하고 후속 이슈로 이관 — #333 리뷰 라운드3 FR-1). 이 eval 전용 호출부에서만
# 청크로 나눠 순차 호출하고 입력 순서대로 이어붙인다.


def _truncate(vector: list[float]) -> list[float]:
    factor = 100_000_000
    return [math.trunc(value * factor) / factor for value in vector]


def _embed_texts_chunked(texts: list[str], *, task_type: str | None) -> list[list[float]]:
    """embed_texts를 100건 이하 청크로 나눠 순차 호출하고 입력 순서대로 이어붙인다."""
    out: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH_MAX):
        chunk = texts[start : start + _EMBED_BATCH_MAX]
        out.extend(embed_texts(chunk, task_type=task_type))
    return out


def _source_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def build_snapshot(output: Path = FIXTURE_PATH) -> dict[str, object]:
    settings = get_settings()
    cases = list(load_cases("dev"))
    fixtures = load_evaluation_fixtures()
    product_ids = sorted(
        {
            int(product_id)
            for case in cases
            for product_id in fixtures.search_responses[case.search_fixture_id]["productIds"]
        }
    )
    with psycopg.connect(settings.catalog_db_url) as connection:
        register_vector(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT product_id, embedding FROM product_document "
                "WHERE product_id = ANY(%s) ORDER BY product_id",
                (product_ids,),
            )
            documents = {
                str(product_id): _truncate(
                    [float(value) for value in embedding.to_list()]
                )
                for product_id, embedding in cursor.fetchall()
            }
    query_vectors = _embed_texts_chunked(
        [case.query for case in cases], task_type=settings.embedding_task_query
    )
    queries = {
        case.case_id: _truncate(vector)
        for case, vector in zip(cases, query_vectors, strict=True)
    }
    payload: dict[str, object] = {
        "metadata": {
            "model": settings.embedding_model_id,
            "dim": settings.embedding_dim,
            "taskType": {
                "documents": settings.embedding_task_document,
                "queries": settings.embedding_task_query,
            },
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "sourceCommit": _source_commit(),
            "documentCount": len(documents),
            "queryCount": len(queries),
        },
        "documents": documents,
        "queries": queries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="scoring dev 임베딩 fixture 생성")
    parser.add_argument("--out", type=Path, default=FIXTURE_PATH)
    args = parser.parse_args(argv)
    payload = build_snapshot(args.out)
    metadata = payload["metadata"]
    print(
        f"documents={metadata['documentCount']} queries={metadata['queryCount']} out={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
