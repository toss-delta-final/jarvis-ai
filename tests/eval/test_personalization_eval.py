from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.agents.buyer.recommendation import graph as recommendation_graph
from evals.metrics.settings import EvaluationSettings
from evals.personalization import cli as personalization_cli
from evals.personalization.cli import main, normalize_paired_artifacts

# 대부분의 테스트가 main() 전체 CLI 파이프라인(arm 생성 × 케이스 스코어링 × nDCG)을
# 독립적으로 다시 돈다 — 기본 PR pytest에서 제외하고 별도 워크플로우에서만 실행한다.
pytestmark = pytest.mark.slow

BASELINE = Path("evals/personalization/baselines/dev-v2")

# Tier D 는 `provider: "scripted"` 라 LLM 을 타지 않고, 같은 플랫폼에서는 바이트 단위로 재현된다
# (`test_personalization_run_is_deterministic_across_environment_and_clock`). 그래도 정확 일치를
# 요구하지 않는 이유는 **플랫폼 간 libm 차이** 하나다 — `evals/metrics/metrics.py` 가 `math.log2`
# 결과를 케이스마다 누적 합산하므로 glibc(CI)와 MSVC(로컬 Windows)의 마지막 ulp 가 다르면 미세한
# 차가 남을 수 있다. baseline 생성 플랫폼과 CI 플랫폼이 다를 수 있는 저장소라 실제 위험이다.
# 1e-6 은 그 노이즈를 흡수하면서 #119 가 만든 −0.288 보다 5자리 이상 엄격하다.
# **`app/core/config.py` 에 두지 않는다** — 서빙 런타임 튜너블이 아니고, `run_manifest` 가 그
# 파일을 sha256 해 모든 baseline 에 박으므로 상수 하나가 커밋된 provenance 를 전부 무효화한다.
_NDCG_REL_TOLERANCE = 1e-6


@pytest.mark.eval
def test_personalization_run_is_deterministic_across_environment_and_clock(
    tmp_path, monkeypatch
) -> None:
    """이 테스트가 고정하는 축: 환경변수(`EXPOSE_MAX`)·시계가 달라도 산출물이 재현된다.

    정규화(`normalize_paired_artifacts`)가 걷어내는 축: 실행 인스턴스(`run`)와 워킹트리 상태
    (`commitSha`·`dirty`) — 이 실행 도중 리포를 편집하거나 커밋이 끼어도 이 테스트는 깨지지
    않는다(#413).

    남겨 둔 계약: `hashes`(uv.lock·goldenset manifest/fixtures·decompose.py·rerank.py·
    config.py 의 sha256, `evals.metrics.run_manifest.build_run_manifest` 참조)는 여전히 비교
    대상이다 — 이 실행 도중 그 파일들을 편집하면 실패하는 것이 의도다.
    """
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("EXPOSE_MAX", "1")
    assert main(["--out", str(first)]) == 0
    monkeypatch.setenv("EXPOSE_MAX", "99")
    monkeypatch.setattr(recommendation_graph, "_now", lambda: datetime(2030, 1, 1))
    assert main(["--out", str(second)]) == 0
    assert normalize_paired_artifacts(first) == normalize_paired_artifacts(second)


