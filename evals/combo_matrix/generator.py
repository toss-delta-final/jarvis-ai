"""결정론 greedy pairwise(2-wise) + 위험축쌍 3-wise 케이스 생성기 (이슈 #335 §3).

외부 의존성(allpairspy 등) 없이 표준 라이브러리만 쓴다. 같은 `axes.json` + 같은 seed 는
항상 바이트 동일 `combo_cases.jsonl` 을 낸다 — dict 순회 순서 등 비결정 원천은 전부 정렬로 없앤다.

## 알고리즘

1. **제약 인지 완전열거(leaf enumeration)** — `axes.json` 의 `forces`/`requires_any_present`/
   `excludes` 제약을 만족하는 "완전 할당"(축 17개 전부에 값이 있는 dict) 전부를 결정론 DFS 로
   나열한다. 8개 필터 축(`category`.._filter_axes) 은 이진(absent/present)이라 원소 그대로
   순회하면 2**8=256 가지가 나오는데, 서로 다른 필터쌍(i,j) 의 4 조합(AA/AP/PA/PP)을 모두
   보려면 **all_present + singleton_present(k) 8종 = 9패턴**이면 충분하다(표준 pairwise 축소 —
   singleton_present(k) 가 k 이외의 모든 축을 absent 로 두므로 k∉{i,j} 인 8종 중 항상 하나가
   (i=absent,j=absent) 를 만들어 준다). 이 축소는 **생성 후보 축소**일 뿐 커버리지 분모 계산에는
   영향 없다 — 분모는 이 축소된 leaf 전체에서 실제로 나타나는 쌍의 집합이다(사후 필터가 아니라
   생성 단계 축소, §3 "제약 위반 조합은 생성 단계에서 배제").
2. **탐욕(greedy) 선택** — 매 라운드 leaf 풀 전체를 스캔해 "현재 미커버 쌍을 가장 많이 새로
   덮는" 하나를 케이스로 채택한다(시드 고정 `random.Random` 은 최초 tie-break 순서만 정한다).
   미커버 쌍이 없어지거나 한 라운드의 최대 gain 이 0이면 종료.
3. **위험 3-wise 승격** — `riskTriples` 에 지정된 축 3개 조합은 별도로 "미커버 3-wise 튜플"
   집합을 추적해 위 1과 같은 방식으로 추가 라운드를 돌려 채운다.
4. **INV/DIR 파생 케이스** — 이미 채택된 MFT 케이스 중 짝을 지어 몇 건의 저비용 INV/DIR 케이스를
   덧붙인다(§3 "INV·DIR 케이스 계열을 싸게 추가"). `perturbation_of` 로 원본을 가리킨다.

사후 필터로 인한 "커버 안 된 쌍"이 생기면 조용히 버리지 않고 `coverage_summary()` 가 정직하게
남긴다(§3 "조용한 truncation 금지").
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from itertools import combinations

from evals.combo_matrix.schema import AxesDocument, ChecklistType, ComboCase


def _stable_hash(text: str) -> int:
    """`hash(str)` 대신 쓰는 프로세스-불변 해시 — PYTHONHASHSEED 랜덤화를 안 탄다(재현성 §3)."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big")


FILTER_AXES = (
    "category",
    "price_min",
    "price_max",
    "brand",
    "rating_min",
    "keyword",
    "color",
    "attr_conditions",
)

# DFS 축 순서 — forces 제약의 `if` 축이 항상 그 `then` 축보다 앞에 오도록 고정한다.
AXIS_ORDER = (
    "surface",
    "intent",
    "constraint_strength",
    "degrade",
    "identity",
    "context",
    "case",
    "buy_all",
    "total_budget",
    *FILTER_AXES,
)

NA = "n/a"


def _match(cond: dict[str, list[str]], assignment: dict[str, str]) -> bool:
    return all(assignment.get(axis) in values for axis, values in cond.items())


