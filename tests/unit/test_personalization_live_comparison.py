"""[#483] Tier L 기준선 교체 — arm 파싱·축 유출·comparison 조립의 단위 테스트.

`run_tier_l` 을 통째로 부르지 않는다. 실 LLM 테스트는 이 리포에서 구조적으로 막혀 있고
(`tests/conftest.py` 가 smoke 가 아니면 API 키를 지운다), 실행기를 태우면 `asyncio.run` 경로가
딸려 온다(`docs/lessons.md` 2026-08-09). 대신 `run_tier_l` 에서 뽑아낸 **순수 함수**만 검증한다.
"""

from __future__ import annotations

import pytest

from evals.personalization import cli as personalization_cli


def _parse(value: str | None) -> list[str]:
    return personalization_cli._parse_arms(value)


def test_parse_arms_requires_both_baselines() -> None:
    """두 기준선이 없으면 주 비교(프로필 효과)도 보조 비교(cold-start)도 계산되지 않는다."""
    for value in (
        "clean_rerank_only,clean_both",  # 둘 다 없음
        "guest,clean_rerank_only",  # 주 기준선 없음
        "member_no_profile,clean_rerank_only",  # 보조 기준선 없음
    ):
        with pytest.raises(ValueError):
            _parse(value)


def test_parse_arms_accepts_baselines_in_any_order() -> None:
    """옛 `arms[0] == "guest"` 위치 규칙은 `results_by_arm["guest"]` 리터럴을 떠받치던 암묵적
    결합이었다. 기준선을 상수로 파라미터화한 뒤에는 순서가 아니라 **포함 여부**만 본다."""
    assert _parse("member_no_profile,guest,clean_rerank_only") == [
        "member_no_profile",
        "guest",
        "clean_rerank_only",
    ]


def test_parse_arms_rejects_duplicates_and_unknown() -> None:
    for value in (
        "guest,member_no_profile,guest",
        "guest,member_no_profile,noisy_rerank_only",
        "",
    ):
        with pytest.raises(ValueError):
            _parse(value)


def test_parse_arms_default_carries_both_baselines() -> None:
    """기본 실행만으로 주 비교가 나와야 한다 — 기준선을 `--arms` 로 매번 적게 하면 안 된다."""
    default = _parse(None)
    assert default == list(personalization_cli.DEFAULT_LIVE_ARMS)
    assert {
        personalization_cli.LIVE_BASELINE_ARM,
        personalization_cli.LIVE_SECONDARY_BASELINE_ARM,
    } <= set(default)


def _row(case_id: str, repeat: int, filters: dict[str, object]) -> dict[str, object]:
    return {"caseId": case_id, "repeat": repeat, "extractedFilters": filters}


def _results(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"caseResults": rows}


def _leaked_axes(result: dict[str, object]) -> list[list[str]]:
    return [row["filterAxisLeakage"] for row in result["caseResults"]]


def test_axis_leakage_pairs_the_same_repeat() -> None:
    """[#483] 기준선을 repeat 0 한 벌로 고정하면 LLM 샘플링 지터가 프로필 유출로 둔갑한다.

    아래 합성 데이터에서 두 arm 은 **완전히 같다** — repeat 0 에는 category 만, repeat 1 에는
    brand 가 더 붙었다(같은 프롬프트라도 반복마다 나올 수 있는 흔들림). 유출은 0 이어야 한다.
    기준선을 repeat 0 에 고정하면 arm 의 repeat 1 이 기준선 repeat 0 과 비교돼 `brand` 가
    "프로필이 새로 만든 축"으로 잡힌다. `ranking_change` 는 이미 (caseId, repeat) 로 짝짓는다.
    """
    jittered = [
        _row("c1", 0, {"category": "이어폰"}),
        _row("c1", 1, {"category": "이어폰", "brand": ["소니"]}),
    ]
    results_by_arm = {
        "member_no_profile": _results([dict(row) for row in jittered]),
        "clean_rerank_only": _results([dict(row) for row in jittered]),
    }
    personalization_cli.annotate_axis_metrics(
        results_by_arm,
        baseline_arm="member_no_profile",
        expected_filters_by_case={"c1": {}},
    )
    assert _leaked_axes(results_by_arm["clean_rerank_only"]) == [[], []]


def test_axis_leakage_baseline_arm_rows_are_self_zero_and_guest_becomes_noise_floor() -> None:
    """기준선 자기 자신은 항상 0 이고, 보조 기준선(guest)은 이제 **지터 바닥**으로 관측된다.

    기준선이 `guest` 였을 때는 `axisLeakage["guest"]` 가 자기 비교라 늘 비어 있어 아무것도
    알려주지 않았다. 기준선이 `member_no_profile` 로 옮겨가면 guest 행이 "프로필 없이도 이만큼
    흔들린다"는 바닥값이 되어, 다른 arm 의 유출이 신호인지 잡음인지 가르는 기준이 된다.
    """
    results_by_arm = {
        "member_no_profile": _results([_row("c1", 0, {"category": "이어폰"})]),
        "guest": _results([_row("c1", 0, {"category": "이어폰", "priceMax": 50000})]),
        "clean_rerank_only": _results([_row("c1", 0, {"category": "이어폰", "brand": ["소니"]})]),
    }
    personalization_cli.annotate_axis_metrics(
        results_by_arm,
        baseline_arm="member_no_profile",
        expected_filters_by_case={"c1": {}},
    )
    assert _leaked_axes(results_by_arm["member_no_profile"]) == [[]]
    assert _leaked_axes(results_by_arm["guest"]) == [["price_max"]]
    assert _leaked_axes(results_by_arm["clean_rerank_only"]) == [["brand"]]


