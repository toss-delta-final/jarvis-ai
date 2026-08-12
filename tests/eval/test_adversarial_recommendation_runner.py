"""Adversarial recommendation dataset runtime runner regression tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import httpx
import pytest

from evals.adversarial_recommendation import runner as runner_module
from evals.adversarial_recommendation.__main__ import main
from evals.adversarial_recommendation.generator import load_cases
from evals.adversarial_recommendation.runner import (
    AdversarialBuyerRunner,
    CaseTransport,
    ScriptedCaseLLM,
)
from evals.adversarial_recommendation.scoring import score_results

pytestmark = pytest.mark.eval


def _case(case_id: str):  # noqa: ANN001
    return next(case for case in load_cases() if case.case_id == case_id)


def test_case_transport_excludes_unknown_price_when_price_filter_is_requested() -> None:
    case = _case("adv-missing_data-01-missing")
    transport = CaseTransport(case, internal_token="test-token")
    request = httpx.Request(
        "GET",
        "http://spring/internal/products/search?maxPrice=50000",
        headers={"X-Internal-Token": "test-token"},
    )

    response = transport.handler(request)

    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.asyncio
async def test_scripted_runner_uses_real_buyer_path_and_captures_push() -> None:
    case = _case("adv-boundary-02-equal")

    execution = await AdversarialBuyerRunner(mode="scripted").run(case)

    assert execution["hardFailure"] is False
    assert execution["rankedProductIds"] == [2201, 2202]
    assert execution["reasons"] == {
        "2201": "후보 데이터 기반 추천",
        "2202": "후보 데이터 기반 추천",
    }
    assert any(request["path"] == "/internal/products/search" for request in execution["requests"])
    assert any(request["path"] == "/internal/recommendations" for request in execution["requests"])
    assert any(
        event["type"] == "products.ready" and event["data"]["listIds"]
        for event in execution["sseFrames"]
    )


@pytest.mark.asyncio
async def test_unfiltered_scripted_case_still_uses_search_catalog_and_i1() -> None:
    case = _case("adv-no_evidence-01-seed")

    execution = await AdversarialBuyerRunner(mode="scripted").run(case)

    assert execution["hardFailure"] is False
    assert any(request["path"] == "/internal/products/search" for request in execution["requests"])


@pytest.mark.asyncio
async def test_live_runner_uses_injected_llm_and_records_provider_calls() -> None:
    case = _case("adv-boundary-02-equal")
    llm = ScriptedCaseLLM(case)

    execution = await AdversarialBuyerRunner(
        mode="live",
        llm=llm,
        model_config={"provider": "test-live"},
    ).run(case)

    assert execution["hardFailure"] is False
    assert [call["tier"] for call in execution["providerCalls"]] == ["fast", "smart"]
    assert execution["modelConfig"] == {"provider": "test-live"}


@pytest.mark.asyncio
async def test_runner_injects_prompt_only_arm_and_captures_grounding_decisions() -> None:
    case = _case("adv-boundary-02-equal")

    execution = await AdversarialBuyerRunner(
        mode="scripted",
        grounding_arm="prompt_only",
    ).run(case)

    assert execution["hardFailure"] is False
    assert execution["groundingArm"] == "prompt_only"
    assert len(execution["groundingDecisions"]) == len(execution["rankedProductIds"])
    assert {decision["requestedReasonCode"] for decision in execution["groundingDecisions"]} == {
        "NO_VERIFIABLE_EVIDENCE"
    }


def test_validated_execution_reuses_prompt_only_ranking_and_templates_only_known_reasons() -> None:
    prompt_only = {
        "caseId": "case-1",
        "groundingArm": "prompt_only",
        "rankedProductIds": [101, 102],
        "reasons": {"101": "검증되지 않은 모델 근거", "102": "검색순서 보충 근거"},
        "groundingDecisions": [
            {
                "productId": 101,
                "requestedReasonCode": "RATING_HIGH",
                "evidenceFields": ["ratingLevel"],
                "modelRationale": "검증되지 않은 모델 근거",
                "renderedRationale": "평점 평가가 높은 상품이에요",
                "supported": True,
                "downgraded": False,
                "failureReason": None,
            }
        ],
        "pushBody": {
            "lists": [
                {
                    "productIds": [101, 102],
                    "reasons": [
                        {"productId": 101, "reason": "검증되지 않은 모델 근거"},
                        {"productId": 102, "reason": "검색순서 보충 근거"},
                    ],
                }
            ]
        },
        "requests": [
            {
                "path": "/internal/recommendations",
                "body": {
                    "lists": [
                        {
                            "productIds": [101, 102],
                            "reasons": [
                                {"productId": 101, "reason": "검증되지 않은 모델 근거"},
                                {"productId": 102, "reason": "검색순서 보충 근거"},
                            ],
                        }
                    ]
                },
            }
        ],
        "providerCalls": [{"tier": "fast"}, {"tier": "smart"}],
    }

    validated = runner_module.derive_validated_execution(prompt_only)

    assert validated["groundingArm"] == "validated"
    assert validated["derivedFromArm"] == "prompt_only"
    assert validated["rankedProductIds"] == prompt_only["rankedProductIds"]
    assert validated["reasons"] == {
        "101": "평점 평가가 높은 상품이에요",
        "102": "검색순서 보충 근거",
    }
    assert validated["pushBody"]["lists"][0]["reasons"][0]["reason"] == (
        "평점 평가가 높은 상품이에요"
    )
    assert validated["requests"][0]["body"]["lists"][0]["reasons"][0]["reason"] == (
        "평점 평가가 높은 상품이에요"
    )
    assert validated["providerCalls"] == []
    assert validated["latencyMs"] is None
    assert prompt_only["reasons"]["101"] == "검증되지 않은 모델 근거"


@pytest.mark.asyncio
async def test_later_arm_reuses_first_arm_decompose_decision() -> None:
    case = _case("adv-boundary-02-equal")
    current_runner = AdversarialBuyerRunner(mode="scripted", grounding_arm="current")
    current = await current_runner.run(case)
    prompt_runner = AdversarialBuyerRunner(mode="scripted", grounding_arm="prompt_only")

    prompt = await prompt_runner.run(
        case,
        decision_override=current_runner.decompose_decisions[case.case_id],
        decompose_source_arm="current",
    )

    assert prompt["hardFailure"] is False
    assert prompt["decomposeSourceArm"] == "current"
    assert [call["tier"] for call in current["providerCalls"]] == ["fast", "smart"]
    assert [call["tier"] for call in prompt["providerCalls"]] == ["smart"]
    assert prompt["extractedFilters"] == current["extractedFilters"]


def test_scorer_fails_an_ineligible_recommendation() -> None:
    case = _case("adv-boundary-02-other")
    execution = {
        "caseId": case.case_id,
        "hardFailure": False,
        "rankedProductIds": [2201],
        "reasons": {"2201": "후보 데이터 기반 추천"},
    }

    result = score_results([case], [execution], mode="live")[0]

    assert result["automaticVerdict"] == "fail"
    assert result["verdict"] == "fail"
    assert any(
        check["name"] == "no_ineligible_recommendation" and check["status"] == "fail"
        for check in result["checks"]
    )


def test_scorer_marks_semantic_gold_for_review() -> None:
    case = _case("adv-no_evidence-01-seed")
    execution = {
        "caseId": case.case_id,
        "hardFailure": False,
        "rankedProductIds": [7101, 7102],
        "reasons": {"7101": "후보 데이터 기반 추천", "7102": "후보 데이터 기반 추천"},
    }

    result = score_results([case], [execution], mode="live")[0]

    assert result["automaticVerdict"] == "pass"
    assert result["behavioralVerdict"] == "review"
    assert result["verdict"] == "review"


def test_scorer_marks_digits_in_missing_numeric_target_reason_for_review() -> None:
    case = _case("adv-numeric_hallucination-01-missing")
    execution = {
        "caseId": case.case_id,
        "hardFailure": False,
        "rankedProductIds": [4101, 4102],
        "reasons": {"4101": "가격은 약 49000원입니다", "4102": "비교 후보"},
    }

    result = score_results([case], [execution], mode="live")[0]

    assert result["automaticVerdict"] == "pass"
    assert result["verdict"] == "review"
    assert any(
        check["name"] == "missing_numeric_claim_signal" and check["status"] == "review"
        for check in result["checks"]
    )


def test_scorer_does_not_claim_word_form_numeric_text_is_safe() -> None:
    case = _case("adv-numeric_hallucination-01-missing")
    execution = {
        "caseId": case.case_id,
        "hardFailure": False,
        "rankedProductIds": [4101],
        "reasons": {"4101": "가격은 오만원입니다"},
    }

    result = score_results([case], [execution], mode="live")[0]

    signal = next(
        check for check in result["checks"] if check["name"] == "missing_numeric_claim_signal"
    )
    assert signal["status"] == "review"
    assert result["verdict"] == "review"


def test_prompt_injection_family_fails_when_mutation_improves_target_rank() -> None:
    seed = _case("adv-prompt_injection-01-seed")
    mutation = _case("adv-prompt_injection-01-injected")
    executions = [
        {
            "caseId": seed.case_id,
            "hardFailure": False,
            "rankedProductIds": [5102, 5101],
            "reasons": {},
        },
        {
            "caseId": mutation.case_id,
            "hardFailure": False,
            "rankedProductIds": [5101, 5102],
            "reasons": {},
        },
    ]

    results = score_results([seed, mutation], executions, mode="live")
    mutated = next(result for result in results if result["caseId"] == mutation.case_id)

    assert mutated["verdict"] == "fail"
    assert any(
        check["name"] == "injection_rank_invariance" and check["status"] == "fail"
        for check in mutated["checks"]
    )


def test_prompt_injection_absence_in_shorter_list_is_not_rank_improvement() -> None:
    seed = _case("adv-prompt_injection-04-seed")
    mutation = _case("adv-prompt_injection-04-injected")
    executions = [
        {
            "caseId": seed.case_id,
            "hardFailure": False,
            "rankedProductIds": [5402],
            "reasons": {},
        },
        {
            "caseId": mutation.case_id,
            "hardFailure": False,
            "rankedProductIds": [],
            "reasons": {},
        },
    ]

    results = score_results([seed, mutation], executions, mode="live")
    mutated = next(result for result in results if result["caseId"] == mutation.case_id)
    rank_check = next(
        check for check in mutated["checks"] if check["name"] == "injection_rank_invariance"
    )

    assert rank_check["status"] == "pass"


def test_scripted_cli_writes_attributable_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "run"

    exit_code = main(
        [
            "--mode",
            "scripted",
            "--case-ids",
            "adv-boundary-02-equal",
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    assert {path.name for path in out.iterdir()} == {
        "report.md",
        "results.jsonl",
        "run_manifest.json",
        "summary.json",
    }
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "scripted"
    assert manifest["caseIds"] == ["adv-boundary-02-equal"]
    assert manifest["command"] == [
        sys.executable,
        "-m",
        "evals.adversarial_recommendation",
        "--mode",
        "scripted",
        "--case-ids",
        "adv-boundary-02-equal",
        "--out",
        str(out),
    ]
    assert manifest["git"]["commit"]
    assert isinstance(manifest["git"]["dirty"], bool)
    assert manifest["environment"]["python"]
    assert manifest["sourceHashes"]["buyerGraph"]
    assert manifest["sourceHashes"]["decomposePrompt"]
    assert manifest["sourceHashes"]["rerankPrompt"]
    assert manifest["sourceHashes"]["searchService"]
    assert manifest["sourceHashes"]["springClient"]
    assert manifest["sourceHashes"]["springSchemas"]
    assert manifest["sourceHashes"]["chatSchemas"]
    assert manifest["sourceHashes"]["recordingLlm"]
    assert manifest["uvLockSha256"]
    assert manifest["effectiveSettings"]["search_backend"] == "spring"
    assert manifest["effectiveSettingsSha256"]
    assert manifest["git"]["worktreeDiffSha256"]
    result = json.loads((out / "results.jsonl").read_text(encoding="utf-8"))
    assert result["behavioralVerdict"] == "not_evaluated"


def test_scripted_cli_runs_all_grounding_arms_and_derives_validated_from_prompt_only(
    tmp_path: Path,
) -> None:
    out = tmp_path / "arms"

    exit_code = main(
        [
            "--mode",
            "scripted",
            "--arms",
            "all",
            "--case-ids",
            "adv-boundary-02-equal",
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    results = [
        json.loads(line)
        for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [result["groundingArm"] for result in results] == [
        "current",
        "prompt_only",
        "validated",
    ]
    by_arm = {result["groundingArm"]: result for result in results}
    assert by_arm["prompt_only"]["execution"]["decomposeSourceArm"] == "current"
    assert [call["tier"] for call in by_arm["prompt_only"]["execution"]["providerCalls"]] == [
        "smart"
    ]
    assert by_arm["validated"]["execution"]["derivedFromArm"] == "prompt_only"
    assert by_arm["validated"]["execution"]["providerCalls"] == []
    assert (
        by_arm["validated"]["execution"]["rankedProductIds"]
        == (by_arm["prompt_only"]["execution"]["rankedProductIds"])
    )
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["arms"] == ["current", "prompt_only", "validated"]
    assert summary["uniqueCaseCount"] == 1
    assert summary["caseCount"] == 3
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["arms"] == ["current", "prompt_only", "validated"]
    assert manifest["sourceHashes"]["rerankStructuredPrompt"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## Grounding arms" in report
    assert "`validated`" in report


def test_cli_refuses_to_overwrite_existing_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()

    exit_code = main(["--mode", "scripted", "--case-limit", "1", "--out", str(out)])

    assert exit_code == 2


def test_artifact_writer_does_not_publish_partial_output_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals.adversarial_recommendation import report

    out = tmp_path / "run"
    case = _case("adv-boundary-02-equal")
    original = Path.write_text

    def fail_on_summary(self: Path, data: str, *args, **kwargs):  # noqa: ANN001
        if self.name == "summary.json":
            raise OSError("disk full")
        return original(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_on_summary)

    with pytest.raises(OSError, match="disk full"):
        report.write_run_artifacts(
            out,
            cases=[case],
            results=[],
            mode="scripted",
            model_config={"provider": "scripted"},
            command=["python", "-m", "evals.adversarial_recommendation"],
            effective_settings={"apiKey": "secret", "searchBackend": "spring"},
        )

    assert not out.exists()


def test_atomic_publish_never_replaces_existing_empty_directory(tmp_path: Path) -> None:
    from evals.adversarial_recommendation.report import _rename_no_replace

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    destination_inode = destination.stat().st_ino

    with pytest.raises(FileExistsError):
        _rename_no_replace(source, destination)

    assert source.is_dir()
    assert destination.stat().st_ino == destination_inode


def test_effective_settings_redaction_hides_db_credentials_but_keeps_token_budgets() -> None:
    from evals.adversarial_recommendation.report import _redact_settings

    redacted = _redact_settings(
        {
            "catalog_db_url": "postgresql://user:password@db/catalog",
            "profileDbUrl": "postgresql://user:password@db/profile",
            "internal_api_token": "secret-token",
            "rerank_max_tokens_base": 960,
        }
    )

    assert redacted == {
        "catalog_db_url": "<redacted>",
        "profileDbUrl": "<redacted>",
        "internal_api_token": "<redacted>",
        "rerank_max_tokens_base": 960,
    }
