"""앵커 정답지 로더 — 해시 게이트, 셀 전개, decompose 컨텍스트 조립."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.buyer.recommendation.decompose import ScreenPrompt, build_screen_prompt
from app.core.config import Settings
from app.schemas.chat import ScreenContext, ScreenProduct
from app.schemas.spring import ProductSearchFilters
from evals.intent_probe.schema import AnchorSet, ProbeContext, ScreenFixture, Utterance

FIXTURE_DIR = Path(__file__).with_name("fixtures")
BUILTIN_FIXTURES = {"a": "anchors_a.json", "b": "anchors_b.json"}


@dataclass(frozen=True)
class Cell:
    """측정 단위 = 발화 1개 × 컨텍스트 1개. 이 셀을 N회 반복해 분포를 만든다."""

    cell_id: str
    utterance: Utterance
    context: ProbeContext


def resolve_fixture_path(name: str, *, fixture_dir: Path | None = None) -> Path:
    """`a`/`b` 별칭 또는 임의 경로를 앵커 파일 경로로 푼다."""
    directory = fixture_dir or FIXTURE_DIR
    if name in BUILTIN_FIXTURES:
        return directory / BUILTIN_FIXTURES[name]
    return Path(name)


def fixture_sha256(path: Path) -> str:
    """앵커 파일의 sha256 — 산출물에 그대로 실려 '무엇을 쟀는지'를 남긴다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_anchor_set(name: str = "b", *, fixture_dir: Path | None = None) -> AnchorSet:
    """앵커 파일을 읽는다. 커밋된 픽스처는 manifest 해시와 대조한다.

    manifest 에 없는 외부 경로(후보 정답지 실험)는 해시 대조를 건너뛰되, 호출부가
    `fixture_sha256` 으로 산출물에 해시를 남긴다 — 어떤 정답지로 잰 표인지 항상 특정된다.
    """
    path = resolve_fixture_path(name, fixture_dir=fixture_dir)
    manifest_path = path.parent / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("sha256", {}).get(path.name)
        if expected and expected != fixture_sha256(path):
            raise ValueError(
                f"{path.name} 의 SHA-256 이 manifest 와 다릅니다 — 손상되었거나 수정됐습니다"
            )
    return AnchorSet.model_validate_json(path.read_text(encoding="utf-8"))


def build_cells(anchors: AnchorSet) -> list[Cell]:
    """발화 × 컨텍스트를 전개한다. cellId 정렬이라 동시 실행에도 순서가 결정론이다."""
    by_id = {context.context_id: context for context in anchors.contexts}
    cells = [
        Cell(
            cell_id=f"{utterance.utterance_id}|{context_id}",
            utterance=utterance,
            context=by_id[context_id],
        )
        for utterance in anchors.utterances
        for context_id in utterance.contexts
    ]
    return sorted(cells, key=lambda cell: cell.cell_id)


def _screen_context(screen: ScreenFixture) -> ScreenContext:
    """`ScreenFixture`(픽스처) → `ScreenContext`(앱 스키마). [#300, D-4/§4.3]

    라벨은 픽스처에 쓰지 않고 프로덕션 투영(`decompose.build_screen_prompt`)을 태워야 하므로,
    여기서는 `page_type` 원시값만 옮긴다 — 표시명 매핑은 호출부가 `settings.screen_page_type_labels`
    로 건다.
    """
    return ScreenContext(
        page_type=screen.page_type,
        filters=dict(screen.filters),
        products=[
            ScreenProduct(product_id=product.product_id, name=product.name)
            for product in screen.products
        ],
        columns=screen.columns,
    )


def build_screen_prompt_for_context(
    anchors: AnchorSet, context: ProbeContext, *, settings: Settings
) -> ScreenPrompt | None:
    """이 컨텍스트가 실을 화면 맥락을 만든다 — **프로덕션 투영 경로 그대로**(§4.3).

    `context.include_screen` 이 거짓이면 None(=decompose 프롬프트에 SCREEN 블록이 안 실린다,
    `graph.py` 의 `screen_context_active` 게이트와 같은 모양). 참이면 `context.screen_ref` 가
    가리키는 `ScreenFixture` 를 `app.schemas.chat.ScreenContext` 로 만들고
    `decompose.build_screen_prompt(labels=settings.screen_page_type_labels)` 를 그대로 불러
    표시명(예: `chat` → `인기 상품`)을 프로덕션과 같은 매핑으로 붙인다 — 라벨 문자열을 픽스처에
    직접 쓰지 않는다.
    """
    if not context.include_screen or context.screen_ref is None:
        return None
    screen = next(screen for screen in anchors.screens if screen.screen_id == context.screen_ref)
    return build_screen_prompt(_screen_context(screen), labels=settings.screen_page_type_labels)


def build_context_kwargs(
    anchors: AnchorSet, context: ProbeContext, *, settings: Settings
) -> dict[str, Any]:
    """decompose 가 받는 세션 상태 인자를 만든다.

    PENDING_CART 모양은 app/agents/buyer/graph.py 가 실제로 넘기는 dict 와 같아야 한다
    (quantity·attempts 는 프롬프트에 싣지 않는다).

    [#84] 어느 PRIOR_FILTERS 를 싣는지는 `context.prior_filters_ref` 가 고른다 — 기본값
    `default` 는 기존 컨텍스트가 쓰던 `anchors.prior_filters` 그대로다.

    [#300] `settings` 는 **필수**다(기본값을 두지 않는다) — screen 라벨 투영에 쓰이는데, 러너가
    이미 받고 있는 `settings` 를 그대로 넘겨야 한다(CLAUDE.md 「튜너블 하드코딩 금지」, 여기서는
    "새로 get_settings() 를 부르지 않는다"는 형태로 지킨다). `last_recommendations_ref` 가
    `screen` 이면 `anchors.screen_last_recommendations` 를 싣는다 — screen 컨텍스트 전용 목록
    (D-4 근거: 기본 lastRecommendations 와 화면 이름이 겹치면 이름 매칭 셀이 무의미해진다).
    """
    pending = None
    if context.include_pending_cart:
        pending = {
            "productId": anchors.reask_product_id,
            "options": [
                {"optionId": option.option_id, "name": option.name} for option in anchors.options
            ],
        }
    prior_source = (
        anchors.category_prior_filters
        if context.prior_filters_ref == "category"
        else anchors.prior_filters
    )
    reco_source = (
        anchors.screen_last_recommendations
        if context.last_recommendations_ref == "screen"
        else anchors.last_recommendations
    )
    return {
        "prior_filters": (
            ProductSearchFilters.model_validate(prior_source)
            if context.include_prior_filters
            else None
        ),
        "last_recommendations": (
            [(product.product_id, product.name) for product in reco_source]
            if context.include_last_recommendations
            else None
        ),
        "pending_cart": pending,
        "screen": build_screen_prompt_for_context(anchors, context, settings=settings),
    }
