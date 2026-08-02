"""runner CLI의 사전 실패와 불변 출력 경계를 검증한다."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.core.config import Settings
from evals.benchmark import runner
from evals.benchmark.scenarios import evaluate_outcome, load_scenarios


def _base(tmp_path: Path) -> list[str]:
    return [
        "--base-url",
        "http://localhost:8000",
        "--target-label",
        "local",
        "--out-dir",
        str(tmp_path),
        "--dry-run",
    ]


def test_expected_outcome_match_mismatch_and_unknown() -> None:
    recommend = load_scenarios({"buyer_recommend"})[0]
    matched = evaluate_outcome(
        {
            "client_ttft_ms": 10,
            "terminal_event": "done",
            "server_join": "joined",
            "lane": "recommend",
            "degraded": False,
        },
        recommend,
    )
    assert matched["outcome_match"] is True

    dependency = load_scenarios({"buyer_dependency_degrade"})[0]
    mismatched = evaluate_outcome(
        {
            "client_ttft_ms": 10,
            "terminal_event": "done",
            "server_join": "joined",
            "lane": "recommend",
            "degraded": False,
        },
        dependency,
    )
    assert mismatched["outcome_match"] is False
    assert any("degrade" in reason for reason in mismatched["outcome_mismatch_reasons"])

    unknown = evaluate_outcome(
        {
            "client_ttft_ms": 10,
            "terminal_event": "done",
            "server_join": "unavailable",
            "lane": None,
            "degraded": None,
        },
        recommend,
    )
    assert unknown["outcome_match"] == "unknown"
    assert unknown["outcome_match"] is not False


def test_small_sample_without_rationale_exits_two(tmp_path, capsys) -> None:
    code = runner.main([*_base(tmp_path), "--measured-requests", "10"])
    assert code == 2
    assert "sample-size-rationale" in capsys.readouterr().err


def test_existing_output_directory_exits_two(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(runner, "_compact", lambda value: "fixed")
    (tmp_path / "fixed-local").mkdir()
    code = runner.main(_base(tmp_path))
    assert code == 2
    assert "already exists" in capsys.readouterr().err


def test_dry_run_validates_without_network(tmp_path, capsys) -> None:
    code = runner.main([*_base(tmp_path), "--scenarios", "buyer_recommend,buyer_fallback"])
    output = capsys.readouterr().out
    assert code == 0
    assert "network_calls=0 status=valid" in output
    assert "buyer_recommend,buyer_fallback" in output


def test_contract_sample_floor_survives_lower_setting(tmp_path, monkeypatch) -> None:
    settings = Settings(
        benchmark_min_measured_requests=10,
        benchmark_p99_min_samples=30,
    )
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    code = runner.main([*_base(tmp_path), "--measured-requests", "10"])
    assert code == 2


def test_duplicate_concurrency_is_removed_in_order(tmp_path, capsys) -> None:
    code = runner.main([*_base(tmp_path), "--concurrency", "1,1,5,1"])
    assert code == 0
    assert "concurrency=1,5 " in capsys.readouterr().out


def test_each_group_gets_warmup_and_each_scenario_gets_cold(monkeypatch) -> None:
    calls: list[tuple[str, str, int, int]] = []

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    async def fake_batch(client, scenario, *, phase, count, concurrency, headers):
        calls.append((scenario.id, phase, concurrency, count))
        return [
            {
                "scenario_id": scenario.id,
                "phase": phase,
                "concurrency": concurrency,
                "group_elapsed_s": 1.0,
                "success": True,
                "client_ttft_ms": 1,
                "error": False,
                "timed_out": False,
                "degraded": None,
            }
        ]

    monkeypatch.setattr(runner.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(runner, "_run_batch", fake_batch)
    scenarios = load_scenarios({"buyer_recommend", "seller_general"})
    args = SimpleNamespace(base_url="http://test", measured_requests=30)
    records = asyncio.run(runner.run_network(args, scenarios, [1, 5], None, Settings()))
    assert sum(phase == "cold" for _, phase, _, _ in calls) == 2
    assert sum(phase == "warmup" for _, phase, _, _ in calls) == 4
    assert {
        (scenario.id, "warmup", concurrency, max(5, concurrency))
        for scenario in scenarios
        for concurrency in (1, 5)
    } <= set(calls)
    summaries = runner._summaries(records, Settings())
    assert any(group.startswith("cold:") for group in summaries)


def test_userinfo_is_redacted_from_all_artifacts(tmp_path, monkeypatch) -> None:
    credential = "ultra-secret-credential"

    async def fake_network(args, scenarios, levels, token, settings):
        return [
            {
                "request_id": None,
                "scenario_id": scenarios[0].id,
                "role": scenarios[0].role,
                "phase": "measured",
                "concurrency": 1,
                "request_index": 0,
                "group_elapsed_s": 1.0,
                "success": True,
                "client_ttft_ms": 1.0,
                "client_first_event_ms": 1.0,
                "client_total_ms": 2.0,
                "error": False,
                "timed_out": False,
            }
        ]

    monkeypatch.setattr(runner, "run_network", fake_network)
    code = runner.main(
        [
            "--base-url",
            f"https://user:{credential}@host",
            "--target-label",
            "userinfo",
            "--scenarios",
            "buyer_recommend",
            "--concurrency",
            "1",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    artifact_dir = next(tmp_path.iterdir())
    combined = "".join(path.read_text(encoding="utf-8") for path in artifact_dir.iterdir())
    assert credential not in combined
    assert "[REDACTED]@host" in combined
