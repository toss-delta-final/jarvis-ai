"""임베딩 파이프라인 — AI 생성물 갱신 배치의 2단계 (MVP, api-spec §4.8 v0.15.14).

I-17 pull 배치 흐름에서 search_doc 를 조립하고 Google `gemini-embedding-001` API(1536-dim,
MRL 절단)로 임베딩해 AI Postgres(artifact_store)에 upsert 한다. [2026-07-20 결정 6 개정]
셀프호스트 torch 임베딩은 폐기 — MRL 절단 응답은 사전 정규화가 안 돼 있어 여기서 수동 L2
정규화한다. 결합 방식(방식1 벡터검색 / 방식2 재정렬)은 SearchBackend 로 양쪽 구현한다
(§4.8 말미, 2026-07-20 결정).

google-genai SDK 는 함수 내부에서 LAZY import 한다(app/core/llm.py AnthropicLLM 과 동일 패턴).
테스트·배치는 embed 콜러블을 주입해 이 경로를 대체한다(주입형) — _client() 는 라이브 호출 seam.
"""

from __future__ import annotations

import math
import time

from app.core.config import get_settings

_monotonic = time.monotonic  # 라이브 호출 seam — 테스트가 emb._monotonic 을 결정론적 fake 로 대체

_CLIENT_CACHE: dict[str, object] = {}

_EMBED_BATCH_MAX = 100  # Google BatchEmbedContentsRequest 상한(초과 시 400 INVALID_ARGUMENT) —
# Google API 자체 상한이라 튜너블 아님(config 주입 금지, 모듈 상수로 고정). #353.


class EmbeddingError(Exception):
    """임베딩 호출 실패(오류/미구성). 상위(배치)는 그대로 전파해 자연 재개(§4.8)."""


def build_search_doc(product: dict) -> str:
    """상품 필드 + extras(tags·attributes)를 결합해 임베딩 대상 search_doc 문자열을 만든다 (§4.8).

    원본 컬럼을 저장하지는 않으나 임베딩 입력 조립에는 사용한다(산출물 계산). 빈 필드는 건너뛴다.
    """
    extras = product.get("extras") if isinstance(product.get("extras"), dict) else {}
    parts: list[str] = []
    for key in ("name", "category", "brand", "description"):
        val = product.get(key)
        if val:
            parts.append(str(val))
    attributes = product.get("attributes")
    if isinstance(attributes, dict):
        parts.extend(f"{k}: {v}" for k, v in attributes.items() if v)
    tags = extras.get("tags")
    if isinstance(tags, list) and tags:
        parts.append(" ".join(str(t) for t in tags))
    extra_attrs = extras.get("attributes")
    if isinstance(extra_attrs, dict):
        parts.extend(f"{k}: {v}" for k, v in extra_attrs.items() if v)
    return "\n".join(parts)


def _client(api_key: str):
    """genai.Client 를 api_key 별로 캐시해 반환한다 (라이브 호출 seam — 테스트가 대체 주입).

    config embedding_timeout_s 를 http_options(밀리초)로 걸어 요청 상한을 둔다 — 방식2가 hot path
    기본이라 임베딩 API 가 느려지면 SSE 가 무기한 대기하는 것을 막는다(PR#166 리뷰). 초과 시 SDK 가
    예외를 던지고 상위(embed_texts→EmbeddingRerankBackend)가 Spring 순서로 degrade 한다.
    """
    from google import genai  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    if api_key not in _CLIENT_CACHE:
        timeout_ms = int(get_settings().embedding_timeout_s * 1000)
        _CLIENT_CACHE[api_key] = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms)
        )
    return _CLIENT_CACHE[api_key]


