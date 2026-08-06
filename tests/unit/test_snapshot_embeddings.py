from __future__ import annotations

from app.core.config import Settings
from app.pipelines import embedding as emb
from evals.scoring import snapshot_embeddings


def test_embed_texts_chunked_splits_over_100_and_preserves_order(monkeypatch) -> None:
    """101건 입력은 100+1 두 번의 embed_texts 호출로 나뉘고, 결과는 입력 순서를 보존한다."""
    calls: list[list[str]] = []
    task_types: list[str | None] = []

    def fake_embed_texts(texts: list[str], *, task_type: str | None = None) -> list[list[float]]:
        calls.append(list(texts))
        task_types.append(task_type)
        return [[float(int(text))] for text in texts]

    monkeypatch.setattr(snapshot_embeddings, "embed_texts", fake_embed_texts)

    texts = [str(i) for i in range(101)]
    result = snapshot_embeddings._embed_texts_chunked(texts, task_type="RETRIEVAL_QUERY")

    assert len(calls) == 2
    assert len(calls[0]) == 100
    assert len(calls[1]) == 1
    assert task_types == ["RETRIEVAL_QUERY", "RETRIEVAL_QUERY"]
    assert result == [[float(i)] for i in range(101)]


def test_embed_texts_chunked_single_call_under_limit(monkeypatch) -> None:
    """100건 이하 입력은 한 번의 embed_texts 호출로 처리된다."""
    calls: list[list[str]] = []

    def fake_embed_texts(texts: list[str], *, task_type: str | None = None) -> list[list[float]]:
        calls.append(list(texts))
        return [[float(int(text))] for text in texts]

    monkeypatch.setattr(snapshot_embeddings, "embed_texts", fake_embed_texts)

    texts = [str(i) for i in range(100)]
    result = snapshot_embeddings._embed_texts_chunked(texts, task_type=None)

    assert len(calls) == 1
    assert len(calls[0]) == 100
    assert result == [[float(i)] for i in range(100)]


class _FakeEmbedding:
    def __init__(self, values: list[float]) -> None:
        self.values = values


class _FakeModels:
    def embed_content(self, *, model, contents, config):
        class _Response:
            embeddings = [_FakeEmbedding([1.0]) for _ in contents]

        return _Response()


class _FakeClient:
    def __init__(self) -> None:
        self.models = _FakeModels()


def test_embed_texts_budget_is_per_call_not_process_global(monkeypatch) -> None:
    """[#391] 총 예산은 embed_texts 호출 1회 단위로만 걸리고 프로세스 전역으로 누적되지
    않는다. 각 호출은 101건(2청크)이라 예산 검사가 실제로 걸리는데, 두 번째 호출의 시작
    시각이 첫 번째 호출로부터 예산을 한참 넘겨(1000s) 흐른 뒤여도 두 번째 호출 자신의
    경과(0.1s)만 보므로 성공한다 — 만약 구현이 프로세스 전역 기준 시각을 썼다면 두 번째
    호출의 두 번째 청크가 거부됐을 것이다.
    """
    settings = Settings(
        _env_file=None,
        google_api_key="test-key",
        embedding_dim=1,
        embedding_normalized=False,
        embedding_timeout_s=1.0,
        embedding_total_timeout_s=3.0,
    )
    monkeypatch.setattr(emb, "get_settings", lambda: settings)
    monkeypatch.setattr(emb, "_client", lambda api_key: _FakeClient())
    clock_values = iter([0.0, 0.1, 1000.0, 1000.1])
    monkeypatch.setattr(emb, "_monotonic", lambda: next(clock_values))

    first = emb.embed_texts([str(i) for i in range(101)])
    second = emb.embed_texts([str(i) for i in range(101)])

    assert len(first) == 101
    assert len(second) == 101