class ConstraintIndex:
    """axes.json 제약을 축별로 색인해 DFS 중 반복 스캔을 피한다."""

    def __init__(self, doc: AxesDocument) -> None:
        self.forces = [c for c in doc.constraints if c.type == "forces"]
        self.requires_any_present = [c for c in doc.constraints if c.type == "requires_any_present"]
        self.excludes = [c for c in doc.constraints if c.type == "excludes"]

    def effective_domain(
        self, axis: str, full_domain: list[str], assignment: dict[str, str]
    ) -> list[str]:
        """`assignment` 에 이미 배정된 축들을 조건으로 이 축의 유효 도메인을 좁힌다."""
        forced: set[str] | None = None
        for rule in self.forces:
            if axis not in rule.then:
                continue
            # if 축이 전부 이미 배정돼야 조건을 평가할 수 있다(AXIS_ORDER 가 이를 보장).
            if not all(a in assignment for a in rule.if_):
                continue
            if _match(rule.if_, assignment):
                value = rule.then[axis]
                forced = {value} if forced is None else (forced & {value})
        if forced is not None:
            return [v for v in full_domain if v in forced]
        # `n/a` 는 다른 축이 강제할 때만 유효한 센티널이다 — 강제가 없으면 자유 선택지에서 뺀다
        # (그러지 않으면 예: surface=CHAT 인데 intent=n/a 같은 무의미한 조합이 새어 들어온다).
        return [v for v in full_domain if v != NA]

    def is_valid_leaf(self, assignment: dict[str, str]) -> bool:
        for rule in self.requires_any_present:
            if _match(rule.if_, assignment) and not any(
                assignment.get(a) == "present" for a in rule.axes
            ):
                return False
        for rule in self.excludes:
            if _match(rule.if_, assignment) and _match(
                {k: v for k, v in rule.forbid.items()}, assignment
            ):
                return False
        return True


def enumerate_leaves(doc: AxesDocument) -> list[dict[str, str]]:
    """제약을 만족하는 모든 완전 할당을 결정론 순서로 나열한다(§3 알고리즘 1)."""
    axis_domain = {a.id: a.value_ids() for a in doc.axes}
    idx = ConstraintIndex(doc)
    leaves: list[dict[str, str]] = []

    def domain_for(axis: str, assignment: dict[str, str]) -> list[str]:
        if axis in FILTER_AXES:
            base = idx.effective_domain(axis, axis_domain[axis], assignment)
            if base == [NA]:
                return base
            # 필터 축이 자유이면(강제 없음) 전체 2**8 대신 9패턴으로 축소(모듈 docstring 참조).
            return base
        return idx.effective_domain(axis, axis_domain[axis], assignment)

    def filters_free(assignment: dict[str, str]) -> bool:
        # 강제(forces)가 걸리면 도메인이 항상 단일값("n/a" 또는 "absent")으로 좁혀진다 —
        # 그 어느 쪽이든 "자유"가 아니므로 도메인 크기로 판정한다(`!= [NA]` 만 보면
        # constraint_strength=unspecified 의 "absent 로 강제" 를 자유로 오판한다).
        return len(domain_for(FILTER_AXES[0], assignment)) > 1

    def dfs(i: int, assignment: dict[str, str]) -> None:
        if i == len(AXIS_ORDER):
            if idx.is_valid_leaf(assignment):
                leaves.append(dict(assignment))
            return
        axis = AXIS_ORDER[i]
        if axis == FILTER_AXES[0] and filters_free(assignment):
            # 8개 필터 축을 개별 DFS 로 펼치지 않고 9패턴을 한 번에 배정한다.
            for pattern in _filter_patterns():
                assignment.update(pattern)
                dfs(i + len(FILTER_AXES), assignment)
            for axis_name in FILTER_AXES:
                assignment.pop(axis_name, None)
            return
        for value in domain_for(axis, assignment):
            assignment[axis] = value
            dfs(i + 1, assignment)
        assignment.pop(axis, None)

    dfs(0, {})
    leaves.sort(key=lambda a: tuple(a[axis] for axis in AXIS_ORDER))
    return leaves


def _filter_patterns() -> list[dict[str, str]]:
    all_present: dict[str, str] = {axis: "present" for axis in FILTER_AXES}
    patterns: list[dict[str, str]] = [all_present]
    for k in FILTER_AXES:
        pattern: dict[str, str] = {axis: "absent" for axis in FILTER_AXES}
        pattern[k] = "present"
        patterns.append(pattern)
    return patterns


def all_pairs(leaf: dict[str, str], axes: tuple[str, ...] = AXIS_ORDER) -> set[tuple]:
    pairs = set()
    present_axes = [a for a in axes if a in leaf]
    for a, b in combinations(sorted(present_axes), 2):
        pairs.add((a, leaf[a], b, leaf[b]))
    return pairs


def all_triples(leaf: dict[str, str], axes: tuple[str, ...]) -> tuple:
    ordered = sorted(axes)
    return tuple((axis, leaf[axis]) for axis in ordered)


