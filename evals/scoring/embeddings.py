"""커밋된 dev 임베딩 fixture 로더."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "embeddings_dev.json"


def _normalize(vector: list[float], expected_dim: int) -> list[float]:
    if len(vector) != expected_dim:
        raise ValueError(f"embedding dimension {len(vector)} != {expected_dim}")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise ValueError("zero-norm embedding fixture")
    return [value / norm for value in vector]


@dataclass(frozen=True)
class EmbeddingFixture:
    metadata: dict[str, object]
    documents: dict[int, list[float]]
    queries: dict[str, list[float]]


def load_embeddings(path: Path = FIXTURE_PATH) -> EmbeddingFixture:
    """반올림 저장 오차를 제거하도록 로드 시 모든 벡터를 L2 재정규화한다."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = dict(payload["metadata"])
    dim = int(metadata["dim"])
    return EmbeddingFixture(
        metadata=metadata,
        documents={
            int(product_id): _normalize([float(value) for value in vector], dim)
            for product_id, vector in payload["documents"].items()
        },
        queries={
            case_id: _normalize([float(value) for value in vector], dim)
            for case_id, vector in payload["queries"].items()
        },
    )
