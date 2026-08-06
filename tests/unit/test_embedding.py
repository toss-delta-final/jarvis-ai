"""임베딩 클라이언트 유닛 테스트 (이슈 #31, api-spec §4.8 v0.15.14).

google-genai SDK는 _client() 심(seam)을 통해 주입형 fake 로 대체한다 — 라이브 Google API
호출 없이 정규화·차원검증·미구성 오류 경로를 검증한다.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.pipelines import embedding as emb


class _FakeEmbedding:
    def __init__(self, values: list[float]) -> None:
        self.values = values


class _FakeResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.embeddings = [_FakeEmbedding(v) for v in vectors]


class _FakeModels:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors

    def embed_content(self, *, model, contents, config):
        return _FakeResponse(self._vectors)


class _FakeClient:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.models = _FakeModels(vectors)


class _CapturingModels:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.last_config = None

    def embed_content(self, *, model, contents, config):
        self.last_config = config
        return _FakeResponse(self._vectors)


class _CapturingClient:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.models = _CapturingModels(vectors)


def test_embed_texts_calls_google_and_l2_normalizes(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="test-key", embedding_dim=3)
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    monkeypatch.setattr(emb, "_client", lambda api_key: _FakeClient([[3.0, 4.0, 0.0]]))

    out = emb.embed_texts(["hello"])

    assert len(out) == 1
    assert out[0] == pytest.approx([0.6, 0.8, 0.0])  # MRL 절단 응답 수동 L2 정규화(3-4-5)


def test_embed_texts_skips_normalization_when_disabled(monkeypatch):
    # embedding_normalized=False 면 실제로 정규화하지 않는다 — 기록되는 normalized
    # 프로비넌스와 동작이 일치해야 한다(이슈 #65 PR 리뷰).
    settings = Settings(
        _env_file=None, google_api_key="test-key", embedding_dim=3, embedding_normalized=False
    )
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    monkeypatch.setattr(emb, "_client", lambda api_key: _FakeClient([[3.0, 4.0, 0.0]]))

    out = emb.embed_texts(["hello"])

    assert out[0] == pytest.approx([3.0, 4.0, 0.0])  # 원시값 그대로(정규화 안 함)


def test_embed_texts_raises_without_api_key(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="")
    monkeypatch.setattr(emb, "get_settings", lambda: settings)

    with pytest.raises(emb.EmbeddingError):
        emb.embed_texts(["hello"])


def test_embed_texts_dim_mismatch_raises(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="test-key", embedding_dim=4)
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    monkeypatch.setattr(emb, "_client", lambda api_key: _FakeClient([[1.0, 0.0, 0.0]]))

    with pytest.raises(ValueError):
        emb.embed_texts(["hello"])


def test_embed_texts_wraps_malformed_response_parsing_as_embedding_error(monkeypatch):
    """PR #42 리뷰 — 응답 파싱(item.values 접근)이 try 밖에 있으면 예상 밖 응답 형태(세이프티
    필터링 등)가 AttributeError/TypeError 를 원본 그대로 새게 한다. EmbeddingError 로 통일돼야 한다."""

    class _BrokenModels:
        def embed_content(self, *, model, contents, config):
            class _Response:
                embeddings = None  # 순회 시 TypeError

            return _Response()

    class _BrokenClient:
        models = _BrokenModels()

    settings = Settings(_env_file=None, google_api_key="test-key", embedding_dim=3)
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    monkeypatch.setattr(emb, "_client", lambda api_key: _BrokenClient())

    with pytest.raises(emb.EmbeddingError):
        emb.embed_texts(["hello"])


def test_embed_texts_passes_task_type_when_given(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="test-key", embedding_dim=3)
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    client = _CapturingClient([[3.0, 4.0, 0.0]])
    monkeypatch.setattr(emb, "_client", lambda api_key: client)

    emb.embed_texts(["q"], task_type="RETRIEVAL_QUERY")

    assert client.models.last_config.task_type == "RETRIEVAL_QUERY"


def test_embed_texts_omits_task_type_by_default(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="test-key", embedding_dim=3)
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    client = _CapturingClient([[3.0, 4.0, 0.0]])
    monkeypatch.setattr(emb, "_client", lambda api_key: client)

    emb.embed_texts(["d"])

    assert getattr(client.models.last_config, "task_type", None) is None


class _ChunkCapturingModels:
    """호출마다 contents/config 를 기록하고, 각 텍스트 "t{i}" 로부터 결정론적 벡터를 만든다.

    벡터는 contents 리스트 내 인덱스가 아니라 텍스트 자체(정수 i)로부터 유도해, 청크 순서가
    뒤집혀도 우연히 통과하지 않게 한다(순서 보존을 실제 값으로 검증).
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.configs: list[object] = []

    def embed_content(self, *, model, contents, config):
        self.calls.append(list(contents))
        self.configs.append(config)
        vectors = [[float(int(text[1:])), 0.0, 0.0] for text in contents]
        return _FakeResponse(vectors)


