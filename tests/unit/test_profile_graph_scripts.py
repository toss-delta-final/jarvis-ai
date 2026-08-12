"""#356 시드·프로브 스크립트 배선 검증.

스크립트는 CI 에서 실행하지 않는다(실 DB·실 LLM·비용). 여기서 재는 것은 **배선**이다 —
import 가 되는지, 운영 코드를 부르는지, 표본을 실제로 만들 수 있는지. 스크립트가 조용히 깨진 채
발표 당일에 발견되는 것을 막는 최소한이다.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed_script():
    return _load("seed_profile_graph_356")


@pytest.fixture(scope="module")
def probe_script():
    return _load("probe_delta_prompt_356")


def test_seed_scenario_covers_every_resolvable_kind(seed_script) -> None:
    """시드가 한 축만 채우면 "그래프가 찬 계정"이 아니다 — 시연에서 빈 축이 드러난다."""
    kinds = {row[1] for row in seed_script._SCENARIO if row[1]}

    assert {"brand", "priceBand", "ratingBand", "attribute", "category"} <= kinds


def test_seed_scenario_includes_an_unprojectable_fact(seed_script) -> None:
    """트리플이 안 붙는 fact 가 하나는 있어야 unprojected_count 가 0이 아닌 상태를 볼 수 있다."""
    assert any(not row[1] for row in seed_script._SCENARIO)


def test_seed_scenario_includes_a_negative_polarity(seed_script) -> None:
    """avoids edge 가 없으면 상충·supersede 를 시연할 수 없다."""
    assert any(row[4] == "negative" for row in seed_script._SCENARIO)


def test_seed_band_labels_pass_the_strict_parser(seed_script) -> None:
    """시드 라벨이 엄격 파서를 통과하지 못하면 시드가 조용히 밴드 없는 그래프를 만든다.

    **정규식이 아니라 파서를 부른다.** `BAND_RE` 는 `"-"` 도 매치하고(그 모듈 docstring 이
    직접 그렇게 적었다) 크기·스케일·도메인 경계 접기를 전혀 모른다 — 정규식만 보면
    `"50000-30000"`·`ratingBand "4-9"`·`"0-5"` 가 전부 통과한 뒤 `_resolve_band` 에서
    드롭되어, 이 테스트가 막으라고 있는 바로 그 "조용히 밴드 없는 그래프"가 만들어진다.
    """
    from app.agents.profile.resolver import _resolve_band

    for fact, kind, label, *_ in seed_script._SCENARIO:
        if kind in ("priceBand", "ratingBand"):
            node = _resolve_band(kind, label, anchor_phrase="", now="2026-08-11T00:00:00+00:00")
            assert node is not None, f"{fact}: {label!r} 이 파서에서 드롭된다"


def test_seed_calls_production_code_not_a_copy(seed_script) -> None:
    """흉내 낸 시드는 운영과 다른 문서를 만든다 — 그러면 시드로 확인한 동작이 근거가 못 된다."""
    from app.agents.profile import graph_merge, resolver

    assert seed_script.resolve_triple is resolver.resolve_triple
    assert seed_script.build_graph_document is graph_merge.build_graph_document


def test_probe_loads_sessions_from_the_goldenset(probe_script) -> None:
    """표본 0 은 근거가 아니라 질문이다 — 골든셋 경로가 살아 있는지 먼저 고정한다."""
    sessions = probe_script.load_sessions(3, 3)

    assert len(sessions) == 3
    assert all(len(s) == 3 for s in sessions)
    assert all(isinstance(utterance, str) and utterance for s in sessions for utterance in s)


def test_probe_session_sampling_is_deterministic(probe_script) -> None:
    """표본에 임의성이 들어가면 구·신 비교가 프롬프트 차이가 아니라 표본 차이를 잰다."""
    assert probe_script.load_sessions(4, 3) == probe_script.load_sessions(4, 3)


def test_probe_compares_both_prompts(probe_script) -> None:
    from app.agents.profile import builder

    assert probe_script._DELTA_SYSTEM is builder._DELTA_SYSTEM
    assert probe_script._DELTA_SYSTEM_LEGACY is builder._DELTA_SYSTEM_LEGACY


def test_probe_does_not_hardcode_a_provider_key(probe_script) -> None:
    """미구성 안내가 provider 를 단정하면 키를 엉뚱한 곳에 채우게 만든다.

    `get_llm()` 은 `settings.llm_provider` 로 갈리는데(기본 openai), 안내만 ANTHROPIC 을 가리키면
    그대로 따른 사람은 Anthropic 키를 채우고도 여전히 안 도는 상태에서 다른 곳을 뒤진다.
    낡은 안내는 stale 이 아니라 **틀린 곳을 고치게 하는** 안내다(docs/lessons.md #383 항목과 같은 부류).
    """
    source = (REPO_ROOT / "scripts" / "probe_delta_prompt_356.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # docstring 은 두 키를 모두 설명하므로 본문만 본다

    assert "settings.llm_provider" in body or "llm_provider" in body
    assert "OPENAI_API_KEY" in body and "ANTHROPIC_API_KEY" in body  # 양쪽을 갈라 안내한다


def test_probe_reports_provider_and_model(probe_script) -> None:
    """분포 수치는 **어느 모델에서 쟀는지**와 함께여야 근거가 된다 — 모델이 바뀌면 분포도 바뀐다."""
    from app.core.llm import resolve_model_id

    assert probe_script.resolve_model_id is resolve_model_id