@dataclass
class GenerationResult:
    leaves_total: int
    valid_pairs: set[tuple]
    selected: list[dict[str, str]] = field(default_factory=list)
    risk_triple_targets: dict[str, set[tuple]] = field(default_factory=dict)
    risk_triple_covered: dict[str, set[tuple]] = field(default_factory=dict)
    uncovered_pairs: set[tuple] = field(default_factory=set)


def greedy_cover(
    leaves: list[dict[str, str]],
    rng: random.Random,
    uncovered: set,
    pair_fn,
) -> list[dict[str, str]]:
    """전체 leaf 풀을 매 라운드 스캔해 미커버 쌍을 가장 많이 새로 덮는 leaf 를 채택한다.

    `valid_pairs`(분모)는 leaf 들의 합집합으로 정의되므로, 미커버 쌍이 남아 있는 한 그 쌍을
    낸 leaf 자체가 항상 gain>=1 후보로 스캔에 걸린다 — 무작위 표본만으로 조기 종료(early
    break)하면 희소해진 후반 라운드에서 표본이 우연히 그 leaf 를 놓쳐 **커버 가능한데 못 덮은
    쌍**이 조용히 남는다(실측: 표본 40건 방식은 66/1072 쌍을 놓쳤다). `pair_fn` 은 leaf 당 1회만
    호출해 캐시하고(라운드마다 다시 계산하지 않는다), 매 라운드는 그 캐시 ∩ uncovered 만 본다 —
    leaf 수 수천 수준에서 O(rounds × leaves) 가 수 초 내로 끝난다. 동점 시 결정론 tie-break 는
    `rng` 로 미리 섞어 둔 순서를 쓴다(재현성 §3 "바이트 동일 출력").
    """
    order = list(leaves)
    rng.shuffle(order)
    cached = [(leaf, pair_fn(leaf)) for leaf in order]
    selected: list[dict[str, str]] = []
    while uncovered:
        best_leaf = None
        best_gain = -1
        best_new: set = set()
        for leaf, pairs in cached:
            new = pairs & uncovered
            gain = len(new)
            if gain > best_gain:
                best_gain = gain
                best_leaf = leaf
                best_new = new
        if best_leaf is None or best_gain <= 0:
            break
        selected.append(best_leaf)
        uncovered -= best_new
    return selected


def generate(doc: AxesDocument) -> GenerationResult:
    leaves = enumerate_leaves(doc)
    valid_pairs: set[tuple] = set()
    for leaf in leaves:
        valid_pairs |= all_pairs(leaf)

    rng = random.Random(doc.seed)
    uncovered = set(valid_pairs)
    selected = greedy_cover(leaves, rng, uncovered, all_pairs)

    result = GenerationResult(
        leaves_total=len(leaves), valid_pairs=valid_pairs, selected=list(selected)
    )
    result.uncovered_pairs = set(uncovered)

    # 위험 3-wise 승격 — 지정된 축 3개 조합의 유효 튜플을 전부 커버할 때까지 추가 라운드.
    for rt in doc.risk_triples:
        axes = tuple(rt.axes)
        targets: set[tuple] = set()
        for leaf in leaves:
            # leaf 는 항상 완전 할당(17축 전부 존재)이므로 이 조건은 사실상 상수 True 다 —
            # 의도("축 3개가 전부 leaf 에 존재")를 정확히 쓴다(리뷰 R5, 이전엔 `leaf.get(a, NA)`
            # 가 항상 truthy 문자열이라 무의미했다).
            if all(a in leaf for a in axes):
                targets.add(all_triples(leaf, axes))
        covered = {all_triples(leaf, axes) for leaf in result.selected}
        remaining = targets - covered
        # `hash(str)` 는 PYTHONHASHSEED 에 따라 프로세스마다 달라져(보안 기능) 재현성을 깬다 —
        # 실측: 같은 seed 로 두 번 돌렸는데 casesSha256 이 달랐다. 안정 해시(sha256)만 쓴다.
        rt_rng = random.Random(doc.seed ^ _stable_hash(rt.id))
        extra = greedy_cover(
            leaves, rt_rng, remaining, lambda leaf, axes=axes: {all_triples(leaf, axes)}
        )
        for leaf in extra:
            if leaf not in result.selected:
                result.selected.append(leaf)
        result.risk_triple_targets[rt.id] = targets
        result.risk_triple_covered[rt.id] = {
            all_triples(leaf, axes) for leaf in result.selected
        } & targets

    # 최종 결정론 순서 — 축값 튜플 사전순(재현성 §3 "바이트 동일 출력").
    result.selected.sort(key=lambda a: tuple(a[axis] for axis in AXIS_ORDER))
    return result


