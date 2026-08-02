"""chat_request 파서 재사용과 조인 unknown 처리를 검증한다."""

from evals.benchmark.server_join import join_records


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
