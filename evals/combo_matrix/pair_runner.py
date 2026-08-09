"""INV/DIR 라벨의 실검증 러너 (이슈 #371) — `expected/pair_checks.jsonl` 의 쌍(원본 ↔ 변형)을
실행해 INV(불변)·DIR(방향) 성질을 실제로 검증한다.

`cases/combo_cases.jsonl` 의 `perturbation_of` 케이스 3건 중 원본(combo-0021·0022·0023)은 모두
`observation_mode=manual`(context != none)이다 — 그러나 이 러너는 `runner.py::build_decompose_json`
으로 축 할당을 decompose(LLM) 산출 JSON 으로 직접 실현해 `ScriptedLLM` 에 고정 주입하므로, 멀티턴
컨텍스트 승계 해석(실 LLM 이 필요한 부분)과 무관하게 원본·변형 둘 다 결정론으로 실행할 수 있다
(기존 `runner.py` 의 ci/manual 분기와 같은 전제 — `observation_mode` 는 "일반 관측 러너가 그 케이스를
스킵하는가"를 뜻할 뿐, 이 쌍 러너가 실행 가능한지와는 별개다).

v1 실행 지원 범위(명시적으로 좁힌다 — 어휘를 새로 하드코딩해 대조하지 않고, 지원 밖 축값이 들어오면
`UnsupportedPairAxes` 로 실패시킨다. 조용히 잘못 실행하는 것을 막기 위함):
  surface=CHAT · intent=recommend · constraint_strength=normal ·
  degrade ∈ {none, embedding_missing(=none 과 동일 실행), rerank_failed}

산출 프로젝션 정의(README "INV/DIR 쌍 검증" §의 표 참조) — 두 실행 모두에서 계산하고, 비교는
`PairCheckSpec.invariant_fields`(INV) 또는 `metric`(DIR) 만 본다.

## 카테고리 seam (이슈 #371 R1 결정 → 이슈 #381 D5 로 `runner.py` 본체에 흡수됨)

`app/agents/buyer/graph.py:520-537` 는 `decision.category_legs` 가 비면 `decision.filters.category`
를 무조건 `None` 으로 지운다(canonical-or-null degrade — "미검증 원문이 Spring 검색으로 새지
않게"). `category_legs` 는 오직 `map_categories` 매핑 결과로만 채워지고, 그 매핑은
`decompose` 산출의 `categoryQueries` 만 본다(`filters.category` 자체는 안 본다) — 즉
`ProductSearchFilters.category` 하드필터는 **legs 를 거치지 않으면 실제 검색에 절대 도달하지
않는다.**

#371 R1 결정 당시엔 이 seam 을 `pair_runner` 전용 `_pair_decompose_json()`(`build_decompose_json`
결과에 `categoryQueries` 만 덧붙이는 래퍼)로 좁혀 뒀다 — 그때는 `runner.py` 본체(일반 관측 러너)가
`categoryQueries` 를 채우지 않아 `category` 필터축이 하네스 전체(#335 의 기존 55건 MFT 케이스
포함)에서 한 번도 실제 검색 경계에 도달하지 않았기 때문이다. **#381 D5 로 이 seam 이
`runner.py::build_decompose_json` 본체에 흡수됐다** — `category == "present"` 이면
`categoryQueries` 를 채우고(`_FILTER_SAMPLE["category"]` 공유), `_observe_chat` 도
`fakes.make_exact_match_category_mapping()`(raw exact match 만 대역 — 거리컷·택일·확장은 `#331`
소관, 재구현하지 않는다)을 쓴다. 이 러너는 이제 `_pair_decompose_json()` 래퍼 없이
`build_decompose_json()` 을 직접 쓴다 — 두 러너가 같은 seam 을 공유하므로 드리프트 여지가 없다.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import search_service  # noqa: E402
from evals.combo_matrix.fakes import (  # noqa: E402
    RecordingPush,
    make_exact_match_category_mapping,
    make_order_status_ok,
    make_popular,
    make_recording_filtering_search,
)
from evals.combo_matrix.loader import load_cases, load_pair_checks  # noqa: E402
from evals.combo_matrix.runner import (  # noqa: E402
    _identity_for,
    build_decompose_json,
    search_filters_projection,
)
from evals.combo_matrix.schema import ComboCase, PairCheckSpec  # noqa: E402
from tests.integration._stubs import ScriptedLLM  # noqa: E402
from tests.unit.test_recommendation import _collect, run_buyer_turn  # noqa: E402

_HERE = Path(__file__).resolve().parent
PAIR_CHECKS_MD_PATH = _HERE / "PAIR_CHECKS.md"

_SUPPORTED_SURFACE = "CHAT"
_SUPPORTED_INTENT = "recommend"
_SUPPORTED_CONSTRAINT_STRENGTH = "normal"
_SUPPORTED_DEGRADE = ("none", "embedding_missing", "rerank_failed")


class UnsupportedPairAxes(Exception):
    """v1 pair_runner 가 지원하지 않는 축 조합 — 조용히 잘못 실행하지 않고 명시적으로 실패한다."""


def _check_supported(case: ComboCase) -> None:
    axes = case.axes
    if axes.get("surface") != _SUPPORTED_SURFACE:
        raise UnsupportedPairAxes(
            f"{case.case_id}: surface={axes.get('surface')!r} 는 v1 pair_runner 미지원"
            f"(지원: {_SUPPORTED_SURFACE!r})"
        )
    if axes.get("intent") != _SUPPORTED_INTENT:
        raise UnsupportedPairAxes(
            f"{case.case_id}: intent={axes.get('intent')!r} 는 v1 pair_runner 미지원"
            f"(지원: {_SUPPORTED_INTENT!r})"
        )
    if axes.get("constraint_strength") != _SUPPORTED_CONSTRAINT_STRENGTH:
        raise UnsupportedPairAxes(
            f"{case.case_id}: constraint_strength={axes.get('constraint_strength')!r} 는 "
            f"v1 pair_runner 미지원(지원: {_SUPPORTED_CONSTRAINT_STRENGTH!r})"
        )
    if axes.get("degrade") not in _SUPPORTED_DEGRADE:
        raise UnsupportedPairAxes(
            f"{case.case_id}: degrade={axes.get('degrade')!r} 는 v1 pair_runner 미지원"
            f"(지원: {_SUPPORTED_DEGRADE})"
        )


def _freeze(value: object) -> object:
    """dict/list 필터 값을 해시 가능한 형태로(집합 연산용) — guard 비교 전용 헬퍼."""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _present_filter_items(search_filters: dict) -> set[tuple[str, object]]:
    return {
        (key, _freeze(value))
        for key, value in search_filters.items()
        if value not in (None, [], {}, "")
    }


# `reasons` 는 값이 채워져 있어도 `_entry_field_keys` 집합에서 제외한다(F1, 리뷰 R2) —
# rerank 성공(rationale 채움) vs 폴백(빈 리스트)의 차이는 정의된 동작이고, 그건 이미 "값은
# 비교하지 않는다"는 이 프로젝션의 경계다(§3-b 제외 필드 표 — reasons 값 제외와 같은 원칙).
_ENTRY_FIELD_KEYS_VALUE_EXCLUDED = frozenset({"reasons"})


def _entry_field_keys(entry) -> list[str]:
    """엔트리에 **값이 실제로 채워진** 필드 키만 담는다(F1, 리뷰 R2).

    `entry.model_dump(by_alias=True).keys()` 는 pydantic 이 값과 무관하게 스키마의 모든 필드를
    항상 담으므로, 그 키 집합은 같은 모델이면 언제나 `['label','listId','productIds','reasons']`
    라는 상수다 — invariant_fields 에 넣어도 어떤 회귀에도 깨질 수 없는 "검증하지 않는 검증"이
    된다. 값이 `None`/`[]`/`{}`/`""` 인 필드는 빼서, 예를 들어 `label` 이 한쪽 실행에만 채워지는
    회귀를 실제로 잡게 한다.
    """
    dumped = entry.model_dump(by_alias=True)
    return sorted(
        key
        for key, value in dumped.items()
        if key not in _ENTRY_FIELD_KEYS_VALUE_EXCLUDED and value not in (None, [], {}, "")
    )


@dataclass
class Projection:
    """쌍 실행 1턴에서 캡처하는 산출 프로젝션 (issue #371 §3-b)."""

    terminal: str | None
    finish_reason: str | None
    error_code: str | None
    search_filters: dict
    legs: list = field(default_factory=list)
    list_type: str | None = None
    lists_count: int = 0
    per_list_product_count: list[int] = field(default_factory=list)
    list_entry_field_keys: list[list[str]] = field(default_factory=list)
    product_ids_multiset: list[int] = field(default_factory=list)
    push_product_count: int = 0
    # D1(이슈 #381) — `search_filters` 에 값이 실려 있어도 그 축이 실제로 대역에 **적용됐다**는
    # 뜻은 아니다. [#426] 대역이 `SearchBackend` 자리로 내려가 8축이 전부 적용되면서 이 목록은
    # 비었지만, 미래에 표현 불가 축이 생기면 다시 채워지는 자리라 프로젝션에 남겨 둔다.
    unapplied_search_filters: list[str] = field(default_factory=list)
    # [#426] `attr_conditions` 는 Spring 에 안 나가는 AI 사후필터라 `search_filters` 로는 적용
    # 여부를 알 수 없다 — 사후필터 호출 자체를 계측해 "실제로 평가된 축인가"를 프로젝션에 싣는다
    # (runner.py::_observe_chat 와 같은 규약·같은 스파이). 값 모양: {invoked, inputCount, outputCount}.
    attr_conditions_post_filter: dict | None = None

    def as_dict(self) -> dict:
        return {
            "terminal": self.terminal,
            "finishReason": self.finish_reason,
            "errorCode": self.error_code,
            "searchFilters": self.search_filters,
            "unappliedSearchFilters": self.unapplied_search_filters,
            "attrConditionsPostFilter": self.attr_conditions_post_filter,
            "legs": self.legs,
            "listType": self.list_type,
            "listsCount": self.lists_count,
            "perListProductCount": self.per_list_product_count,
            "listEntryFieldKeys": self.list_entry_field_keys,
            "productIdsMultiset": self.product_ids_multiset,
            "pushProductCount": self.push_product_count,
        }


async def _execute(case: ComboCase) -> Projection:
    """케이스 1건을 결정론으로 실행해 프로젝션을 캡처한다 (원본·변형 공통 경로)."""
    _check_supported(case)
    axes = case.axes
    request = SimpleNamespace(
        session_id=f"pair-s-{case.case_id}",
        thread_id=f"pair-t-{case.case_id}",
        message=case.utterance,
    )
    identity = _identity_for(axes, case.case_id)
    decompose_json = build_decompose_json(axes)
    degrade = axes["degrade"]
    llm = ScriptedLLM(decompose=decompose_json, rerank_error=(degrade == "rerank_failed"))
    search = make_recording_filtering_search()
    push = RecordingPush()
    map_categories = make_exact_match_category_mapping()
    # [#393] 이 러너는 "Spring I-1 WHERE 계약 대역"으로 **필터 배관**(하드필터가 실제로 search
    # 콜러블에 도달하는가)을 잰다 — #393 의 최소 필터 가드(후보 **소스** 라우팅, payload 0개 턴을
    # I-3 로 돌림)는 아예 다른 축이다. 가드를 켜 두면 category/keyword/brand/color/price 가
    # 전부 absent 인 base arm(예: combo-0022, rating_min·total_budget 만 present)이 search 에
    # 도달하지 못해 위 `if not search.calls` 로 "미지원"이 돼 버려 그 축의 필터 배관을 아예 못
    # 잰다 — 그 turn 은 오늘 실제로 무필터 I-1 을 부르는 turn 이므로(#393 이 고치는 바로 그
    # 경우) 이 v1 하네스가 재려는 "필터가 배관을 타는가"라는 질문 자체가 성립하지 않는다.
    # 그래서 **이 실행 한정으로만** 가드를 끈다 — 케이스 정의·축 할당·기대값(`combo_cases.jsonl`
    # ·`expected/*.jsonl`)은 건드리지 않는다. 가드 자체의 회귀는 이 하네스가 아니라 #393 전용
    # 단위/통합 테스트(`tests/unit/test_search_guard_393.py`·`test_recommendation.py`)가 지킨다.
    # 자세한 배경은 README.md "알려진 관측 한계" 절 참조.
    settings = get_settings()
    guard_was_enabled = settings.search_filter_guard_enabled
    settings.search_filter_guard_enabled = False
    # [#426] attr_conditions 사후필터 계측 — runner.py::_observe_chat 와 같은 스파이·같은 근거
    # (`_apply_attr_conditions` 는 그 축이 truthy 일 때만 호출되고, 정의 모듈 전역 조회라 인기
    # 폴백 경로까지 한 번에 잡힌다).
    attr_post_filter_calls: list[tuple[int, int]] = []
    original_apply_attr_conditions = search_service._apply_attr_conditions

    def _apply_attr_conditions_spy(products, conditions):
        result = original_apply_attr_conditions(products, conditions)
        attr_post_filter_calls.append((len(products), len(result)))
        return result

    search_service._apply_attr_conditions = _apply_attr_conditions_spy
    try:
        events = await _collect(
            run_buyer_turn(
                request,
                identity,
                llm=llm,
                search=search,
                push_fn=push,
                popular_fn=make_popular(),
                order_status_fn=make_order_status_ok,
                map_categories=map_categories,
            )
        )
    finally:
        settings.search_filter_guard_enabled = guard_was_enabled
        search_service._apply_attr_conditions = original_apply_attr_conditions
    if not search.calls:
        raise UnsupportedPairAxes(
            f"{case.case_id}: search 콜러블이 호출되지 않았다 — v1 pair_runner 는 단일 검색 "
            "경로(무지정/popular 폴백 없음)만 지원한다"
        )
    # 파이프라인 통과 후 경계 도달값이 요지(§3-b) — 첫 호출(주 검색)을 쓴다. v1 지원 범위(정상
    # 후보 수, constraint_strength=normal)에서는 무필터 보충 재검색(0건일 때만 도는 분기)이 돌지
    # 않아 항상 정확히 1회 호출이지만, 방어적으로 첫 호출을 명시한다.
    first_call_filters = search.calls[0]
    search_filters = search_filters_projection(first_call_filters)
    # 러너(runner.py::_observe_chat)와 같은 규약 — 첫 호출(주 검색) 기준, D1(#381).
    unapplied_search_filters = search.unapplied_calls[0] if search.unapplied_calls else []

    terminal = events[-1] if events else None
    lists = push.pushes[0].lists if push.pushes else []
    all_product_ids = [pid for entry in lists for pid in entry.product_ids]
    # `make_exact_match_category_mapping` 이 실제로 받은 category_queries 개수만큼 호출된다(보통
    # 0회 또는 1회 — #217 전개 재호출은 unresolved 가 있을 때만인데 raw exact match 는 unresolved 를
    # 안 낸다). 마지막 호출의 legs 를 쓴다 — 여러 번 불려도 최종 결정은 마지막 호출의 것이다.
    legs = list(map_categories.calls[-1].legs) if map_categories.calls else []
    return Projection(
        terminal=terminal["type"] if terminal else None,
        finish_reason=(
            terminal["data"].get("finishReason")
            if terminal and terminal["type"] == "done"
            else None
        ),
        error_code=(
            terminal["data"].get("code") if terminal and terminal["type"] == "error" else None
        ),
        search_filters=search_filters,
        unapplied_search_filters=unapplied_search_filters,
        attr_conditions_post_filter=(
            {
                "invoked": bool(attr_post_filter_calls),
                "inputCount": attr_post_filter_calls[0][0] if attr_post_filter_calls else 0,
                "outputCount": attr_post_filter_calls[0][1] if attr_post_filter_calls else 0,
            }
            if axes.get("attr_conditions") == "present"
            else None
        ),
        legs=legs,
        list_type=push.pushes[0].list_type if push.pushes else None,
        lists_count=len(lists),
        per_list_product_count=[len(entry.product_ids) for entry in lists],
        list_entry_field_keys=[_entry_field_keys(entry) for entry in lists],
        product_ids_multiset=sorted(all_product_ids),
        push_product_count=len(all_product_ids),
    )


_METRIC_EXTRACTORS = {
    "push_product_count": lambda projection: projection.push_product_count,
}


def _guard_perturbed_filters_strict_superset(
    base: Projection, perturbed: Projection, _spec: PairCheckSpec
) -> bool:
    base_items = _present_filter_items(base.search_filters)
    perturbed_items = _present_filter_items(perturbed.search_filters)
    return base_items < perturbed_items  # 진상위집합 = perturbed 가 base 를 진부분집합으로 포함


def _guard_base_count_positive(
    base: Projection, _perturbed: Projection, spec: PairCheckSpec
) -> bool:
    assert spec.metric is not None  # PairCheckSpec 검증자가 ci+DIR 에 metric 필수를 강제
    return _METRIC_EXTRACTORS[spec.metric](base) > 0


_GUARD_CHECKS = {
    "perturbed_filters_strict_superset": _guard_perturbed_filters_strict_superset,
    "base_count_positive": _guard_base_count_positive,
}


@dataclass
class PairResult:
    spec: PairCheckSpec
    status: str  # "pass" | "fail" | "manual"
    base_case_id: str | None = None
    base_projection: dict | None = None
    perturbed_projection: dict | None = None
    metric_base: object | None = None
    metric_perturbed: object | None = None
    guard_results: dict[str, bool] | None = None
    mismatched_fields: list[str] | None = None


async def run_pair_checks(
    specs: list[PairCheckSpec], cases_by_id: dict[str, ComboCase]
) -> list[PairResult]:
    """`pair_checks.jsonl` 전 행을 실행한다 — manual 은 실행 없이 결과에 포함한다(조용한 탈락 금지)."""
    results: list[PairResult] = []
    for spec in specs:
        if spec.mode == "manual":
            results.append(PairResult(spec=spec, status="manual"))
            continue

        perturbed_case = cases_by_id[spec.case_id]
        base_case_id = perturbed_case.perturbation_of
        if base_case_id is None:
            raise UnsupportedPairAxes(f"{spec.case_id}: perturbation_of 없음 — 쌍 검증 불가")
        base_case = cases_by_id[base_case_id]

        base_projection = await _execute(base_case)
        perturbed_projection = await _execute(perturbed_case)

        if spec.kind == "INV":
            assert spec.invariant_fields is not None
            base_dict = base_projection.as_dict()
            perturbed_dict = perturbed_projection.as_dict()
            mismatched = [
                field_name
                for field_name in spec.invariant_fields
                if base_dict[field_name] != perturbed_dict[field_name]
            ]
            results.append(
                PairResult(
                    spec=spec,
                    status="pass" if not mismatched else "fail",
                    base_case_id=base_case_id,
                    base_projection=base_dict,
                    perturbed_projection=perturbed_dict,
                    mismatched_fields=mismatched,
                )
            )
        else:  # DIR
            assert spec.metric is not None
            metric_fn = _METRIC_EXTRACTORS[spec.metric]
            base_value = metric_fn(base_projection)
            perturbed_value = metric_fn(perturbed_projection)
            direction_ok = (
                perturbed_value <= base_value
                if spec.direction == "non_increase"
                else perturbed_value >= base_value
            )
            guard_results = {
                guard: _GUARD_CHECKS[guard](base_projection, perturbed_projection, spec)
                for guard in spec.guards
            }
            status = "pass" if direction_ok and all(guard_results.values()) else "fail"
            results.append(
                PairResult(
                    spec=spec,
                    status=status,
                    base_case_id=base_case_id,
                    base_projection=base_projection.as_dict(),
                    perturbed_projection=perturbed_projection.as_dict(),
                    metric_base=base_value,
                    metric_perturbed=perturbed_value,
                    guard_results=guard_results,
                )
            )
    return results


def _axis_diff(base_case: ComboCase, perturbed_case: ComboCase) -> dict[str, tuple[str, str]]:
    diff = {}
    for axis, base_value in base_case.axes.items():
        perturbed_value = perturbed_case.axes.get(axis)
        if perturbed_value != base_value:
            diff[axis] = (base_value, perturbed_value)
    return diff


def render_pair_checks_md(results: list[PairResult], cases_by_id: dict[str, ComboCase]) -> str:
    total = len(results)
    inv_ci = [r for r in results if r.spec.kind == "INV" and r.spec.mode == "ci"]
    dir_ci = [r for r in results if r.spec.kind == "DIR" and r.spec.mode == "ci"]
    manual = [r for r in results if r.spec.mode == "manual"]
    inv_pass = sum(1 for r in inv_ci if r.status == "pass")
    dir_pass = sum(1 for r in dir_ci if r.status == "pass")

    lines = [
        "# INV/DIR 쌍 실검증 결과 (이슈 #371)",
        "",
        "> 생성: `evals/combo_matrix/pair_runner.py::render_pair_checks_md` — "
        "`expected/pair_checks.jsonl` 을 실행해 자동 생성된다. 손으로 고치지 말 것(드리프트 방지).",
        "",
        f"- INV 통과: {inv_pass}/{len(inv_ci)}",
        f"- DIR(ci) 통과: {dir_pass}/{len(dir_ci)}",
        f"- manual 분리: {len(manual)}건",
        f"- 분모(전체 쌍 행 수): {total}",
        "",
    ]

    for result in results:
        spec = result.spec
        perturbed_case = cases_by_id[spec.case_id]
        lines.append(f"## {spec.case_id} ({spec.kind}, mode={spec.mode})")
        lines.append("")
        if result.status == "manual":
            lines.append("- **상태**: manual — 실행 안 함")
            lines.append(f"- **사유**: {spec.reason}")
            lines.append(f"- **소관(link)**: {spec.link}")
            lines.append("")
            continue

        base_case = cases_by_id[result.base_case_id]
        diff = _axis_diff(base_case, perturbed_case)
        diff_desc = ", ".join(f"`{axis}: {b}→{p}`" for axis, (b, p) in sorted(diff.items()))
        lines.append(f"- **원본**: {result.base_case_id}")
        lines.append(f"- **축 diff**: {diff_desc}")
        lines.append(f"- **검증 정의**: {spec.reason}")
        if spec.kind == "INV":
            lines.append(f"- **invariant_fields**: {spec.invariant_fields}")
            lines.append(f"- **불일치 필드**: {result.mismatched_fields}")
        else:
            lines.append(f"- **metric**: {spec.metric} · **direction**: {spec.direction}")
            lines.append(f"- **metric(base)**: {result.metric_base}")
            lines.append(f"- **metric(perturbed)**: {result.metric_perturbed}")
            lines.append(f"- **guards**: {result.guard_results}")
        lines.append(f"- **base 프로젝션**: `{result.base_projection}`")
        lines.append(f"- **perturbed 프로젝션**: `{result.perturbed_projection}`")
        lines.append(f"- **verdict**: {result.status}")
        lines.append("")

    return "\n".join(lines)


async def generate_pair_checks_md(write: bool = True) -> tuple[str, list[PairResult]]:
    specs = load_pair_checks()
    cases_by_id = {c.case_id: c for c in load_cases()}
    results = await run_pair_checks(specs, cases_by_id)
    text = render_pair_checks_md(results, cases_by_id)
    if write:
        # LF 고정 — `test_pair_checks_md_regeneration_matches_committed` 가 바이트 동일을
        # 요구하므로 줄바꿈이 갈리면 재생성한 사람만 통과한다(`__main__.py::regenerate` 주석 참조).
        PAIR_CHECKS_MD_PATH.write_text(text, encoding="utf-8", newline="\n")
    return text, results


def _summary(results: list[PairResult]) -> dict:
    inv_ci = [r for r in results if r.spec.kind == "INV" and r.spec.mode == "ci"]
    dir_ci = [r for r in results if r.spec.kind == "DIR" and r.spec.mode == "ci"]
    manual = [r for r in results if r.spec.mode == "manual"]
    return {
        "invPass": f"{sum(1 for r in inv_ci if r.status == 'pass')}/{len(inv_ci)}",
        "dirCiPass": f"{sum(1 for r in dir_ci if r.status == 'pass')}/{len(dir_ci)}",
        "manual": len(manual),
        "total": len(results),
        "results": [
            {"caseId": r.spec.case_id, "kind": r.spec.kind, "mode": r.spec.mode, "status": r.status}
            for r in results
        ],
    }


def main() -> int:
    _, results = asyncio.run(generate_pair_checks_md(write=True))
    print(json.dumps(_summary(results), ensure_ascii=False, indent=2))
    print(f"wrote {PAIR_CHECKS_MD_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
