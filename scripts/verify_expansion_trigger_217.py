"""#217 전개 트리거(매핑 실패) 실측 재현 — DESIGN-NEEDS-EXPANSION-198 §4.5.

marker 열거를 폐기하고 "매핑 실패(거리 초과 + 마진 얇음)"를 트리거로 쓰는 결정의 근거를 재현한다.
설계 문서의 표가 이 스크립트 출력에서 나왔다.

전제
    docker compose up -d pg-catalog          # categories 2056행 + 임베딩 시드
    uv run python scripts/verify_expansion_trigger_217.py [섹션...]

섹션(생략하면 전부): control · recover · expand · band
    control  §4.5 ① 대조군 오탐 0 — 잘 되던 것이 안 깨진다
    recover  §4.5 ② 회수 — marker 가 놓치던 것이 잡힌다
    expand   §4.5 ③ 전개가 파괴하는 부류 — 합집합 배선의 근거
    band     §4.5 ④ 전개 전용 거리 임계 기각 + ⑤ marker 삭제 순이득

임베딩 API 를 호출하므로 실행마다 비용이 든다(앵커 100여 건). 판정은 config 의 실제 임계를 읽어
`category_mapping` 과 같은 식으로 계산한다 — 임계가 바뀌면 이 스크립트 결과도 함께 움직인다.
"""

from __future__ import annotations

import functools
import sys

from app.core.config import get_settings
from app.pipelines.category_search import search_categories_pg
from app.pipelines.embedding import embed_texts

# ── 앵커 ──────────────────────────────────────────────────────────────────────
# (앵커, 현행 marker 로 잡히는가) — marker 는 폐기 대상이지만 "삭제로 잃는 것"을 세려면 필요하다.
_MARKERS = ("선물", "답례품", "준비물", "용품", "아이템", "키트", "물품", "추천", "것", "거")


def _by_marker(text: str) -> bool:
    """초판 D3 판정 재현 — leg query 가 목적 marker 로 endswith 하는가."""
    return text.endswith(_MARKERS)


# §4.5 ① 대조군: 전개되면 안 되는 표현(이슈 #217 완료 조건 4건 + 정상 상품명)
CONTROL = [
    "한방재료",
    "떡볶이 재료",
    "수예 재료",
    "베이킹 재료",
    "한우 선물세트",
    "청소도구 세트",
    "청바지",
    "무선 이어폰",
]

# §4.5 ② 회수 대상: marker 목록에 없어 초판이 놓치던 목적 표현
RECOVER = [
    "김밥 재료",
    "감자탕 재료",
    "집들이 선물 아이디어",
    "자취 필수템",
]

# §4.5 ③ 목적 표현 → 사람이 전개한 구체 상품(LLM 전개 기대값 대역).
# "매핑 성공 = 정답"이 아님을 보이려면 전개 결과와 나란히 놓아야 한다.
EXPANSION = {
    "떡볶이 재료": ["떡볶이 떡", "어묵", "고추장", "대파"],
    "베이킹 재료": ["밀가루", "버터", "설탕", "베이킹파우더"],
    "수예 재료": ["원단", "재봉실", "지퍼", "바늘"],
    "한방재료": ["인삼", "대추", "감초", "구기자"],
    "김밥 재료": ["김", "단무지", "햄", "시금치"],
    "감자탕 재료": ["돼지등뼈", "감자", "대파", "우거지"],
    "생활용품": ["휴지", "세제", "수세미", "빨래건조대"],
    "캠핑 용품": ["텐트", "캠핑랜턴", "캠핑매트", "코펠"],
    "돌잔치 답례품": ["타월 세트", "향초", "다기세트", "손수건"],
}

# §4.5 ④⑤ 임계 스윕용 — 정상 상품명(전개되면 오탐) vs 목적 표현(전개돼야 회수)
NORMAL = [
    "청바지", "무선 이어폰", "운동화", "텐트", "노트북", "에어프라이어",
    "립스틱", "강아지 사료", "기저귀", "원피스", "백팩", "커피머신",
    "전기밥솥", "매트리스", "선풍기", "가습기", "블루투스 스피커", "요가매트",
    "등산화", "로봇청소기", "식기세척기", "샴푸", "선크림", "향수",
    "니트 가디건", "면도기", "전동칫솔", "무선 청소기", "게이밍 마우스", "모니터",
    "아기 분유", "고양이 모래", "캠핑 의자", "골프 장갑", "수영복", "패딩 점퍼",
    "한우 선물세트", "청소도구 세트", "떡볶이 재료", "한방재료", "수예 재료", "베이킹 재료",
]  # fmt: skip

