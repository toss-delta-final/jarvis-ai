"""#101 방식2 검색 백엔드 Settings 신규 필드 테스트.

pgvector 2차 압축(방식2)을 hot path 기본으로 켜는 토글과 압축 상한이 config 주입되는지 확인.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_search_backend_and_rerank_limit_defaults() -> None:
    """방식2 기본 백엔드 + embedding_rerank_limit 기본값 — 하드코딩 금지, config 주입."""
    s = Settings(_env_file=None)
    assert s.search_backend == "embedding_rerank"  # MVP 기본 = Spring 전량 → pgvector 압축(방식2)
    assert (
        s.embedding_rerank_limit == 30
    )  # pgvector 압축 후 Sonnet 입력 상한(옛 "FastAPI 30" 이관처)


def test_search_backend_literal_rejects_unknown() -> None:
    """search_backend 는 Literal — 미정의 값은 로드 시 거부한다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, search_backend="nonsense")


def test_negative_embedding_rerank_limit_rejected() -> None:
    """embedding_rerank_limit 은 products[:limit] 절단에 쓰이므로 ge=0.

    음수면 Python slice 가 '뒤에서 제외'로 뒤집혀 '<=0 이면 0개' 절단 불변식이 깨진다
    (형제 category_fanout_* 와 동일 규약, PR #73 리뷰).
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_rerank_limit=-1)