class _ChunkCapturingClient:
    def __init__(self) -> None:
        self.models = _ChunkCapturingModels()


def test_embed_texts_chunks_over_100_and_preserves_order(monkeypatch):
    settings = Settings(
        _env_file=None, google_api_key="test-key", embedding_dim=3, embedding_normalized=False
    )
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    client = _ChunkCapturingClient()
    monkeypatch.setattr(emb, "_client", lambda api_key: client)

    texts = [f"t{i}" for i in range(103)]
    out = emb.embed_texts(texts)

    assert len(client.models.calls) == 2
    assert len(client.models.calls[0]) == 100
    assert len(client.models.calls[1]) == 3
    assert len(out) == 103
    for i, vec in enumerate(out):
        assert vec == pytest.approx([float(i), 0.0, 0.0])
    # 청크 순서가 뒤집히면(예: 3건 청크가 먼저) 위 위치별 값 검증이 실패한다 — 공허한 길이 검사 아님.
    assert client.models.configs[0].output_dimensionality == 3
    assert client.models.configs[1].output_dimensionality == 3


def test_embed_texts_passes_task_type_on_every_chunk(monkeypatch):
    # 청크화가 "첫 청크에만 task_type 을 싣고 이후 청크는 빠뜨리는" 형태로 회귀해도
    # 단일 호출(1건) 테스트는 이 갭을 못 덮는다 — 100건 초과 입력으로 모든 청크를 검사한다.
    settings = Settings(
        _env_file=None, google_api_key="test-key", embedding_dim=3, embedding_normalized=False
    )
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    client = _ChunkCapturingClient()
    monkeypatch.setattr(emb, "_client", lambda api_key: client)

    texts = [f"t{i}" for i in range(103)]
    emb.embed_texts(texts, task_type="RETRIEVAL_QUERY")

    assert len(client.models.calls) == 2
    assert [c.task_type for c in client.models.configs] == ["RETRIEVAL_QUERY", "RETRIEVAL_QUERY"]


def test_embed_texts_exactly_100_is_one_call(monkeypatch):
    settings = Settings(
        _env_file=None, google_api_key="test-key", embedding_dim=3, embedding_normalized=False
    )
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    client = _ChunkCapturingClient()
    monkeypatch.setattr(emb, "_client", lambda api_key: client)

    texts = [f"t{i}" for i in range(100)]
    out = emb.embed_texts(texts)

    assert len(client.models.calls) == 1
    assert len(client.models.calls[0]) == 100
    assert len(out) == 100


def test_embed_texts_empty_input_makes_no_calls(monkeypatch):
    settings = Settings(_env_file=None, google_api_key="test-key", embedding_dim=3)
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    client = _ChunkCapturingClient()
    monkeypatch.setattr(emb, "_client", lambda api_key: client)

    out = emb.embed_texts([])

    assert out == []
    assert client.models.calls == []


def test_client_sets_http_timeout_from_config(monkeypatch):
    """[#101 PR#166 리뷰] genai.Client 에 config embedding_timeout_s(ms) 를 http_options 로 건다.

    이 PR 로 방식2(임베딩 재정렬)가 hot path 기본이 돼 매 추천 턴마다 Google 임베딩 API 를 탄다.
    상한이 없으면 그 API 가 느려질 때 SSE 스트림이 first-token 도 못 내고 무기한 대기한다 —
    CLAUDE.md 'AI→외부 3s' 규약대로 클라이언트에 요청 타임아웃을 건다(초과 시 상위에서 degrade).
    """
    import google.genai as genai_mod

    captured = {}

    class _FakeClient:
        def __init__(self, *, api_key, http_options=None):
            captured["api_key"] = api_key
            captured["http_options"] = http_options

    monkeypatch.setattr(genai_mod, "Client", _FakeClient)
    settings = Settings(_env_file=None, google_api_key="k", embedding_timeout_s=3.0)
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    emb._CLIENT_CACHE.clear()

    emb._client("k")

    assert captured["http_options"] is not None
    assert captured["http_options"].timeout == 3000  # 3.0s → 3000ms(HttpOptions.timeout 은 ms)
