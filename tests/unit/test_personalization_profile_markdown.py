"""구조화 선호 → §5.1 프로필 마크다운 렌더러 계약 (이슈 #484)."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from evals.personalization.profile_markdown import (
    MARKDOWN_RENDER_VERSION,
    render_profile_markdown,
)

SECTIONS = ("## 구조화 블록", "## 취향 산문", "## 최근 맥락")
BANDS = (0.7, 0.5)

# buy-budg-0004("2만원 이하 클렌징폼 추천")의 실제 파생 선호 — 강/중/약이 모두 등장한다.
CASE_0004 = {
    "brands": {
        "이니스프리": 1.0,
        "설화수": 2 / 3,
        "메디팜": 1 / 3,
        "이소켈리(ISOKELLY)": 1 / 3,
        "홀츠포맨": 1 / 3,
    },
    "categories": {
        "클렌징/필링 > 클렌징폼": 1.0,
        "클렌징/필링 > 클렌징젤": 0.4,
        "남성화장품 > 남성클렌징": 0.2,
    },
}


def _render(preferences, *, max_chars: int = 1000, bands: tuple[float, float] = BANDS) -> str:
    return render_profile_markdown(preferences, max_chars=max_chars, strength_bands=bands)


def test_render_version_is_separate_from_preference_derivation_version() -> None:
    """파생 버전(case-oracle-distractor-v1)과 다른 축이어야 Tier D 를 건드리지 않는다."""
    from evals.personalization.fixtures import DERIVATION_VERSION

    assert MARKDOWN_RENDER_VERSION == "case-markdown-render-v1"
    assert MARKDOWN_RENDER_VERSION != DERIVATION_VERSION


def test_same_preferences_render_identically_regardless_of_key_order() -> None:
    """재현성이 하네스 계약 — dict 삽입 순서가 출력에 새면 baseline 비교가 깨진다."""
    forward = {"brands": {"Sony": 1.0, "Apple": 0.5}, "categories": {"이어폰": 1.0}}
    backward = {"categories": {"이어폰": 1.0}, "brands": {"Apple": 0.5, "Sony": 1.0}}
    assert _render(forward) == _render(backward)


def test_render_keeps_three_sections_and_every_preference_key_literally() -> None:
    """Tier D 선호와 같은 신호를 표현해야 두 Tier 가 같은 프로필을 잰다(이슈 완료조건)."""
    markdown = _render(CASE_0004)
    assert all(section in markdown for section in SECTIONS)
    for axis in ("brands", "categories"):
        for key in CASE_0004[axis]:
            assert key in markdown, key


def test_strength_labels_follow_injected_bands() -> None:
    """강도는 자연어로만 노출한다(SPEC-PROFILE-001 §5.1) — 수치는 새지 않는다."""
    markdown = _render(CASE_0004)
    assert "이니스프리 (강)" in markdown
    assert "설화수 (중)" in markdown
    assert "그 외 메디팜·이소켈리(ISOKELLY)·홀츠포맨 (약)" in markdown
    assert "1.0" not in markdown and "0.666" not in markdown


def test_strength_labels_move_when_bands_change() -> None:
    """임계는 config 주입 — 값을 바꾸면 라벨이 실제로 따라 움직인다."""
    loose = _render(CASE_0004, bands=(0.5, 0.3))
    assert "설화수 (강)" in loose  # 기본 임계에서는 (중)이었다
    assert "메디팜 (중)" in loose  # 기본 임계에서는 (약) 묶음이었다


def test_entries_are_ordered_by_descending_weight() -> None:
    markdown = _render(CASE_0004)
    line = next(row for row in markdown.splitlines() if row.startswith("- 선호 브랜드:"))
    assert line.index("이니스프리") < line.index("설화수") < line.index("메디팜")


def test_empty_preferences_still_render_three_sections() -> None:
    """선호가 빈 케이스(dev 109건 중 35건)도 섹션 계약을 깨지 않는다."""
    markdown = _render({"brands": {}, "categories": {}})
    assert all(section in markdown for section in SECTIONS)
    assert "선호 신호 없음" in markdown


def test_missing_axis_key_is_treated_as_empty() -> None:
    assert _render({}) == _render({"brands": {}, "categories": {}})


def test_cap_drops_lowest_weight_entries_instead_of_slicing_text() -> None:
    """REQ-PROF-016 — 생성측 압축이지 소비측 절단이 아니다(문자열 중간을 자르지 않는다)."""
    many = {"brands": {f"브랜드{index:02d}": (100 - index) / 100 for index in range(60)}}
    markdown = _render(many, max_chars=220)
    assert len(markdown) <= 220
    assert all(section in markdown for section in SECTIONS)
    assert "브랜드00" in markdown  # 최상위 가중치는 살아남는다
    assert "브랜드59" not in markdown  # 최하위부터 통째로 빠진다
    assert not markdown.rstrip().endswith("브랜드")  # 이름이 중간에서 잘리지 않는다


def test_cap_smaller_than_the_empty_skeleton_raises() -> None:
    """조용히 깨진 프로필을 내보내느니 크게 실패한다."""
    with pytest.raises(ValueError):
        _render(CASE_0004, max_chars=10)


def test_settings_default_bands_are_injected_and_validated() -> None:
    """튜너블 하드코딩 금지 — 기본값은 config 정본에서 온다."""
    assert Settings(_env_file=None).personalization_eval_profile_strength_bands == (0.7, 0.5)
    for invalid in ((0.5, 0.7), (0.7, 0.0), (1.5, 0.5), (0.7, 0.7)):
        with pytest.raises(ValueError):
            Settings(_env_file=None, personalization_eval_profile_strength_bands=invalid)