def test_axis_leakage_marks_unpaired_rows_instead_of_reporting_zero() -> None:
    """기준선에 짝이 없는 행은 `[]`(유출 없음)이 아니라 `None`(계산 못 함)이어야 한다.

    예산 소진으로 기준선이 잘리면 짝 없는 행이 생긴다. 그때 `[]` 를 넣으면 "유출이 없었다"로
    읽혀 측정 중단이 안전 신호로 둔갑한다.
    """
    results_by_arm = {
        "member_no_profile": _results([_row("c1", 0, {"category": "이어폰"})]),
        "clean_rerank_only": _results(
            [
                _row("c1", 0, {"category": "이어폰", "brand": ["소니"]}),
                _row("c1", 1, {"category": "이어폰", "brand": ["소니"]}),
            ]
        ),
    }
    personalization_cli.annotate_axis_metrics(
        results_by_arm,
        baseline_arm="member_no_profile",
        expected_filters_by_case={"c1": {}},
    )
    assert _leaked_axes(results_by_arm["clean_rerank_only"]) == [["brand"], None]


ARMS = ["guest", "member_no_profile", "clean_rerank_only", "clean_both"]
BOOTSTRAP = {"resamples": 20, "confidence": 0.95}


def _report(ndcg_by_case: dict[str, float]) -> dict[str, object]:
    """`_live_metric_report` 산출물의 최소 형태 — paired 통계가 요구하는 축만 채운다."""
    return {
        "datasetHash": "dataset-hash-1",
        "kList": [10],
        "cases": [
            {
                "caseId": case_id,
                "slices": [],
                "rankingExcluded": False,
                "rankingExclusionReason": None,
                "metrics": {"ndcgAtK": {"10": value}},
                "diversity": 0.5,
            }
            for case_id, value in sorted(ndcg_by_case.items())
        ],
    }


def _live_result(ranked_by_case: dict[str, list[int]]) -> dict[str, object]:
    return {
        "coverage": {"missingModels": []},
        "caseResults": [
            {
                "caseId": case_id,
                "repeat": 0,
                "rankedProductIds": ranked,
                "metrics": {"ndcgAtK": {"10": 1.0}},
                "filterAxisLeakage": [],
                "intentContradictionAxes": [],
            }
            for case_id, ranked in sorted(ranked_by_case.items())
        ],
    }


def _build(
    *,
    arm_names: list[str] = ARMS,
    reports: dict[str, object] | None = None,
    results_by_arm: dict[str, object] | None = None,
) -> dict[str, object]:
    ndcg = {"guest": 0.4, "member_no_profile": 0.5, "clean_rerank_only": 0.7, "clean_both": 0.6}
    ranked = {"guest": [3, 4], "member_no_profile": [1, 2], "clean_rerank_only": [1, 2]}
    return personalization_cli.build_live_comparison(
        arm_names=arm_names,
        reports=reports or {arm: _report({"c1": ndcg.get(arm, 0.5)}) for arm in arm_names},
        results_by_arm=results_by_arm
        or {arm: _live_result({"c1": ranked.get(arm, [1, 2])}) for arm in arm_names},
        primary_metric="overall.ndcgAtK.10",
        k_list=(10,),
        bootstrap=BOOTSTRAP,
        seed=1,
        budget_snapshot={"calls": 0},
    )


def test_build_live_comparison_uses_member_no_profile_as_primary_baseline() -> None:
    """[#483] 주 비교는 프로필만 다른 쌍이고, guest 비교는 보조로 **남는다**(제거가 아니다)."""
    comparison = _build()
    assert comparison["baselineArm"] == "member_no_profile"
    assert comparison["secondaryBaselineArm"] == "guest"
    assert comparison["primaryComparison"] == "clean_rerank_only_vs_member_no_profile"
    # 주 비교: 기준선 자신만 빠진다. guest 가 여기 들어오는 건 의도다 — identity 효과 단독 수치.
    assert set(comparison["pairedVsMemberNoProfile"]) == {
        "guest",
        "clean_rerank_only",
        "clean_both",
    }
    assert set(comparison["pairedVsGuest"]) == {
        "member_no_profile",
        "clean_rerank_only",
        "clean_both",
    }
    headline = comparison["pairedVsMemberNoProfile"]["clean_rerank_only"]
    assert headline["overall"]["ndcgAtK"]["10"]["meanDelta"] == pytest.approx(0.2)


def test_primary_comparison_is_null_when_headline_arm_is_not_run() -> None:
    """헤드라인 arm 을 안 돌렸는데 주 비교 이름만 남으면 없는 수치를 가리키게 된다."""
    arms = ["guest", "member_no_profile", "clean_both"]
    assert _build(arm_names=arms)["primaryComparison"] is None


def test_ranking_change_follows_the_paired_baseline_not_guest() -> None:
    """[#483] 활성화 지표는 이득 지표와 **같은 기준선**을 봐야 한 표에서 읽을 수 있다.

    합성 데이터에서 clean_rerank_only 의 노출은 member_no_profile 과 같고 guest 와는 다르다.
    기준선이 리터럴 `"guest"` 로 되돌아가면 changeRate 가 0.0 이 아니라 1.0 이 되어 깨진다.
    """
    comparison = _build()
    assert comparison["rankingChange"]["clean_rerank_only"]["changeRate"] == 0.0
    assert comparison["rankingChange"]["guest"]["changeRate"] == 1.0
    assert set(comparison["rankingChange"]) == {"guest", "clean_rerank_only", "clean_both"}
