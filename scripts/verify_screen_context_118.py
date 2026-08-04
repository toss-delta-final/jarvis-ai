"""#118 라운드 2 실 LLM 프로브 — screen 맥락 주입 · last_reco 누적화의 intent 분포 측정.

프롬프트 계층(`decompose._SYSTEM` + user 메시지)은 `gpt-5-nano` 가 해석하는 **확률적 계층**이라
단위 테스트(FakeLLM)로는 동작이 증명되지 않는다. #234/#239/#240 에서 "한 줄을 고치면 다른 경로가
깎이는" 것이 반복 실측됐으므로, 이 라운드의 채택 근거는 **수정 전/후 분포 비교**다.
(docs/lessons.md 2026-08-02 「프롬프트 계층 intent 안정성은 …」)

측정하는 것
  1. 회귀 대조군 — #240 이 "낮추지 말 것"으로 못박은 경로들. screen 은 실리지 않고, 달라지는 것은
     ① LAST_RECOMMENDATIONS 가 누적으로 길어진 것 ② (안 2 한정) `_SYSTEM` 에 한 줄 추가된 것이다.
  2. 신규 기능 셀 — screen 이 실린 요청에서 지시어("이거"·"3번째 거"·"3번째 줄 2번째"·이름)가
     실제로 풀리는지.

변형을 **같은 스크립트로** 잰다.
  before                  — 오늘의 코드. screen 미주입 + 누적 전 LAST_RECOMMENDATIONS.
  after-merge             — 안 1. 화면 상품을 LAST_RECOMMENDATIONS 에 합류(_SYSTEM 무변경) + 누적.
  after-block             — 안 2. 별도 SCREEN 블록 + cart_add 규칙 한 줄 추가 + 누적.
  after-block-noaccum     — 안 2 에서 **승계분만** 뺀 대조군. 회귀 원인이 (A)인지 (B)인지 가른다.
  after-block-promptcapN  — 승계분을 최근 N 건으로 자른 수정 후보.
  adopted                 — **채택한 최종 배선**. 안 2 + (되물음 턴에만 승계분 제외) +
                            graph.py 의 코드 해소기(screen_reference)까지 태운다.

전제
    uv run python scripts/verify_screen_context_118.py [--n 8] [--variants adopted]

⚠️ **실제 LLM 을 호출한다.** 셀 × N 이라 전 변형을 돌리면 1,200회를 넘는다 — 실행 전에 예상 호출
수를 출력하고 `--max-calls` 상한을 넘기면 시작하지 않는다. DB·Spring·JWT 는 필요 없다.

프로덕션 함수를 **그대로 호출**한다(scripts/verify_regression6_217.py 규약) — 흐름을 흉내내면
그 흉내가 틀렸을 때 검증이 무의미해진다. `adopted` 는 `decompose` 다음에 `resolve_screen_reference`
를 거는데, 이는 graph.py 의 cart_add 분기와 **같은 두 프로덕션 호출을 같은 순서로** 태우는 것이다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.agents.buyer.recommendation import decompose as decompose_mod
from app.agents.buyer.recommendation.decompose import ScreenPrompt, decompose
from app.agents.buyer.recommendation.state import CartIntent, RouteDecision
from app.agents.buyer.screen_reference import resolve_screen_reference
from app.core.config import get_settings
from app.core.llm import get_llm
from app.schemas.spring import ProductSearchFilters

# ── 세션 맥락 (PR #239 프로브 설정을 따른다 — 기준선과 비교 가능해야 한다) ──────────────────
# 직전 추천: 세탁 세제 3종. PRIOR_FILTERS.semanticQuery = "세탁 세제".
RECO_BASE: list[tuple[int, str]] = [
    (2001, "리필 세탁 세제 2L"),
    (2002, "드럼용 세탁 세제"),
    (2003, "섬유유연제"),
]
# 상품 전환 셀("이어폰으로 할래")이 지목할 대상 — 전환은 목록 안의 다른 상품으로 간다.
SWITCH_RECO: list[tuple[int, str]] = RECO_BASE + [(2004, "무선 블루투스 이어폰")]
# 누적화(A)로 **승계**되는 직전 턴들의 상품. 이 목록이 붙는 것 자체가 프롬프트 변경이라
# 회귀 대조군을 이 길이로도 다시 잰다. 이어폰류는 넣지 않는다(이름 매칭 셀과 겹치지 않게).
CARRY: list[tuple[int, str]] = [
    (2101, "실리콘 주방장갑"),
    (2102, "스테인리스 냄비 20cm"),
    (2103, "극세사 청소포"),
    (2104, "욕실 미끄럼방지 매트"),
    (2105, "압축 수납팩 10p"),
    (2106, "문틈 방음 테이프"),
    (2107, "빨래건조대 3단"),
]
PENDING_CART = {
    "productId": 2002,
    "options": [{"optionId": 1001, "name": "일반형"}, {"optionId": 1002, "name": "드럼형"}],
}
PRIOR = ProductSearchFilters(semantic_query="세탁 세제")

# PROFILE_SUMMARY 는 **비운다**. config 기본값 `profile_injection_scope="rerank_only"` 에서
# 프로덕션 graph 가 decompose 에 `profile_summary=None` 을 넘기기 때문이다 — 채우면 오히려
# 배포 경로에 없는 맥락을 재는 것이 된다(lessons "실제 세션이 싣는 맥락을 모두 채운다"의 취지).
PROFILE = None

# 화면(screen) — 정본 §3.1 "구매자 대화 시작 전 P-4 인기상품 패널"(pageType=chat).
SCREEN_LABEL = "인기 상품"
SCREEN_FILTERS = {"page": "1"}


def _screen(products: Sequence[tuple[int, str]], columns: int | None) -> ScreenPrompt:
    return ScreenPrompt(
        label=SCREEN_LABEL, filters=dict(SCREEN_FILTERS), products=list(products), columns=columns
    )


SCREEN_1 = [(3101, "코튼 워셔블 러그 150x200")]
SCREEN_3 = [
    (3101, "코튼 워셔블 러그 150x200"),
    (3102, "라탄 수납 바구니 3종"),
    (3103, "무드등 겸용 가습기"),
]
SCREEN_5 = SCREEN_3 + [(3104, "우드 사이드테이블"), (3105, "린넨 커튼 2p")]
SCREEN_9 = SCREEN_5 + [
    (3106, "저상형 프레임 침대"),
    (3107, "메모리폼 방석"),
    (3108, "무선 블루투스 이어폰"),  # index 7 = 3번째 줄 2번째 (columns=3)
    (3109, "탁상 선풍기"),
]
SCREEN_NAME = SCREEN_3 + [(3110, "무선 블루투스 이어폰")]


# ── 셀 정의 ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Cell:
    """발화 × 컨텍스트 1조합. `check` 가 RouteDecision 을 보고 목표 충족을 판정한다."""

    group: str
    utterance: str
    context: str
    check: Callable[[RouteDecision], bool]
    label: str  # 목표 설명(표에 그대로 실린다)
    reco: list[tuple[int, str]] = field(default_factory=list)
    prior: ProductSearchFilters | None = None
    pending: dict | None = None
    # screen 셀만 채운다. (products, columns) — before 변형에서는 무시된다(오늘은 미주입).
    screen: tuple[Sequence[tuple[int, str]], int | None] | None = None
    baseline: str = ""  # #240 이 못박은 기준선 표기(있으면 표에 함께 싣는다)


def _intent(name: str) -> Callable[[RouteDecision], bool]:
    return lambda d: d.intent == name


def _option(option_id: int) -> Callable[[RouteDecision], bool]:
    return lambda d: d.intent == "cart_add" and d.cart is not None and d.cart.option_id == option_id


def _product(product_id: int) -> Callable[[RouteDecision], bool]:
    return lambda d: (
        d.intent == "cart_add" and d.cart is not None and d.cart.product_id == product_id
    )


def _switched_product(d: RouteDecision) -> bool:
    """되물음 중 **다른 상품으로 전환** — pending 상품이 아닌 목록 내 상품을 고르면 성공."""
    if d.intent != "cart_add" or d.cart is None or d.cart.product_id is None:
        return False
    return d.cart.product_id != PENDING_CART["productId"]


def _no_product(d: RouteDecision) -> bool:
    """후보가 여러 건이면 임의 확정 금지 — productId 를 비워야 되물음으로 흐른다(정본 §3.1)."""
    return d.cart is None or d.cart.product_id is None


def _not_hallucinated(d: RouteDecision) -> bool:
    """두 목록 어디에도 없는 발화 속 숫자(301)를 담기 대상으로 확정하지 않는다."""
    return d.cart is None or d.cart.product_id != 301


# 컨텍스트 3종 — PR #239 프로브와 같은 구성.
_CTX_NONE = dict(reco=[], prior=None, pending=None)
_CTX_RECO = dict(reco=RECO_BASE, prior=PRIOR, pending=None)
_CTX_PENDING = dict(reco=RECO_BASE, prior=PRIOR, pending=PENDING_CART)
_CONTEXTS = {"맥락없음": _CTX_NONE, "직전추천": _CTX_RECO, "pending": _CTX_PENDING}


def _build_cells() -> list[Cell]:
    cells: list[Cell] = []

    # 1) 옵션 답변 4종 (pending 컨텍스트) — 기준선 32/32. intent + optionId 를 함께 센다.
    for utterance, option_id in (
        ("드럼형으로", 1002),
        ("일반형", 1001),
        ("드럼형으로 담아줘", 1002),
        ("2번으로", 1002),
    ):
        cells.append(
            Cell(
                group="옵션 답변",
                utterance=utterance,
                context="pending",
                check=_option(option_id),
                label=f"cart_add + optionId={option_id}",
                baseline="8/8",
                **_CTX_PENDING,  # type: ignore[arg-type]
            )
        )

    # 2) 장바구니 대조군 6종 × 3컨텍스트 — 기준선 144/144.
    for utterance, intent in (
        ("장바구니 보여줘", "cart_view"),
        ("장바구니에 뭐 있어?", "cart_view"),
        ("내 장바구니 확인해줘", "cart_view"),
        ("그거 담아줘", "cart_add"),
        ("장바구니에 넣어줘", "cart_add"),
        ("2번 담아줘", "cart_add"),
    ):
        for ctx_name, ctx in _CONTEXTS.items():
            cells.append(
                Cell(
                    group="장바구니 대조군",
                    utterance=utterance,
                    context=ctx_name,
                    check=_intent(intent),
                    label=intent,
                    baseline="8/8",
                    **ctx,  # type: ignore[arg-type]
                )
            )

    # 3) order_status 2종 × 3컨텍스트 — 기준선 48/48.
    for utterance in ("내 주문 어디까지 왔어?", "배송 상태 알려줘"):
        for ctx_name, ctx in _CONTEXTS.items():
            cells.append(
                Cell(
                    group="order_status",
                    utterance=utterance,
                    context=ctx_name,
                    check=_intent("order_status"),
                    label="order_status",
                    baseline="8/8",
                    **ctx,  # type: ignore[arg-type]
                )
            )

    # 4) general 대조군 — #239 가 함께 잰 축(7/8·8/8·8/8).
    for ctx_name, ctx in _CONTEXTS.items():
        cells.append(
            Cell(
                group="general 대조군",
                utterance="주문 취소 방법",
                context=ctx_name,
                check=_intent("general"),
                label="general",
                baseline="7~8/8",
                **ctx,  # type: ignore[arg-type]
            )
        )

    # 5) 지시대명사 4종 × 3컨텍스트 — #239 표. pending 컬럼에 기존 미달이 있다(숨기지 않는다).
    for utterance in (
        "저번에 그거 다시 보여줘",
        "저번에 그거 다시 사고 싶어",
        "그거 또 사고 싶어",
        "그거 보여줘",
    ):
        for ctx_name, ctx in _CONTEXTS.items():
            cells.append(
                Cell(
                    group="지시대명사",
                    utterance=utterance,
                    context=ctx_name,
                    check=_intent("recommend"),
                    label="recommend",
                    baseline="8/8 (pending 컬럼은 6~8/8 기존 미달)",
                    **ctx,  # type: ignore[arg-type]
                )
            )

    # 6) PENDING_CART 중 상품 전환 2종 — 기준선 8/8 · 7/8.
    for utterance, baseline in (("이어폰으로 할래", "8/8"), ("다른 거 담아줘", "7/8")):
        cells.append(
            Cell(
                group="상품 전환",
                utterance=utterance,
                context="pending",
                check=_switched_product,
                label="cart_add + pending 아닌 productId",
                reco=SWITCH_RECO,
                prior=PRIOR,
                pending=PENDING_CART,
                baseline=baseline,
            )
        )

    # 7) 참고 셀 — #240 이 "남은 미달"로 기록한 경로. 기준선이 0/8 이라 게이트가 아니다.
    cells.append(
        Cell(
            group="참고(기존 미달)",
            utterance="안녕",
            context="pending",
            check=_intent("general"),
            label="general",
            baseline="0/8 (#239 기록)",
            **_CTX_PENDING,  # type: ignore[arg-type]
        )
    )

    # 8) 신규 기능 셀 — screen 이 실린 요청. before 변형에서는 screen 이 주입되지 않는다.
    screen_cells: list[tuple[str, Sequence[tuple[int, str]], int | None, Callable, str]] = [
        ("이거 담아줘", SCREEN_1, 1, _product(3101), "productId=3101 확정"),
        ("이거 담아줘", SCREEN_3, 3, _no_product, "되물음(productId 비움)"),
        ("3번째 거 담아줘", SCREEN_5, 3, _product(3103), "productId=3103 (3번째)"),
        ("3번째 줄 2번째 담아줘", SCREEN_9, 3, _product(3108), "productId=3108 (index 7)"),
        ("무선 이어폰 담아줘", SCREEN_NAME, 2, _product(3110), "productId=3110 (이름 매칭)"),
        ("301 담아줘", SCREEN_3, 3, _not_hallucinated, "301 확정 금지"),
    ]
    for utterance, products, columns, check, label in screen_cells:
        cells.append(
            Cell(
                group="신규(screen)",
                utterance=utterance,
                context=f"screen {len(products)}건" + (f"·{columns}열" if columns else ""),
                check=check,
                label=label,
                reco=RECO_BASE,
                prior=PRIOR,
                screen=(products, columns),
            )
        )
    return cells


# ── 실행 ───────────────────────────────────────────────────────────────────────────────
@dataclass
class CellResult:
    cell: Cell
    hits: int = 0
    runs: int = 0
    errors: int = 0
    observed: dict[str, int] = field(default_factory=dict)

    @property
    def score(self) -> str:
        return f"{self.hits}/{self.runs}" if self.runs else "-"


def _observe(decision: RouteDecision) -> str:
    """표에 남길 관측 요약 — intent 와, 담기면 productId/optionId 까지."""
    if decision.intent == "cart_add" and decision.cart is not None:
        return f"cart_add(p={decision.cart.product_id},o={decision.cart.option_id})"
    return decision.intent


async def _run_cell(  # noqa: ANN001
    cell: Cell, *, variant: str, n: int, sem: asyncio.Semaphore, llm, retries: int
) -> CellResult:
    result = CellResult(cell=cell)
    # 누적화(A)의 효과 — after 변형은 승계분이 붙은 긴 LAST_RECOMMENDATIONS 로 잰다.
    # `-noaccum` 접미사는 **(A)와 (B)를 가르는 대조군**이다: screen 주입·_SYSTEM 변경은 그대로
    # 두고 승계분만 뺀다. 여기서 수치가 before 로 돌아오면 회귀 원인은 목록 길이(A)지 화면 설계(B)가
    # 아니다 — 1차 캠페인에서 두 after 변형이 상품 전환 셀을 **똑같이** 깎은 것이 이 가설의 근거다.
    #
    # `-promptcapN` 은 그 회귀의 **수정 후보**를 잰다: 담기 가드(allowed)는 누적 전체를 쓰되
    # 프롬프트 LAST_RECOMMENDATIONS 만 최근 N 건으로 자른다. 정본 §3.1 [보안]이 누적을 요구하는
    # 대상은 **가드**이지 프롬프트가 아니므로 계약을 어기지 않는다.
    #
    # `adopted` 는 **채택한 최종 배선**이다(위 두 대조군의 결론). graph.py 의 규칙 그대로:
    # 옵션 되물음(PENDING_CART) 턴에는 승계분을 프롬프트에서 빼고, 아닌 턴에는 누적을 싣는다.
    # 담기 가드(allowed)는 어느 쪽이든 누적 전체를 쓴다(여기서는 아래 `allowed` 로 재현).
    carry = variant != "before" and not variant.endswith("-noaccum")
    if variant == "adopted":
        carry = cell.pending is None
    reco = list(cell.reco)
    if carry and reco:
        reco = reco + [item for item in CARRY if item not in reco]
        if "-promptcap" in variant:
            reco = reco[: int(variant.rsplit("-promptcap", 1)[1])]
    screen = None
    if variant != "before" and cell.screen is not None:
        screen = _screen(*cell.screen)

    async def _once(slot: int) -> None:
        # provider TPM(200k/min · 프롬프트 약 3k tok)이 실질 상한이라 429 가 뜬다. 재시도는
        # **측정 대상이 아니라 측정 도구**의 문제라 회차를 버리지 않고 백오프 후 다시 친다 —
        # 실패로 남기면 분포의 분모가 셀마다 달라져 표를 비교할 수 없다.
        decision = None
        for attempt in range(retries + 1):
            async with sem:
                try:
                    decision = await decompose(
                        llm,
                        query=cell.utterance,
                        prior_filters=cell.prior,
                        profile_summary=PROFILE,
                        tier="fast",
                        last_recommendations=reco,
                        pending_cart=cell.pending,
                        screen=screen,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - 한 회차 실패가 캠페인을 죽이지 않게
                    result.errors += 1
                    key = f"ERROR:{type(exc).__name__}"
                    result.observed[key] = result.observed.get(key, 0) + 1
                    if attempt == retries:
                        return
            # 슬롯별로 어긋난 지연을 줘서 재시도가 한꺼번에 몰리지 않게 한다.
            await asyncio.sleep(min(2**attempt, 30) + slot * 0.7)
        if decision is None:
            return
        # `adopted` 는 담기 대상 확정의 **전체 배선**을 잰다 — decompose 다음에 graph.py 가
        # 거는 코드 해소기까지 같은 순서로 태운다(프로덕션 함수를 그대로 호출한다. 순서를
        # 흉내내는 것이 아니라 graph.py 의 cart_add 분기와 같은 두 호출이다).
        if variant == "adopted" and screen is not None and screen.products and cell.pending is None:
            allowed = {pid for pid, _ in reco} | {pid for pid, _ in screen.products}
            resolved = resolve_screen_reference(
                cell.utterance,
                products=list(screen.products),
                columns=screen.columns,
                allowed_product_ids=allowed,
                deictic_markers=get_settings().screen_deictic_markers,
            )
            if resolved is not None:
                decision.cart = replace(
                    decision.cart or CartIntent(), product_id=resolved.product_id
                )
        result.runs += 1
        key = _observe(decision)
        result.observed[key] = result.observed.get(key, 0) + 1
        if cell.check(decision):
            result.hits += 1

    await asyncio.gather(*[_once(slot) for slot in range(n)])
    return result


async def _run_variant(
    variant: str, cells: list[Cell], *, n: int, concurrency: int, retries: int
) -> list[CellResult]:
    llm = get_llm()
    if llm is None:
        raise SystemExit("LLM 미구성 — 이 스크립트는 실 LLM 이 있어야 의미가 있다.")
    # 프로덕션 모듈 전역을 바꾼다(프롬프트 조립은 프로덕션 코드가 그대로 한다).
    if hasattr(decompose_mod, "_SCREEN_STYLE"):  # 설계 선택 전(측정 스위치가 살아 있던) 리비전
        style = "block" if variant.startswith("after-block") else "merge"
        setattr(decompose_mod, "_SCREEN_STYLE", style)  # noqa: B010 - 동적 속성(현 리비전엔 없다)
    elif variant == "after-merge":
        raise SystemExit(
            "after-merge(안 1)는 측정 후 폐기돼 코드에 남아 있지 않다 — 재현하려면 "
            "이 프로브를 돌린 리비전(측정 스위치 `_SCREEN_STYLE` 이 있던 시점)으로 돌아가라."
        )
    sem = asyncio.Semaphore(concurrency)
    results: list[CellResult] = []
    for index, cell in enumerate(cells, start=1):
        results.append(
            await _run_cell(cell, variant=variant, n=n, sem=sem, llm=llm, retries=retries)
        )
        print(
            f"  [{variant}] {index}/{len(cells)} {cell.group} · {cell.utterance!r}"
            f" ({cell.context}) → {results[-1].score}",
            flush=True,
        )
    return results


def _markdown(cells: list[Cell], per_variant: dict[str, list[CellResult]]) -> str:
    variants = list(per_variant)
    lines: list[str] = []
    groups: list[str] = []
    for cell in cells:
        if cell.group not in groups:
            groups.append(cell.group)
    for group in groups:
        lines.append(f"\n### {group}\n")
        lines.append(
            "| 발화 | 컨텍스트 | 목표 | 기준선 | " + " | ".join(variants) + " | 관측(마지막 변형) |"
        )
        lines.append("|---|---|---|---|" + "---|" * (len(variants) + 1))
        for index, cell in enumerate(cells):
            if cell.group != group:
                continue
            scores = [per_variant[v][index].score for v in variants]
            observed = per_variant[variants[-1]][index].observed
            obs = ", ".join(f"{k}×{v}" for k, v in sorted(observed.items(), key=lambda kv: -kv[1]))
            lines.append(
                f"| `{cell.utterance}` | {cell.context} | {cell.label} | {cell.baseline or '-'} | "
                + " | ".join(scores)
                + f" | {obs} |"
            )
        # 그룹 합계
        totals = []
        for v in variants:
            hits = sum(per_variant[v][i].hits for i, c in enumerate(cells) if c.group == group)
            runs = sum(per_variant[v][i].runs for i, c in enumerate(cells) if c.group == group)
            totals.append(f"{hits}/{runs}")
        lines.append("| **합계** | | | | " + " | ".join(f"**{t}**" for t in totals) + " | |")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="#118 라운드 2 실 LLM 프로브")
    parser.add_argument("--n", type=int, default=8, help="셀당 반복 수 (기본 8)")
    parser.add_argument(
        "--variants",
        default="before,after-merge,after-block",
        help="쉼표 구분: before,after-merge,after-block[,after-block-noaccum]",
    )
    # provider TPM 200k · 프롬프트 약 3k tok 이면 분당 60여 회가 실질 상한이다. 동시성을 올려도
    # 429 만 늘어나므로 낮게 두고 재시도로 흡수한다.
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--retries", type=int, default=6, help="429 등 호출 실패 재시도 횟수")
    parser.add_argument("--max-calls", type=int, default=1500, help="총 호출 상한 (초과 시 미실행)")
    parser.add_argument("--groups", default="", help="쉼표 구분 그룹 필터(부분 일치)")
    parser.add_argument("--out", default="", help="마크다운 결과 저장 경로")
    args = parser.parse_args()

    logging.getLogger("app").setLevel(logging.WARNING)

    cells = _build_cells()
    if args.groups:
        wanted = [g.strip() for g in args.groups.split(",") if g.strip()]
        cells = [c for c in cells if any(w in c.group for w in wanted)]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]

    planned = len(cells) * args.n * len(variants)
    print(f"셀 {len(cells)} × N={args.n} × 변형 {len(variants)} = 예상 호출 {planned}회")
    if planned > args.max_calls:
        raise SystemExit(f"예상 호출 {planned} > --max-calls {args.max_calls} — 실행하지 않는다.")

    per_variant: dict[str, list[CellResult]] = {}
    for variant in variants:
        print(f"\n■ 변형 {variant}")
        per_variant[variant] = await _run_variant(
            variant, cells, n=args.n, concurrency=args.concurrency, retries=args.retries
        )

    report = _markdown(cells, per_variant)
    errors = sum(r.errors for rs in per_variant.values() for r in rs)
    summary = [f"\n=== 요약 (N={args.n}, 호출 {planned}회, 실패 {errors}회) ==="]
    for variant, results in per_variant.items():
        hits = sum(r.hits for r in results)
        runs = sum(r.runs for r in results)
        summary.append(f"{variant}: {hits}/{runs}")
    print(report)
    print("\n".join(summary))

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report + "\n" + "\n".join(summary) + "\n", encoding="utf-8")
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    v: [
                        {
                            "group": r.cell.group,
                            "utterance": r.cell.utterance,
                            "context": r.cell.context,
                            "score": r.score,
                            "observed": r.observed,
                        }
                        for r in rs
                    ]
                    for v, rs in per_variant.items()
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n결과 저장: {path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
