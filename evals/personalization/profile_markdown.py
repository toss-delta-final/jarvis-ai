"""구조화 선호 → SPEC-PROFILE-001 §5.1 마크다운 결정론 순수 렌더러 (이슈 #484).

Tier D 는 케이스별 구조화 선호(`derive_case_preferences`)를 쓰지만 Tier L 은 서빙과 같은
**마크다운**을 소비한다. 그 사이를 잇는 변환기가 없어 Tier L 이 픽스처의 고정 문자열 한 개를
전 케이스에 먹이고 있었다 — 그래서 live-v1 의 ΔnDCG 는 "무관한 프로필의 효과"를 잰 값이다.

**evals 밖(app) 의존을 두지 않는다.** 입력은 `{axis: {key: weight}}` 뿐이고 출력은 str 이라,
후속에서 실 `GraphDocument` 파생 선호를 물릴 때 이 파일을 그대로 옮길 수 있다.
"""

from __future__ import annotations

# 선호 파생 규칙(`fixtures.DERIVATION_VERSION`)과 **다른 축**이다 — 파생은 그대로 두고 렌더링만
# 바뀌는 변경을 baseline 간 구분하기 위해 별도 버전을 둔다. 파생 버전을 올리면 Tier D 의
# eval_config 스냅샷 검증(`config.load_eval_config`)이 깨진다.
MARKDOWN_RENDER_VERSION = "case-markdown-render-v1"

_AXIS_ORDER: tuple[str, ...] = ("brands", "categories")
_AXIS_LABELS = {"brands": "선호 브랜드", "categories": "선호 카테고리"}
_EMPTY_BLOCK_LINE = "- 선호 신호 없음"
_EMPTY_PROSE = "이 케이스에서 파생된 선호 신호가 없다."
_RECENT_CONTEXT = "케이스 파생 선호에는 최근 맥락 신호가 없다."

_Entry = tuple[str, float]


def _sorted_entries(axis_values: dict[str, float]) -> list[_Entry]:
    """가중치 내림차순, 동점은 키 오름차순 — 입력 dict 순서가 출력에 새지 않게 고정한다."""
    return sorted(axis_values.items(), key=lambda item: (-item[1], item[0]))


def _axis_line(axis: str, entries: list[_Entry], bands: tuple[float, float]) -> str:
    """강·중은 개별 표기, 약은 뒤에 묶어 표기 — 1회짜리 꼬리를 최상위와 동급으로 읽지 않게."""
    high, mid = bands
    parts = [
        f"{key} (강)" if weight >= high else f"{key} (중)"
        for key, weight in entries
        if weight >= mid
    ]
    weak = [key for key, weight in entries if weight < mid]
    if weak:
        prefix = "그 외 " if parts else ""
        parts.append(f"{prefix}{'·'.join(weak)} (약)")
    return f"- {_AXIS_LABELS[axis]}: {', '.join(parts)}"


def _prose(entries_by_axis: dict[str, list[_Entry]]) -> str:
    """축별 최상위 하나씩만 산문에 올린다 — 나열은 구조화 블록의 몫이다."""
    leads = [entries_by_axis[axis][0][0] for axis in _AXIS_ORDER if entries_by_axis[axis]]
    if not leads:
        return _EMPTY_PROSE
    return f"{' · '.join(leads)} 선호를 참고하되 현재 발화의 명시 조건을 우선한다."


def _compose(entries_by_axis: dict[str, list[_Entry]], bands: tuple[float, float]) -> str:
    lines = [
        _axis_line(axis, entries_by_axis[axis], bands)
        for axis in _AXIS_ORDER
        if entries_by_axis[axis]
    ]
    return (
        "## 구조화 블록\n"
        + "\n".join(lines or [_EMPTY_BLOCK_LINE])
        + "\n## 취향 산문\n"
        + _prose(entries_by_axis)
        + "\n## 최근 맥락\n"
        + _RECENT_CONTEXT
        + "\n"
    )


def render_profile_markdown(
    preferences: dict[str, dict[str, float]],
    *,
    max_chars: int,
    strength_bands: tuple[float, float],
) -> str:
    """구조화 선호를 §5.1 3섹션 마크다운으로 렌더한다(같은 입력 → 같은 출력).

    상한 집행은 **생성측 압축**이다(REQ-PROF-016) — 문자열을 중간에서 자르지 않고 가중치가
    가장 낮은 항목부터 통째로 드롭하고 다시 만든다. 항목을 다 버려도 골격이 상한을 넘으면
    `ValueError` 를 낸다: 섹션이 깨진 프로필을 조용히 내보내면 소비 측이 그것을 정상 프로필로
    읽는다.
    """
    high, mid = strength_bands
    entries_by_axis = {axis: _sorted_entries(preferences.get(axis) or {}) for axis in _AXIS_ORDER}
    markdown = _compose(entries_by_axis, (high, mid))
    if len(markdown) <= max_chars:
        return markdown

    droppable = sorted(
        (weight, axis, key) for axis in _AXIS_ORDER for key, weight in entries_by_axis[axis]
    )
    for _, axis, key in droppable:
        entries_by_axis[axis] = [item for item in entries_by_axis[axis] if item[0] != key]
        markdown = _compose(entries_by_axis, (high, mid))
        if len(markdown) <= max_chars:
            return markdown
    raise ValueError(f"profile_summary_max_chars={max_chars}가 §5.1 3섹션 골격보다 작습니다")