PURPOSE = [
    "집들이 선물", "돌잔치 답례품", "캠핑 용품", "환갑 선물 아이템", "자취 시작 키트",
    "생활용품", "선물용품", "캠핑 준비물", "유럽여행 준비물", "결혼 답례품",
    "신혼 살림 물품", "겨울 캠핑 아이템", "집들이 선물 추천", "김밥 재료", "감자탕 재료",
    "집들이 선물 아이디어", "발 보습 제품", "캠핑 전자기기 필수품", "자취 필수템", "신학기 준비",
]  # fmt: skip

# §4.5 ⑤ 전용 추가 앵커 — marker **전수** 감사에는 필요하지만 ④ 임계 스윕에는 넣지 않는다.
# 두 측정의 표본이 다른 이유:
#   ④ 는 "임계를 낮추면 정상 상품명이 얼마나 새는가"라 normal/purpose 균형이 결과를 좌우한다.
#     실사용 분포를 흉내낸 고정 표본이어야 임계 간 비교가 성립한다.
#   ⑤ 는 "marker 가 잡는 것 전부를 매핑 결과로 나눈다"라 marker 커버리지가 빠지면 셈이 틀린다 —
#     `것`·`거` marker 와 `용품` 다중 사례가 있어야 §4.5 ⑤ 의 "용품은 역효과 marker" 논거가 선다.
# `평점 높은 거` 는 case 2 라 실제로는 게이트에 막히지만(§4.2), marker 판정 자체는 걸리므로
# marker 감사 표본에는 포함한다 — 게이트가 없으면 무엇이 새는지가 그 논거의 절반이다.
MARKER_AUDIT_EXTRA = ["필요한 것", "평점 높은 거", "주방 용품", "반려견 용품", "캠핑 초보자 물품"]

THRESHOLD_SWEEP = (0.18, 0.19, 0.20, 0.21, 0.22)


class _Probe:
    """앵커 → (top1, 거리, 마진, 판정). 판정은 `category_mapping` 의 §4·§4.5 규칙 재현."""

    def __init__(self) -> None:
        self.s = get_settings()
        self._embed = functools.partial(embed_texts, task_type=self.s.embedding_task_query)
        self._vecs: dict[str, list[float]] = {}

    def warm(self, texts: list[str]) -> None:
        """미조회 앵커만 한 번에 임베딩한다 — 호출 수를 앵커 수가 아니라 배치 수로 묶는다."""
        todo = [t for t in dict.fromkeys(texts) if t not in self._vecs]
        if todo:
            self._vecs.update(zip(todo, self._embed(todo), strict=True))

    def __call__(self, text: str) -> tuple[str, float, float | None, bool]:
        self.warm([text])
        hits = search_categories_pg(
            self._vecs[text], self.s.catalog_db_url, k=self.s.category_top_k
        )
        if not hits:
            return ("(히트 0)", 9.9, None, False)  # 시드 결측 — §4 ④ 는 트리거 대상이 아니다
        top1, dist = hits[0]
        margin = round(hits[1][1] - dist, 4) if len(hits) > 1 else None
        dist = round(dist, 4)
        return (top1, dist, margin, self.failed(dist, margin))

    def failed(self, dist: float, margin: float | None, *, threshold: float | None = None) -> bool:
        """매핑 실패 판정 — 거리 초과 **그리고** §4.5 마진 예외 미해당(둘 다여야 한다).

        `threshold` 로 거리 임계를 바꿔 스윕한다(§4.5 ④). 마진 조건은 고정 — 임계를 낮춰도
        `베이킹 재료`(마진 0.0397)가 안 잡히는 이유가 여기 있다.
        """
        limit = self.s.category_distance_max if threshold is None else threshold
        return dist > limit and (
            margin is None or margin < self.s.category_distance_override_margin
        )


