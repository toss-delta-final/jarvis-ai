"""chat_request 파서 재사용과 조인·가격 근거 unknown 처리를 검증한다."""

from evals.benchmark.server_join import apply_price_evidence, join_records
from evals.benchmark.stats import summarize_group


def test_server_join_matches_request_id_and_preserves_unknowns() -> None:
    clients = [
        {"request_id": "yes", "phase": "measured"},
        {"request_id": "no", "phase": "measured"},
    ]
    servers = {
        "yes": {
            "requestId": "yes",
            "model": ["provider-model"],
            "latencyFirstToken": 123,
            "promptTokens": 4,
            "completionTokens": 5,
            "costUsd": 0.1,
            "lane": "recommend",
            "degraded": False,
        }
    }
    joined, stats = join_records(clients, servers)
    assert stats == {"joined": 1, "measured": 2, "rate": 0.5}
    assert joined[0]["server_join"] == "joined"
    assert joined[0]["server_first_text_token_ms"] == 123
    assert joined[0]["provider_ttft_ms"] is None
    assert joined[0]["provider_ttft_unknown_reason"] == "not_in_chat_request_log"
    assert joined[0]["reasoning_tokens"] == {
        "value": None,
        "unknown_reason": "not_emitted_by_server",
    }
    assert joined[1]["server_join"] == "unavailable"
    assert joined[1]["cost_usd"] is None
    assert joined[1]["server_join_reason"] == "request_id_not_found"


def test_server_log_omission_is_explicit() -> None:
    joined, stats = join_records([{"request_id": "x", "phase": "measured"}], None)
    assert stats["joined"] == 0
    assert joined[0]["server_join_reason"] == "server_log_not_provided"


def test_empty_price_table_turns_server_zero_cost_into_unknown() -> None:
    record = {
        "phase": "measured",
        "server_join": "joined",
        "model_ids": ["gpt-5-nano"],
        "cost_usd": 0.0,
    }
    # #437 — Settings() 기본 단가표는 더 이상 빈 dict 가 아니다(gpt-5-nano/gpt-5.6-luna 내장
    # 기본값). 이 테스트가 실제로 지키려는 불변식은 "빈 단가표면 서버의 costUsd 0 을 unknown
    # 으로 강등한다"이므로, 그 조건을 명시적 빈 dict 로 재현해 불변식을 그대로 유지한다.
    empty_prices: dict[str, float] = {}
    apply_price_evidence(
        [record],
        input_prices=empty_prices,
        output_prices=empty_prices,
    )
    assert record["cost_usd"] is None
    assert "price_missing" in record["cost_unknown_reason"]
    summary = summarize_group(
        [record], elapsed_s=1, p99_min_samples=100, resamples=2, confidence=0.95, seed=1
    )
    assert summary["server_metrics"]["cost_sample_count"] == 0
    assert summary["server_metrics"]["cost_unknown_count"] == 1
    assert summary["server_metrics"]["cost_price_missing_count"] == 1
    assert 0.0 not in [value for value in (record["cost_usd"],) if isinstance(value, int | float)]


def test_partial_price_table_is_not_sufficient_cost_evidence() -> None:
    record = {
        "phase": "measured",
        "server_join": "joined",
        "model_ids": ["model-a", "model-b"],
        "cost_usd": 0.25,
    }
    apply_price_evidence(
        [record],
        input_prices={"model-a": 0.1, "model-b": 0.2},
        output_prices={"model-a": 0.3},
    )
    assert record["cost_usd"] is None
    assert record["cost_unknown_reason"] == "price_missing(model=model-b)"


def test_complete_price_table_preserves_measured_server_cost() -> None:
    record = {
        "phase": "measured",
        "server_join": "joined",
        "model_ids": ["model-a", "model-b"],
        "cost_usd": 0.25,
    }
    apply_price_evidence(
        [record],
        input_prices={"model-a": 0.1, "model-b": 0.2},
        output_prices={"model-a": 0.3, "model-b": 0.4},
    )
    assert record["cost_usd"] == 0.25
    assert record["cost_unknown_reason"] is None
    summary = summarize_group(
        [record], elapsed_s=1, p99_min_samples=100, resamples=2, confidence=0.95, seed=1
    )
    assert summary["server_metrics"]["cost_sample_count"] == 1
    assert summary["server_metrics"]["cost_unknown_count"] == 0
