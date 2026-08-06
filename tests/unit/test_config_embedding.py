"""임베딩/카탈로그 배치 Settings 신규 필드 테스트 (이슈 #31, api-spec §4.8 v0.15.14).

Google gemini-embedding-001 API 전환 이후 값(dim 1536)이 기본값으로 로드되는지 확인한다.
"""

from __future__ import annotations

import pydantic
import pytest

from app.core.config import Settings


def test_embedding_settings_reflect_google_api() -> None:
    """[2026-07-20 결정] 셀프호스트 torch → Google gemini-embedding-001, dim 1024→1536."""
    settings = Settings(_env_file=None)
    assert settings.embedding_model_id == "gemini-embedding-001"
    assert settings.embedding_dim == 1536
    assert settings.google_api_key == ""


def test_catalog_batch_interval_default() -> None:
    """주기 증분 pull 배치 스케줄러 간격(초) — config 주입, 하드코딩 금지."""
    settings = Settings(_env_file=None)
    assert settings.catalog_batch_interval_s == 300.0


def test_embedding_total_timeout_must_be_at_least_request_timeout() -> None:
    """#391 PR #412 Claude 리뷰 — 총 예산이 요청당 상한보다 작으면 기동 실패.

    작게 잡으면 1청크 호출(hot path 전부)은 idx==0 이라 예산 검사를 건너뛰어 설정이 무효고,
    다중 청크 호출은 정상 상황에서도 두 번째 청크에서 거의 항상 거부된다.
    """
    with pytest.raises(pydantic.ValidationError) as exc_info:
        Settings(_env_file=None, embedding_timeout_s=3.0, embedding_total_timeout_s=1.0)

    message = str(exc_info.value)
    assert "EMBEDDING_TOTAL_TIMEOUT_S" in message
    assert "EMBEDDING_TIMEOUT_S" in message


def test_embedding_total_timeout_equal_to_request_timeout_is_allowed() -> None:
    """경계(같은 값)는 허용 — 기본값 3.0 == 3.0 이 "hot path 는 청크 1개분"이라는 의도된 조합이다.

    부등호를 `<` → `<=` 로 바꾸면 이 테스트가 깨진다.
    """
    settings = Settings(_env_file=None, embedding_timeout_s=3.0, embedding_total_timeout_s=3.0)
    assert settings.embedding_total_timeout_s == settings.embedding_timeout_s


def test_embedding_total_timeout_default_combination_boots() -> None:
    """기본값 조합(둘 다 3.0)이 이 검증기를 통과해 기동 가능함을 못 박는다."""
    settings = Settings(_env_file=None)
    assert settings.embedding_total_timeout_s >= settings.embedding_timeout_s
