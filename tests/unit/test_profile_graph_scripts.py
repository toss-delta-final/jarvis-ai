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
    """시드 라벨이 엄격 파서를 통과하지 못하면 시드가 조용히 밴드 없는 그래프를 만든다."""
    from app.agents.profile.resolver import _BAND_RE

    for fact, kind, label, *_ in seed_script._SCENARIO:
        if kind in ("priceBand", "ratingBand"):
            assert _BAND_RE.match(label), f"{fact}: {label!r} 은 밴드 형식이 아니다"


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