def _l2_normalize(vec: list[float]) -> list[float]:
    """MRL 절단 응답은 사전 정규화가 안 돼 있으므로 수동 L2 정규화한다 (§4.8 v0.15.14)."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def embed_texts(
    texts: list[str], *, task_type: str | None = None, total_timeout_s: float | None = None
) -> list[list[float]]:
    """텍스트 목록을 Google gemini-embedding-001 API 로 임베딩한다 (§4.8).

    config.embedding_dim 을 output_dimensionality 로 요청하고, 응답을 수동 L2 정규화한다.
    task_type 지정 시 비대칭 검색용으로 전달한다(문서=RETRIEVAL_DOCUMENT / 질의=RETRIEVAL_QUERY).
    google_api_key 미구성 시 곧바로 EmbeddingError — 배치·테스트는 embed 콜러블을 주입한다.
    입력이 `_EMBED_BATCH_MAX`(100건)를 넘으면 청크로 나눠 순차 호출하고 순서대로 이어붙인다(#353).

    `total_timeout_s` 는 이 호출 1회 전체의 벽시계 예산이다(#391) — `embedding_timeout_s` 는
    청크(HTTP 요청) 1건당 상한이라 청크가 여러 개면 총 소요가 누적될 수 있는 것과 구분된다.
    None(기본)이면 `config.embedding_total_timeout_s` 를 쓰고, `math.inf` 를 주면 예산 없이
    전 청크를 낸다(오프라인 1회 빌드용 명시 opt-out — category_seed.seed_from_file). 그 외 숫자를
    주면 config 기본값을 덮어쓴다. **`embedding_timeout_s` 이상이어야 하며, 미만이면 `ValueError`**
    (config 쪽 기동 검증기와 같은 불변식을 인자 경로에도 강제 — #391 PR #412). 첫 청크는 예산과
    무관하게 항상 시도하고(빈 입력이면 호출 0회),
    두 번째 이후 청크는 내기 전에 `경과 + embedding_timeout_s > 예산` 이면 EmbeddingError 를 던져
    청크를 내지 않는다 — 이 규칙으로 총 벽시계는 `max(embedding_timeout_s, total_timeout_s)`
    이하로 유계다(SDK 가 자기 타임아웃을 지킨다는 전제). 예산 초과는 `EmbeddingError` →
    `EmbeddingRerankBackend` 가 Spring 순서로 degrade한다(#101 #7, PR#166).
    """
    settings = get_settings()
    if not settings.google_api_key:
        raise EmbeddingError("embed_texts: google_api_key 미구성 — Google 임베딩 API 호출 불가")

    budget = settings.embedding_total_timeout_s if total_timeout_s is None else total_timeout_s
    per_request_timeout = settings.embedding_timeout_s
    if budget < per_request_timeout:
        raise ValueError(
            f"embed_texts: total_timeout_s={budget} 는 embedding_timeout_s={per_request_timeout} "
            "미만일 수 없다 — 미만이면 1청크 호출(hot path 전부)은 예산 검사(idx==0)가 건너뛰어져 "
            "값이 무효고, 다중 청크 호출은 정상 상황에서도 상시 거부된다(config 쪽 "
            "_require_embedding_total_timeout_covers_request_timeout 과 같은 불변식, #391 PR #412)"
        )

    from google.genai import types  # noqa: PLC0415

    client = _client(settings.google_api_key)
    chunks = [
        texts[start : start + _EMBED_BATCH_MAX] for start in range(0, len(texts), _EMBED_BATCH_MAX)
    ]
    try:
        raw: list[list[float]] = []
        start_time = _monotonic()
        for idx, chunk in enumerate(chunks):
            if idx > 0:
                elapsed = _monotonic() - start_time
                if elapsed + per_request_timeout > budget:
                    raise EmbeddingError(
                        f"embed_texts: 총 시간 예산 초과 — 입력 {len(texts)}건/{len(chunks)}청크 중 "
                        f"{idx}청크 완료, 경과 {elapsed:.2f}s + 요청당 {per_request_timeout:.2f}s "
                        f"> 예산 {budget:.2f}s (embedding_total_timeout_s)"
                    )
            response = client.models.embed_content(
                model=settings.embedding_model_id,
                contents=chunk,
                config=types.EmbedContentConfig(
                    output_dimensionality=settings.embedding_dim,
                    **({"task_type": task_type} if task_type else {}),
                ),
            )
            raw.extend([float(x) for x in item.values] for item in response.embeddings)
        # settings.embedding_normalized 를 실제 분기 조건으로 사용 — 기록되는 normalized
        # 프로비넌스와 실제 정규화 동작이 어긋나지 않게 한다(이슈 #65 PR 리뷰).
        out = [_l2_normalize(vec) for vec in raw] if settings.embedding_normalized else raw
    except EmbeddingError:
        raise
    except Exception as exc:  # noqa: BLE001 - SDK 호출·응답 파싱 예외를 EmbeddingError 로 통일 매핑
        raise EmbeddingError(str(exc)) from exc

    for vec in out:
        if len(vec) != settings.embedding_dim:
            raise ValueError(f"임베딩 차원 불일치: {len(vec)} != {settings.embedding_dim}")
    return out