# ─────────── 발화 예시 (한국어) ───────────

_INTENT_UTTERANCE = {
    "recommend": None,  # 아래 case/필터 조합으로 구성
    "cart_add": "이거 장바구니에 담아줘",
    "cart_view": "장바구니 보여줘",
    "order_status": "내가 주문한 거 어떻게 됐어?",
    "general": "너는 뭘 할 수 있어?",
    "cart_remove": "방금 담은 거 빼줘",
    "wishlist_add": "이거 찜해줘",
    "wishlist_remove": "찜 목록에서 빼줘",
    "wishlist_view": "내가 뭐 찜했지?",
}

_FILTER_PHRASES = {
    "category": "무선 이어폰",
    "price_min": "3만원 이상",
    "price_max": "5만원 이하",
    "brand": "나이키",
    "rating_min": "평점 4점 이상",
    "keyword": "가벼운",
    "color": "검정색",
    "attr_conditions": "방수 되는",
}

_CONTEXT_PHRASE = {
    "none": "",
    "lastRecommendations": "아까 그 중에서 ",
    "pendingCart": "그거 검정색으로 ",
    "categoryPrior": "그거 말고 ",
}


def build_utterance(axes: dict[str, str]) -> str:
    """축 할당을 자연스럽게 실현하는 한국어 발화 예시 1건(§3)."""
    intent = axes["intent"]
    if axes["surface"] == "HOME":
        return "(HOME 지면 — 발화 없음, I-22 콜백)"
    if intent != "recommend":
        return _CONTEXT_PHRASE.get(axes.get("context", "none"), "") + _INTENT_UTTERANCE[intent]

    parts: list[str] = [_CONTEXT_PHRASE.get(axes.get("context", "none"), "")]
    present_filters = [f for f in FILTER_AXES if axes.get(f) == "present"]
    if axes["constraint_strength"] == "unspecified":
        parts.append("아무거나 추천해줘")
    else:
        parts.extend(_FILTER_PHRASES[f] for f in present_filters)
        case = axes.get("case", "2")
        if case == "1":
            parts.append("무선 이어폰")
        elif case == "3":
            parts.append("집들이 선물")
        parts.append("추천해줘")
    if axes.get("total_budget") == "present":
        parts.append("(총 5만원 안에서)")
    if axes.get("buy_all") == "true":
        parts.append("(다 사줘)")
    return " ".join(p for p in parts if p)


# ─────────── 케이스 조립 ───────────

# context 가 멀티턴 승계를 요구하는 값이면 "발화→라우팅 실측"이 필요해 manual 이다(§5) —
# 이 하네스는 항상 decompose 산출을 고정 주입하므로, 그 주입이 맞는지(자연어 해석)는 여기서
# 재지 않고 intent_probe 셀로 연결만 한다.
_MANUAL_LINK = {
    "lastRecommendations": "intent_probe:demonstrative",
    "pendingCart": "intent_probe:option_answer",
    "categoryPrior": "intent_probe:category_action",
}


def assemble_cases(result: GenerationResult) -> list[ComboCase]:
    """선택된 leaf 를 `combo-NNNN` 케이스로 조립한다(§3) — 생성 순서 = leaf 결정론 정렬 순서."""
    cases: list[ComboCase] = []
    for i, axes in enumerate(result.selected, start=1):
        context = axes.get("context", NA)
        manual = context in _MANUAL_LINK
        cases.append(
            ComboCase(
                case_id=f"combo-{i:04d}",
                axes=dict(axes),
                utterance=build_utterance(axes),
                checklist_type="MFT",
                label_required=True,
                observation_mode="manual" if manual else "ci",
                linked=_MANUAL_LINK.get(context) if manual else None,
            )
        )
    return cases


def _find_base(cases: list[ComboCase], predicate) -> ComboCase | None:
    for case in cases:
        if predicate(case.axes):
            return case
    return None


