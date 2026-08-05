from __future__ import annotations

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
