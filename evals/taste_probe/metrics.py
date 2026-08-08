"""축 정의와 채점 (#462).

정의 문장을 **코드 옆 데이터로** 둔다 — `AxisResult` 는 산출물(`results.json`·`report.md`)에 그대로
실린다. 숫자가 정의 없이 돌아다니면 같은 이름의 지표가 다른 뜻으로 비교되는 사고가 난다
(`evals/README.md` 8항 — 지표는 분자·분모 정의 동봉).

**다중 비교 통제**(#328 규약 5): primary confirmatory 는 `recall` 하나, 사전 등록 2차는
`noiseFalsePositiveRate`·`nodeIdAgreement` 둘까지(`exploratory=False`). 나머지 축과 슬라이스
분해는 전부 `exploratory=True` 를 스스로 단다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.agents.profile.graph_models import normalize_label
from evals.taste_probe.runner import ProducedTriple, SessionResult
from evals.taste_probe.schema import ExpectedTriple, GoldenSet


@dataclass(frozen=True)
class AxisResult:
    axis_id: str
    title: str
    numerator: int
    denominator: int
    expected_denominator: int
    unfilled_sample_count: int
    definition_numerator: str
    definition_denominator: str
    exploratory: bool = False

    @property
    def ratio(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "axisId": self.axis_id,
            "title": self.title,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "expectedDenominator": self.expected_denominator,
            "unfilledSampleCount": self.unfilled_sample_count,
            "ratio": self.ratio,
            "exploratory": self.exploratory,
            "definition": {
                "numerator": self.definition_numerator,
                "denominator": self.definition_denominator,
            },
        }


MatchResult = tuple[list[tuple[int, int]], list[int], list[int]]


def _match_edges(expected: list[ExpectedTriple], produced: list[ProducedTriple]) -> list[list[int]]:
    """`expected[i]` 가 붙을 수 있는 `produced` 인덱스 목록(§7 매칭 규칙 — 변경 없음).

    매칭 조건: `produced.node.type == expected.kind` 그리고 `produced.predicate ==
    expected.predicate` 그리고 `normalize_label(produced.node.label) ∈
    {normalize_label(a) for a in expected.accept}`.
    """
    edges: list[list[int]] = []
    for expected_triple in expected:
        accept_norms = {normalize_label(a) for a in expected_triple.accept}
        edges.append(
            [
                p_idx
                for p_idx, produced_triple in enumerate(produced)
                if produced_triple.node_type == expected_triple.kind
                and produced_triple.predicate == expected_triple.predicate
                and normalize_label(produced_triple.node_label) in accept_norms
            ]
        )
    return edges


def match_sample(expected: list[ExpectedTriple], produced: list[ProducedTriple]) -> MatchResult:
    """최대 이분 매칭(Kuhn 알고리즘, 증강 경로) — §7 매칭 규칙(A-13).

    **왜 그리디가 아니라 최대 매칭인가**: 그리디 1:1 매칭은 배열 순서에 매칭 크기가 의존한다 —
    `E1.accept={A,B}`, `E2.accept={A}`, `produced=[A,B]` 일 때 `[E1,E2]` 순서는 1개만 매칭하지만
    `[E2,E1]` 순서는 2개 모두 매칭한다(JSON 라벨 순서만 바꿔도 recall 50%↔100%). 최대 매칭의
    **크기**는 그래프 이론상 입력 순서와 무관하게 유일하게 정해진다 — 이 채점기가 보고하는 수치
    (매칭 개수, 그리고 그로부터 파생되는 recall·missRate·sessionExactSet)는 순서 불변이다(어느
    특정 쌍이 짝지어지는지는 동률에서 갈릴 수 있으나, 크기는 갈리지 않는다).

    반환: `(matched_pairs, unmatched_expected_indices, unmatched_produced_indices)`.
    """
    edges = _match_edges(expected, produced)
    match_of_produced: dict[int, int] = {}  # produced idx -> expected idx

    def _try_augment(e_idx: int, visited: set[int]) -> bool:
        for p_idx in edges[e_idx]:
            if p_idx in visited:
                continue
            visited.add(p_idx)
            if p_idx not in match_of_produced or _try_augment(match_of_produced[p_idx], visited):
                match_of_produced[p_idx] = e_idx
                return True
        return False

    for e_idx in range(len(expected)):
        _try_augment(e_idx, set())

    matched_pairs = sorted((e_idx, p_idx) for p_idx, e_idx in match_of_produced.items())
    matched_expected_idx = {pair[0] for pair in matched_pairs}
    unmatched_expected = [i for i in range(len(expected)) if i not in matched_expected_idx]
    unmatched_produced = [i for i in range(len(produced)) if i not in match_of_produced]
    return matched_pairs, unmatched_expected, unmatched_produced


def _label_only_edges(
    expected: list[ExpectedTriple],
    produced: list[ProducedTriple],
    unmatched_expected: list[int],
    unmatched_produced: list[int],
    *,
    require_kind_match: bool,
) -> MatchResult:
    """미매칭 기대 × 미매칭 산출에서 **라벨(그리고 선택적으로 kind)만** 보고 짝짓는 보조 매칭(§A-4).

    `match_sample` 은 kind·predicate·label 셋 다 맞아야 짝짓지만, 혼동 진단은 "라벨은 맞는데 뭐가
    갈렸나"를 보려는 것이라 조건을 완화한다. `require_kind_match=False` 는 kind 오분류 행렬용
    (라벨만 일치), `True` 는 `predicateConfusion` 용(라벨+kind 일치, predicate 만 다름).
    """
    edges: dict[int, list[int]] = {}
    for e_idx in unmatched_expected:
        expected_triple = expected[e_idx]
        accept_norms = {normalize_label(a) for a in expected_triple.accept}
        edges[e_idx] = [
            p_idx
            for p_idx in unmatched_produced
            if normalize_label(produced[p_idx].node_label) in accept_norms
            and (not require_kind_match or produced[p_idx].node_type == expected_triple.kind)
        ]
    match_of_produced: dict[int, int] = {}

    def _try_augment(e_idx: int, visited: set[int]) -> bool:
        for p_idx in edges[e_idx]:
            if p_idx in visited:
                continue
            visited.add(p_idx)
            if p_idx not in match_of_produced or _try_augment(match_of_produced[p_idx], visited):
                match_of_produced[p_idx] = e_idx
                return True
        return False

    for e_idx in unmatched_expected:
        _try_augment(e_idx, set())

    matched_pairs = sorted((e_idx, p_idx) for p_idx, e_idx in match_of_produced.items())
    matched_expected_idx = {pair[0] for pair in matched_pairs}
    remaining_expected = [i for i in unmatched_expected if i not in matched_expected_idx]
    remaining_produced = [i for i in unmatched_produced if i not in match_of_produced]
    return matched_pairs, remaining_expected, remaining_produced


def _sessions_by_id(golden_set: GoldenSet) -> dict[str, Any]:
    return {session.session_id: session for session in golden_set.sessions}


def score_recall(results: list[SessionResult], golden_set: GoldenSet, *, n: int) -> AxisResult:
    """primary confirmatory 축(#328 규약 5) — 미탐율의 반대.

    [A-12] `expectedDenominator` 는 **항상 골든셋 전체**에서 계산한다(`results` 에 있는 세션만
    보지 않는다) — 예산 중단 등 부분 산출 경로에서 아직 시작도 못 한 세션이 분모에서도 사라지면
    부분 결과가 100% 처럼 과대평가된다.
    """
    non_noise_sessions = [s for s in golden_set.sessions if s.slice_id != "noise"]
    expected = n * sum(len(session.expected_triples) for session in non_noise_sessions)
    by_id = {result.session_id: result for result in results}
    numerator = denominator = 0
    for session in non_noise_sessions:
        result = by_id.get(session.session_id)
        if result is None:
            continue
        denominator += len(result.samples) * len(session.expected_triples)
        for sample in result.samples:
            matched, _, _ = match_sample(session.expected_triples, sample.produced)
            numerator += len(matched)
    return AxisResult(
        axis_id="recall",
        title="recall(미탐율의 반대)",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator="매칭된 기대 트리플 수(최대 이분 매칭, §7 매칭 규칙, A-13)",
        definition_denominator="노이즈 제외 세션의 기대 트리플 수 × N",
        exploratory=False,
    )


def score_miss_rate(recall: AxisResult) -> AxisResult:
    """`recall` 과 같은 패스에서 산출된 값을 재사용한다 — 별도로 다시 매칭하면 두 축이 드리프트할
    수 있다(반올림·집계 순서 차)."""
    return AxisResult(
        axis_id="missRate",
        title="미탐율",
        numerator=recall.denominator - recall.numerator,
        denominator=recall.denominator,
        expected_denominator=recall.expected_denominator,
        unfilled_sample_count=recall.unfilled_sample_count,
        definition_numerator="1 - recall 의 분자(매칭 안 된 기대 트리플 수)",
        definition_denominator=recall.definition_denominator,
        exploratory=True,
    )


def score_noise_false_positive_rate(
    results: list[SessionResult], golden_set: GoldenSet, *, n: int
) -> AxisResult:
    """사전 등록 2차 축(#328 규약 5) — noise 슬라이스에서 산출된 트리플 수(표본당 여러 개 가능).

    [A-12] `expectedDenominator` 는 골든셋 전체의 noise 세션 수 기준(부분 산출 과대평가 방지).
    """
    noise_sessions = [s for s in golden_set.sessions if s.slice_id == "noise"]
    expected = n * len(noise_sessions)
    by_id = {result.session_id: result for result in results}
    numerator = denominator = 0
    for session in noise_sessions:
        result = by_id.get(session.session_id)
        if result is None:
            continue
        denominator += len(result.samples)
        numerator += sum(len(sample.produced) for sample in result.samples)
    return AxisResult(
        axis_id="noiseFalsePositiveRate",
        title="오탐 슬라이스 산출 트리플 수",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator="noise 세션 표본에서 산출된 트리플 총수(표본당 0개 이상)",
        definition_denominator="noise 세션 수 × N(표본 수 기준)",
        exploratory=False,
    )


def score_false_positive_rate(results: list[SessionResult], golden_set: GoldenSet) -> AxisResult:
    sessions = _sessions_by_id(golden_set)
    numerator = denominator = 0
    for result in results:
        session = sessions.get(result.session_id)
        expected_triples = session.expected_triples if session is not None else []
        for sample in result.samples:
            _, _, unmatched_produced = match_sample(expected_triples, sample.produced)
            numerator += len(unmatched_produced)
            denominator += len(sample.produced)
    return AxisResult(
        axis_id="falsePositiveRate",
        title="오탐율(전체)",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=denominator,
        unfilled_sample_count=0,
        definition_numerator="어떤 기대 트리플과도 매칭 안 된 산출 트리플 수",
        definition_denominator="산출 트리플 총수(전체 슬라이스)",
        exploratory=True,
    )


def score_node_id_agreement(results: list[SessionResult], golden_set: GoldenSet) -> AxisResult:
    """사전 등록 2차 축(#328 규약 5) — 매칭 트리플 중 nodeId 까지 일치하는 비율."""
    sessions = {sid: s for sid, s in _sessions_by_id(golden_set).items() if s.slice_id != "noise"}
    numerator = denominator = 0
    for result in results:
        session = sessions.get(result.session_id)
        if session is None:
            continue
        for sample in result.samples:
            matched, _, _ = match_sample(session.expected_triples, sample.produced)
            for e_idx, p_idx in matched:
                denominator += 1
                if sample.produced[p_idx].node_id == session.expected_triples[e_idx].node_id:
                    numerator += 1
    return AxisResult(
        axis_id="nodeIdAgreement",
        title="nodeId 일치율(매칭 트리플 중)",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=denominator,
        unfilled_sample_count=0,
        definition_numerator="매칭 트리플 중 produced.node.node_id == expected.node_id",
        definition_denominator="매칭 트리플 수",
        exploratory=False,
    )


def score_session_exact_set(
    results: list[SessionResult], golden_set: GoldenSet, *, n: int
) -> AxisResult:
    """[A-12] `expectedDenominator` 는 골든셋 전체 세션 수 기준."""
    expected = n * len(golden_set.sessions)
    by_id = {result.session_id: result for result in results}
    numerator = denominator = 0
    for session in golden_set.sessions:
        result = by_id.get(session.session_id)
        if result is None:
            continue
        denominator += len(result.samples)
        for sample in result.samples:
            _, unmatched_expected, unmatched_produced = match_sample(
                session.expected_triples, sample.produced
            )
            if not unmatched_expected and not unmatched_produced:
                numerator += 1
    return AxisResult(
        axis_id="sessionExactSet",
        title="세션 집합 정확 일치",
        numerator=numerator,
        denominator=denominator,
        expected_denominator=expected,
        unfilled_sample_count=expected - denominator,
        definition_numerator="산출 트리플 집합이 기대 집합과 정확히 일치(여분 0·누락 0)",
        definition_denominator="세션 수 × N",
        exploratory=True,
    )


def score_recall_by_slice(
    results: list[SessionResult], golden_set: GoldenSet, *, n: int
) -> dict[str, AxisResult]:
    """슬라이스 분해(exploratory) — noise 슬라이스는 기대 트리플이 없어 분모 0(ratio=None).

    [A-12] `expectedDenominator` 는 슬라이스 안 골든셋 전체 세션 기준(부분 산출 과대평가 방지).
    """
    by_id = {result.session_id: result for result in results}
    sessions_by_slice: dict[str, list] = {}
    for session in golden_set.sessions:
        sessions_by_slice.setdefault(session.slice_id, []).append(session)
    out: dict[str, AxisResult] = {}
    for slice_id, slice_sessions in sessions_by_slice.items():
        expected = n * sum(len(session.expected_triples) for session in slice_sessions)
        numerator = denominator = 0
        for session in slice_sessions:
            result = by_id.get(session.session_id)
            if result is None:
                continue
            denominator += len(result.samples) * len(session.expected_triples)
            for sample in result.samples:
                matched, _, _ = match_sample(session.expected_triples, sample.produced)
                numerator += len(matched)
        out[slice_id] = AxisResult(
            axis_id=f"recall.{slice_id}",
            title=f"recall — {slice_id} 슬라이스",
            numerator=numerator,
            denominator=denominator,
            expected_denominator=expected,
            unfilled_sample_count=expected - denominator,
            definition_numerator="매칭된 기대 트리플 수(최대 이분 매칭, A-13)",
            definition_denominator=f"{slice_id} 세션의 기대 트리플 수 × N",
            exploratory=True,
        )
    return out


def score_all(
    results: list[SessionResult], golden_set: GoldenSet, *, n: int
) -> dict[str, AxisResult]:
    recall = score_recall(results, golden_set, n=n)
    axes = (
        recall,
        score_miss_rate(recall),
        score_noise_false_positive_rate(results, golden_set, n=n),
        score_false_positive_rate(results, golden_set),
        score_node_id_agreement(results, golden_set),
        score_session_exact_set(results, golden_set, n=n),
    )
    return {axis.axis_id: axis for axis in axes}


def diagnostics(results: list[SessionResult]) -> dict[str, Any]:
    """합불이 아닌 진단 카운터 — 미탐이 프롬프트/게이트/resolver 중 어디서 났는지 가른다(§6)."""
    emitted_deltas = sum(sample.emitted_deltas for result in results for sample in result.samples)
    promoted_count = sum(sample.promoted_count for result in results for sample in result.samples)
    gate_rejected = sum(sample.gate_rejected for result in results for sample in result.samples)
    resolver_dropped_entries = [
        entry
        for result in results
        for sample in result.samples
        for entry in sample.resolver_dropped
    ]
    resolver_dropped_by_kind = Counter(entry["kind"] for entry in resolver_dropped_entries)
    legacy_schema_no_kind = sum(
        sample.legacy_schema_no_kind for result in results for sample in result.samples
    )
    fact_dedup_collapsed = sum(
        sample.fact_dedup_collapsed for result in results for sample in result.samples
    )
    band_label_rejected = [
        pair
        for result in results
        for sample in result.samples
        for pair in sample.band_label_rejected
    ]
    verified_false_count = sum(
        1
        for result in results
        for sample in result.samples
        for triple in sample.produced
        if not triple.verified
    )
    schema_violation_count = sum(
        1
        for result in results
        for failure in result.failures
        if failure.error_type == "schemaViolation"
    )
    transport_error_types = Counter(
        failure.error_type
        for result in results
        for failure in result.failures
        if failure.error_type.startswith("transportError")
    )
    return {
        "emittedDeltas": emitted_deltas,
        "promotedCount": promoted_count,
        "gateRejected": gate_rejected,
        # [A-3] kind→건수. fact 문자열 역매핑 근사 — factDedupCollapsed>0 인 표본에서는 하한이다.
        "resolverDroppedByKind": dict(resolver_dropped_by_kind),
        "resolverDroppedCount": len(resolver_dropped_entries),
        # [A-11] 구스키마(kind 없음) 조기 반환 — `_resolve_delta` 가 "정상 경로"로 문서화한 분기라
        # resolverDropped 에 안 섞는다.
        "legacySchemaNoKind": legacy_schema_no_kind,
        "unprojectedFacts": len(resolver_dropped_entries) + legacy_schema_no_kind,
        # [A-10] promotedCount 와 실제 fact 레코드 수 차 — 0 이 아니면 같은 fact 문자열을 낸
        # 델타가 dedup 으로 합쳐져 위 kind 귀속이 근사였다는 뜻.
        "factDedupCollapsed": fact_dedup_collapsed,
        "bandLabelRejected": band_label_rejected,
        "verifiedFalseCount": verified_false_count,
        "schemaViolation": schema_violation_count,
        "transportError": dict(transport_error_types),
        "definition": {
            "emittedDeltas": "LLM 이 낸 델타 수(채록한 원문을 extract_json 으로 관측, 판정 아님)",
            "promotedCount": "generate_session_delta 의 반환값(승격 fact 목록) 길이 합 — 프로덕션 값",
            "gateRejected": "채록한 델타 중 should_promote(프로덕션 게이트)가 거절한 수",
            "resolverDroppedByKind": "kind→건수. FactRecord.fact 문자열로 델타를 역매핑해 얻는다"
            "(추정 아님) — 같은 fact 문자열을 내는 델타가 여럿이면 첫 델타로 귀속하는 근사가 섞인다",
            "resolverDroppedCount": "resolverDroppedByKind 값의 총합",
            "legacySchemaNoKind": "kind 없는 구스키마 조기 반환 수(정상 경로, resolver 실패 아님)",
            "unprojectedFacts": "resolverDroppedCount + legacySchemaNoKind"
            "(그래프 트리플이 안 붙은 fact 총수)",
            "factDedupCollapsed": "promotedCount - fact 레코드 수(0 초과면 위 kind 귀속이 근사였다는 뜻)",
            "bandLabelRejected": "밴드 kind 인데 _resolve_band 가 드롭한 {kind,label} 목록",
            "verifiedFalseCount": "산출 트리플 중 node.verified=false 인 수 "
            "(brand/attribute/situation 무어휘·product 는 항상 false, #357 비범위)",
            "schemaViolation": "extract_json 파싱 실패로 표본이 못 된 시도 수",
            "transportError": "llm.complete() 예외로 표본이 못 된 시도 수(타입별)",
        },
    }


def build_confusion(results: list[SessionResult], golden_set: GoldenSet) -> list[dict[str, Any]]:
    """[A-4] 미매칭 기대 × 미매칭 산출에서 **라벨이 실제로 일치하는 쌍**을 먼저 짝지어
    (expectedKind → producedKind) 로 센다 — 이게 진짜 kind 오분류다. 남은 미매칭 기대는
    (expectedKind → ∅), 남은 미매칭 산출은 (∅ → producedKind).

    종전(위치 짝짓기, `zip_longest`)은 "라벨은 맞는데 kind 만 틀린" 진짜 오분류와 "아무 관계
    없는 오탐"을 같은 칸에 넣었다 — 여기서는 근거가 라벨 일치라서 그 구분이 선다(인과까지
    주장하지는 않는다: 라벨이 우연히 같은 두 무관한 취향이 섞일 수 있다).
    """
    sessions = _sessions_by_id(golden_set)
    counter: Counter[tuple[str, str]] = Counter()
    for result in results:
        session = sessions.get(result.session_id)
        if session is None:
            continue
        for sample in result.samples:
            _, unmatched_expected, unmatched_produced = match_sample(
                session.expected_triples, sample.produced
            )
            label_pairs, remaining_expected, remaining_produced = _label_only_edges(
                session.expected_triples,
                sample.produced,
                unmatched_expected,
                unmatched_produced,
                require_kind_match=False,
            )
            for e_idx, p_idx in label_pairs:
                counter[
                    (session.expected_triples[e_idx].kind, sample.produced[p_idx].node_type)
                ] += 1
            for e_idx in remaining_expected:
                counter[(session.expected_triples[e_idx].kind, "∅")] += 1
            for p_idx in remaining_produced:
                counter[("∅", sample.produced[p_idx].node_type)] += 1
    return [
        {"expectedKind": expected, "producedKind": produced, "count": count}
        for (expected, produced), count in sorted(counter.items(), key=lambda kv: -kv[1])
    ]


def build_predicate_confusion(
    results: list[SessionResult], golden_set: GoldenSet
) -> list[dict[str, Any]]:
    """[A-4 추가] kind·label 은 맞는데 predicate 만 다른 쌍의 (expectedPredicate →
    producedPredicate) 빈도 — `polarity`·`conflict` 슬라이스가 실제로 재려는 실패 유형을
    직접 드러낸다("소니는 별로예요"가 `likes 소니` 로 저장되는 극성 반전,
    `resolver._decide_predicate` 주석이 기록한 실측 결함 유형).
    """
    sessions = _sessions_by_id(golden_set)
    counter: Counter[tuple[str, str]] = Counter()
    for result in results:
        session = sessions.get(result.session_id)
        if session is None:
            continue
        for sample in result.samples:
            _, unmatched_expected, unmatched_produced = match_sample(
                session.expected_triples, sample.produced
            )
            pairs, _, _ = _label_only_edges(
                session.expected_triples,
                sample.produced,
                unmatched_expected,
                unmatched_produced,
                require_kind_match=True,
            )
            for e_idx, p_idx in pairs:
                expected_triple = session.expected_triples[e_idx]
                produced_triple = sample.produced[p_idx]
                counter[(expected_triple.predicate, produced_triple.predicate)] += 1
    return [
        {"expectedPredicate": expected, "producedPredicate": produced, "count": count}
        for (expected, produced), count in sorted(counter.items(), key=lambda kv: -kv[1])
    ]