def _row(label: str, probe: _Probe, text: str, *, note: str = "") -> bool:
    top1, dist, margin, failed = probe(text)
    ms = f"{margin:.4f}" if margin is not None else "None"
    verdict = "매핑실패 → 전개" if failed else "매핑성공 → 미전개"
    print(f"  {label:<22} {top1:<34} {dist:.4f} / {ms}  {verdict}{note}")
    return failed


def section_control(probe: _Probe) -> None:
    print("\n=== §4.5 ① 대조군 — 전개되면 안 되는 표현 (오탐 0 이어야 한다) ===")
    probe.warm(CONTROL)
    wrong = [t for t in CONTROL if _row(t, probe, t)]
    print(f"  → 오탐 {len(wrong)}건 {wrong or ''}")


def section_recover(probe: _Probe) -> None:
    print("\n=== §4.5 ② 회수 — marker 가 놓치던 목적 표현 (전부 전개돼야 한다) ===")
    probe.warm(RECOVER)
    missed = [t for t in RECOVER if not _row(t, probe, t, note=f"  [marker={_by_marker(t)}]")]
    print(f"  → 미회수 {len(missed)}건 {missed or ''}")


def section_expand(probe: _Probe) -> None:
    print("\n=== §4.5 ③ 전개 결과 — '매핑 성공 = 정답'이 아니다 (합집합 배선의 근거) ===")
    probe.warm(list(EXPANSION) + [p for v in EXPANSION.values() for p in v])
    for purpose, items in EXPANSION.items():
        print(f"\n  ■ {purpose}")
        _row("[미전개]", probe, purpose)
        for it in items:
            _row(f"  └ {it}", probe, it)


def section_band(probe: _Probe) -> None:
    print("\n=== §4.5 ④ 거리 임계 스윕 — 낮춰도 `베이킹 재료`(마진 0.0397)는 안 잡힌다 ===")
    probe.warm(NORMAL + PURPOSE)
    scored = [(t, "normal", *probe(t)[1:3]) for t in NORMAL]
    scored += [(t, "purpose", *probe(t)[1:3]) for t in PURPOSE]

    print(f"  {'임계':>6} {'오탐':>5} {'회수':>5}  오탐 목록")
    for th in THRESHOLD_SWEEP:
        fp = [t for t, k, d, m in scored if k == "normal" and probe.failed(d, m, threshold=th)]
        tp = [t for t, k, d, m in scored if k == "purpose" and probe.failed(d, m, threshold=th)]
        print(f"  {th:>6} {len(fp):>5} {len(tp):>5}  {fp}")

    print("\n=== §4.5 ⑤ marker 삭제 순이득 — 걸리는 표현을 매핑 결과로 나눈다 ===")
    audit = [t for t in PURPOSE + MARKER_AUDIT_EXTRA if _by_marker(t)]
    probe.warm(audit)
    marked = [(t, *probe(t)[1:3]) for t in audit]
    also_failed = [t for t, d, m in marked if probe.failed(d, m)]
    only_marker = [t for t, d, m in marked if not probe.failed(d, m)]
    print(f"  marker 에 걸리는 목적 표현 {len(marked)}건")
    print(f"    ├─ 매핑도 실패 {len(also_failed)}건 → marker 없어도 잡힘(순이득 0): {also_failed}")
    print(
        f"    └─ 매핑 성공 {len(only_marker)}건 → marker 만 잡음(= 삭제로 잃는 것): {only_marker}"
    )
    print("    ※ 뒤쪽이 '이미 정확히 매핑되는 것을 더 쪼개는' 이득이라 삭제를 택했다(§4.5 ⑤).")


SECTIONS = {
    "control": section_control,
    "recover": section_recover,
    "expand": section_expand,
    "band": section_band,
}


def main(argv: list[str]) -> None:
    probe = _Probe()
    s = probe.s
    print(
        f"distance_max={s.category_distance_max}  "
        f"override_margin={s.category_distance_override_margin}  "
        f"select_margin_max={s.category_select_margin_max}  top_k={s.category_top_k}"
    )
    wanted = argv or list(SECTIONS)
    unknown = [a for a in wanted if a not in SECTIONS]
    if unknown:
        raise SystemExit(f"알 수 없는 섹션: {unknown} (가능: {list(SECTIONS)})")
    for name in wanted:
        SECTIONS[name](probe)


if __name__ == "__main__":
    main(sys.argv[1:])