def add_perturbations(doc: AxesDocument, cases: list[ComboCase]) -> list[ComboCase]:
    """저비용 INV/DIR 케이스 3건을 기존 MFT 케이스에서 파생한다(§3 "싸게 추가").

    라벨(정답) 없이 **관계**만 주장하므로 `label_required=False` — 원본은 `perturbation_of`.
    """
    idx = ConstraintIndex(doc)
    next_n = len(cases) + 1
    extra: list[ComboCase] = []

    def _emit(
        base: ComboCase, overrides: dict[str, str], checklist_type: ChecklistType, note: str
    ) -> None:
        nonlocal next_n
        axes = {**base.axes, **overrides}
        if not idx.is_valid_leaf(axes):
            return
        extra.append(
            ComboCase(
                case_id=f"combo-{next_n:04d}",
                axes=axes,
                utterance=f"{build_utterance(axes)} [{note}]",
                checklist_type=checklist_type,
                label_required=False,
                observation_mode="ci",
                perturbation_of=base.case_id,
            )
        )
        next_n += 1

    # DIR — 필터를 추가하면 결과 수가 늘지 않는다(단조 감소/유지).
    base = _find_base(
        cases,
        lambda a: (
            a.get("intent") == "recommend"
            and a.get("constraint_strength") in ("normal", "overspecified_zero")
            and any(a.get(f) == "absent" for f in FILTER_AXES)
        ),
    )
    if base is not None:
        absent_filter = next(f for f in FILTER_AXES if base.axes.get(f) == "absent")
        _emit(base, {absent_filter: "present"}, "DIR", "필터 추가 → 결과 수 비증가")

    # DIR — 회원 recall >= 게스트 recall (#119 선례).
    base = _find_base(
        cases, lambda a: a.get("intent") == "recommend" and a.get("identity") == "guest"
    )
    if base is not None:
        _emit(base, {"identity": "member"}, "DIR", "회원 recall >= 게스트 recall(#119)")

    # INV — rerank 실패 degrade 에서도 push 계약 형태(listType/필드) 불변.
    base = _find_base(
        cases,
        lambda a: (
            a.get("intent") == "recommend"
            and a.get("degrade") == "none"
            and a.get("constraint_strength") != "overspecified_zero"
        ),
    )
    if base is not None:
        _emit(base, {"degrade": "rerank_failed"}, "INV", "push 계약 형태 불변")

    return cases + extra


def complete_assignment(doc: AxesDocument, partial: dict[str, str]) -> dict[str, str]:
    """부분 축 할당을 axes.json 제약이 함의하는 값으로 결정론 완성한다(§ DirectedCase, 리뷰 R3).

    `AXIS_ORDER` 순서로 채운다 — forces 제약의 `if` 축이 그 `then` 축보다 항상 앞이라
    (`enumerate_leaves` 와 같은 전제), 아직 안 채운 축은 이미 채워진 축들만으로 유효 도메인이
    정해진다. 여러 값이 남으면(강제되지 않은 자유 축) **도메인의 첫 값**을 결정론으로 고른다 —
    `axes.json` 배열 순서 자체가 재현 가능한 정본이므로 임의성이 없다.
    """
    idx = ConstraintIndex(doc)
    axis_domain = {a.id: a.value_ids() for a in doc.axes}
    assignment = dict(partial)
    for axis in AXIS_ORDER:
        if axis in assignment:
            continue
        domain = idx.effective_domain(axis, axis_domain[axis], assignment)
        if not domain:
            raise ValueError(f"axis {axis!r} has no valid value given {assignment!r}")
        assignment[axis] = domain[0]
    return assignment


def add_directed_cases(doc: AxesDocument, cases: list[ComboCase]) -> list[ComboCase]:
    """`axes.json` `directedCases` 를 결정론으로 덧붙인다 — greedy 가 안 뽑은 특정 조합을
    데이터로 못박아 직접 관측하기 위함(리뷰 R3). 제약 위반이면(축 조합이 실제로 불가능해지면)
    조용히 버리지 않고 예외로 실패시켜 데이터 오류를 바로 드러낸다.
    """
    next_n = len(cases) + 1
    idx = ConstraintIndex(doc)
    extra: list[ComboCase] = []
    for directed in doc.directed_cases:
        axes = complete_assignment(doc, directed.axes)
        if not idx.is_valid_leaf(axes):
            raise ValueError(
                f"directedCases[{directed.id!r}] 이 axes.json 제약을 위반한다: {axes!r}"
            )
        context = axes.get("context", NA)
        manual = context in _MANUAL_LINK
        reason_tag = directed.reason if len(directed.reason) <= 40 else directed.reason[:40] + "…"
        extra.append(
            ComboCase(
                case_id=f"combo-{next_n:04d}",
                axes=axes,
                utterance=f"{build_utterance(axes)} [directed: {reason_tag}]",
                checklist_type="MFT",
                label_required=True,
                observation_mode="manual" if manual else "ci",
                linked=_MANUAL_LINK.get(context) if manual else None,
            )
        )
        next_n += 1
    return cases + extra
