"""#443 사전 기반 category leg 주입의 경로·저하·매칭 경계 계약."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.buyer.recommendation.category_leg_injection import inject_category_leg


def test_settings_default_dictionary_path_is_absolute_and_present() -> None:
    """배선 기본값도 프로세스 CWD가 아니라 모듈 기준 사전을 가리킨다."""
    from app.core.config import Settings

    default = Settings.model_fields["category_leg_injection_path"].default
    assert Path(default).is_absolute()
    assert Path(default).is_file()


def test_default_dictionary_path_is_independent_of_process_cwd(tmp_path, monkeypatch) -> None:
    """기본 사전은 uvicorn/컨테이너의 임의 CWD에서도 모듈 기준으로 읽힌다.

    이 테스트가 잡는 변경: 기본 경로를 다시 상대 `app/data/...`로 되돌리는 경우.
    """
    monkeypatch.chdir(tmp_path)

    result = inject_category_leg(
        [], intent="recommend", utterance="과일 추천해줘", min_length=2
    )

    assert [(leg.raw_category, leg.query) for leg in result] == [(None, "과일")]


def test_missing_dictionary_degrades_without_changing_legs(tmp_path) -> None:
    """사전 사전로딩 실패는 decompose 턴을 깨지 않고 오늘의 빈-leg 동작으로 저하한다.

    이 테스트가 잡는 변경: `_names()`의 FileNotFoundError를 호출자 밖으로 내보내는 경우.
    """
    missing = tmp_path / "not-present.json"

    result = inject_category_leg(
        [],
        intent="recommend",
        utterance="과일 추천해줘",
        path=str(missing),
        min_length=2,
    )

    assert result == []


def test_adversarial_substring_inside_unrelated_word_currently_injects() -> None:
    """`과일나무`의 `과일`은 사전명 부분문자열이라 현재 규칙이 주입한다.

    최장 일치 규칙은 유지하지만, 한국어 어절 경계 판정이 없으므로 이 오탐 위험은 의도적으로
    드러낸다. 이 테스트가 실패하면 매칭 규약이 달라졌으므로 사전등록 측정을 다시 검토해야 한다.
    """
    result = inject_category_leg(
        [], intent="recommend", utterance="과일나무 묘목 추천해줘", min_length=2
    )

    assert [(leg.raw_category, leg.query) for leg in result] == [(None, "과일")]


@pytest.mark.parametrize(
    "utterance",
    [
        "5만원 이하 아무거나 추천해줘",
        "평점 좋은 걸로 보여줘",
        "인기 많은 거 추천해줘",
        "무료배송 되는 걸로 찾아줘",
        "가성비 좋은 거 추천해줘",
    ],
)
def test_preregistered_condition_only_utterances_never_inject(utterance: str) -> None:
    """#443 하드 불변식: condition_only 5발화는 사전 주입이 0건이어야 한다."""
    assert inject_category_leg([], intent="recommend", utterance=utterance, min_length=2) == []


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("과일 추천해줘", "과일"),
        ("나 아기 키우는데 과일 추천해줘", "과일"),
        ("과일 추천해줘, 나 아기 키우고 있어서", "과일"),
        ("나 아기 키우는데 유아용 물티슈 추천해줘", "물티슈"),
        ("나 캠핑 다니는데 텐트 추천해줘", "텐트"),
        ("가성비 좋은 텀블러 추천해줘", None),
    ],
)
def test_preregistered_named_category_utterances_match_only_catalog_names(
    utterance: str, expected: str | None
) -> None:
    """#443 named_category 001~005는 발동, 사전에 없는 006은 미발동한다."""
    result = inject_category_leg([], intent="recommend", utterance=utterance, min_length=2)
    assert [leg.query for leg in result] == ([] if expected is None else [expected])


def test_existing_leg_and_non_recommend_intent_do_not_inject() -> None:
    """모델 leg 보존과 recommend 전용 게이트는 사전 주입보다 우선한다."""
    from app.agents.buyer.recommendation.state import CategoryQuery

    existing = [CategoryQuery(raw_category=None, query="기존 모델 leg")]
    assert inject_category_leg(existing, intent="recommend", utterance="과일 추천", min_length=2) == existing
    assert inject_category_leg([], intent="general", utterance="과일 추천", min_length=2) == []


def test_longest_catalog_name_wins_and_min_length_mutation_changes_outcome(tmp_path) -> None:
    """최장 일치가 뒤집히거나 최소 길이 게이트가 무력화되면 이 변이 시험이 잡는다."""
    dictionary = tmp_path / "categories.json"
    dictionary.write_text(
        json.dumps(
            {"categories": [{"path": ["생활 > 물티슈", "생활 > 유아용 물티슈"]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    longest = inject_category_leg(
        [], intent="recommend", utterance="유아용 물티슈 추천", path=str(dictionary), min_length=2
    )
    blocked = inject_category_leg(
        [], intent="recommend", utterance="물티슈 추천", path=str(dictionary), min_length=4
    )

    assert [leg.query for leg in longest] == ["유아용 물티슈"]
    assert blocked == []