@pytest.mark.eval
def test_all_arms_and_weights_preserve_hard_filters(tmp_path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    comparison = json.loads((out / "comparison.json").read_text())
    assert comparison["hardFilter"]["violationCount"] == 0
    assert comparison["hardFilter"]["verdict"] == "pass"
    assert len(comparison["hardFilter"]["cells"]) == 25


@pytest.mark.eval
def test_case_derived_noisy_and_repeated_arms_are_discriminative(tmp_path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    comparison = json.loads((out / "comparison.json").read_text())
    pairs = comparison["pairedComparisons"]
    assert any(
        delta != 0 for delta in pairs["noisy_vs_clean"]["overall"]["ndcgAtK"]["10"]["deltas"]
    )
    assert any(
        delta != 0 for delta in pairs["repeated_vs_clean"]["overall"]["ndcgAtK"]["10"]["deltas"]
    )
    curves = comparison["weightAblation"]
    assert any(
        cell["noisy"]["ndcgAtK"]["10"] != cell["clean"]["ndcgAtK"]["10"] for cell in curves.values()
    )


@pytest.mark.eval
def test_comparison_separates_profile_identity_and_noise_counterfactuals(tmp_path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    comparison = json.loads((out / "comparison.json").read_text())
    assert comparison["primaryComparison"] == "clean_vs_member_no_profile"
    assert set(comparison["pairedComparisons"]) == {
        "clean_vs_member_no_profile",
        "noisy_vs_clean",
        "repeated_vs_clean",
        "member_no_profile_vs_guest",
    }
    configs = comparison["armModelConfig"]
    clean = {key: value for key, value in configs["clean"].items() if key != "profileArm"}
    no_profile = {
        key: value for key, value in configs["member_no_profile"].items() if key != "profileArm"
    }
    assert clean == no_profile


@pytest.mark.eval
def test_overreach_verdicts_match_committed_baseline(tmp_path) -> None:
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    fresh = json.loads((out / "overreach.json").read_text())
    committed = json.loads((BASELINE / "overreach.json").read_text())
    for name in ("intentContradiction", "forbiddenOrRecentInclusion", "cleanNoisyDrop"):
        assert fresh[name]["verdict"] == committed[name]["verdict"]


@pytest.mark.eval
def test_default_weight_ndcg_matches_committed_baseline(tmp_path) -> None:
    """[REQ-PGRAPH-114] 커밋된 baseline **수치**를 회귀시키지 않는다.

    위 `test_overreach_verdicts_match_committed_baseline` 은 verdict **문자열만** 본다. 그래서
    dev 케이스가 96 → 109 로 늘고 arm 별 nDCG 가 전부 움직인 상태를 통과시켰다(#361 에서 실측·
    재생성). 판정 라벨은 굵어서 수치 드리프트를 못 잡는다 — 여기서 값 자체를 잠근다.

    **분모를 먼저 본다.** 케이스 수가 달라진 상태의 nDCG 일치는 무회귀가 아니라 우연이고, 실제로
    그 상태가 3일 넘게 조용히 유지됐다. `caseCount` 가 다르면 그 아래 비교는 의미가 없다.

    비교 대상을 arm 별 `ndcgAtK` 전 k 와 헤드라인 meanDelta 로 한정한다 — `deltas` 배열(케이스별
    55+ 개)은 실패 메시지가 읽히지 않고, `bootstrapCi95` 는 meanDelta 가 대표하며, `weightAblation`
    25셀은 `test_all_arms_and_weights_preserve_hard_filters` 가 이미 소유한다.

    **부수 효과**: 이 게이트가 있으면 골든셋·픽스처·스코어링을 바꿔 Tier D 수치를 움직이는 PR 이
    baseline 재생성을 요구받는다. 그것이 의도다 — 재생성 비용(CLI 1회)을 그 PR 이 지는 대신,
    커밋된 수치가 무엇을 설명하는지가 항상 참이 된다.
    """
    out = tmp_path / "run"
    assert main(["--out", str(out)]) == 0
    fresh = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    committed = json.loads((BASELINE / "comparison.json").read_text(encoding="utf-8"))

    assert set(fresh["defaultWeight"]) == set(committed["defaultWeight"])
    for arm in sorted(committed["defaultWeight"]):
        actual, expected = fresh["defaultWeight"][arm], committed["defaultWeight"][arm]
        assert (actual["caseCount"], actual["ndcgCaseCount"]) == (
            expected["caseCount"],
            expected["ndcgCaseCount"],
        ), f"{arm}: 케이스 집합이 달라졌다 — baseline 재생성이 필요하다"
        for k in sorted(expected["ndcgAtK"]):
            assert actual["ndcgAtK"][k] == pytest.approx(
                expected["ndcgAtK"][k], rel=_NDCG_REL_TOLERANCE
            ), f"{arm} nDCG@{k}"

    # 헤드라인 — #119 가 파괴했던 바로 그 값(개인화가 게스트 대비 얼마나 이득인가).
    primary = committed["primaryComparison"]
    assert fresh["primaryComparison"] == primary
    assert fresh["pairedComparisons"][primary]["overall"]["ndcgAtK"]["10"][
        "meanDelta"
    ] == pytest.approx(
        committed["pairedComparisons"][primary]["overall"]["ndcgAtK"]["10"]["meanDelta"],
        rel=_NDCG_REL_TOLERANCE,
    )


@pytest.mark.eval
def test_live_preflight_rejection_does_not_execute(tmp_path, monkeypatch) -> None:
    called = False

    def forbidden_execution(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        personalization_cli,
        "get_settings",
        lambda: EvaluationSettings(model_eval_max_calls_per_run=1),
    )
    monkeypatch.setattr(personalization_cli, "run_tier_l", forbidden_execution)
    out = tmp_path / "live"
    assert main(["--live", "--case-limit", "1", "--out", str(out)]) == 2
    assert not called
    assert not out.exists()


@pytest.mark.eval
def test_live_mode_dispatches_four_arms_after_preflight(tmp_path, monkeypatch) -> None:
    """[#483] 기본 실행에 두 기준선(guest·member_no_profile)이 모두 들어간다."""
    captured = {}

    def fake_run(output_dir, **kwargs):
        captured["output"] = output_dir
        captured.update(kwargs)

    monkeypatch.setattr(personalization_cli, "run_tier_l", fake_run)
    out = tmp_path / "live"
    assert main(["--live", "--case-limit", "1", "--out", str(out)]) == 0
    assert captured["output"] == out
    assert captured["arm_names"] == [
        "guest",
        "member_no_profile",
        "clean_rerank_only",
        "clean_both",
    ]
    assert len(captured["cases"]) == 1


@pytest.mark.eval
def test_live_mode_accepts_clean_fixed_regression_arm_only_when_asked(
    tmp_path, monkeypatch
) -> None:
    """[#484] 대조군은 허용 목록에만 있고 기본 실행 arm 수(=예산)는 그대로다."""
    captured: dict[str, object] = {}

    def fake_run(output_dir, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(personalization_cli, "run_tier_l", fake_run)
    assert "clean_fixed" in personalization_cli.LIVE_ARMS
    assert "clean_fixed" not in personalization_cli.DEFAULT_LIVE_ARMS
    assert (
        main(
            [
                "--live",
                "--case-limit",
                "1",
                "--arms",
                "guest,member_no_profile,clean_rerank_only,clean_both,clean_fixed",
                "--out",
                str(tmp_path / "live"),
            ]
        )
        == 0
    )
    assert captured["arm_names"] == [
        "guest",
        "member_no_profile",
        "clean_rerank_only",
        "clean_both",
        "clean_fixed",
    ]


def test_live_arm_spec_maps_arms_to_markdown_modes() -> None:
    """[#484] arm 이름 → (마크다운 모드, identity, scope). 모드가 케이스별/고정을 가른다."""
    spec = personalization_cli._live_arm_spec
    assert spec("guest") == (None, "guest", "off")
    # [#483] guest 와 프로필 부재(None)는 같고 identity 만 member — 그 하나가 이 arm 의 존재 이유다.
    assert spec("member_no_profile") == (None, "member", "off")
    assert spec("clean_rerank_only") == ("clean", "member", "rerank_only")
    assert spec("clean_both") == ("clean", "member", "both")
    assert spec("clean_fixed") == ("fixed", "member", "rerank_only")
    with pytest.raises(ValueError):
        spec("noisy_rerank_only")


def test_case_markdown_resolver_expresses_the_cases_own_preferences() -> None:
    """[#484] 케이스마다 다른, 그 케이스 선호를 그대로 담은 마크다운이 나와야 한다."""
    from evals.goldenset.loader import load_cases
    from evals.metrics.runner import load_evaluation_fixtures
    from evals.personalization.fixtures import derive_case_preferences

    fixtures = load_evaluation_fixtures()
    cases = sorted(load_cases("dev"), key=lambda case: case.case_id)
    # 선호가 비는 케이스(dev 109건 중 35건)를 고르면 아래 루프가 0회 돌아 무조건 통과한다.
    with_signal = [
        case
        for case in cases
        if any(
            derive_case_preferences("clean", case, fixtures)[axis]
            for axis in ("brands", "categories")
        )
    ]
    assert len(with_signal) >= 2

    settings = EvaluationSettings()
    resolve = personalization_cli._case_markdown_resolver(
        "clean",
        max_chars=settings.profile_summary_max_chars,
        strength_bands=settings.personalization_eval_profile_strength_bands,
    )
    rendered = []
    for case in with_signal[:2]:
        markdown = resolve(case, fixtures)
        preferences = derive_case_preferences("clean", case, fixtures)
        for axis in ("brands", "categories"):
            for key in preferences[axis]:
                assert key in markdown, (case.case_id, key)
        rendered.append(markdown)
    assert rendered[0] != rendered[1]  # 고정 문자열로 되돌아가지 않았다
