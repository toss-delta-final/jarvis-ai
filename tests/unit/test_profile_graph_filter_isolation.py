"""INV-PGRAPH-ORDER — 그래프 데이터가 검색 필터에 닿지 못하게 구조로 막는다 (#361).

`SPEC-PROFILE-GRAPH-149` §6.12 [HARD]: 그래프 파생 데이터는 **(i) rerank 프롬프트의 순서 지시**와
**(ii) 랭킹 점수 항(홈 프로필 벡터)** 에만 들어갈 수 있다. 검색 필터 필드·검색 API 쿼리 파라미터·
스레드 필터 저장소에 **닿아서는 안 된다.**

#119 는 이미 한 번 밟은 지뢰다 — 프로필을 필터 생성 단계에 주입한 구성이 게스트 대비 nDCG@10
**−0.288**, 31턴 중 29턴에서 가격·브랜드·평점 축이 오염됐다. 실제 손실을 만든 것은 필터가
생성된 사실이 아니라 **그것이 스레드 저장소에 영속돼 다음 턴에 재주입된 것**이었다(REQ-PGRAPH-116).

## 이 파일이 왜 규약 주석이 아니라 테스트인가

api-spec §3.8 v0.32.0 이 `usagePolicy.filterSafe` 와이어 필드를 폐기하면서 *"필드가 지키던 것이
아니라 문장이 지키던 것"* 이라고 정리했고, SPEC v0.3.5(#360)가 그에 맞춰 REQ-PGRAPH-111 을 폐기하며
집행 책임을 **계약 문장 + 코드 구조**로 옮겼다. 그 "코드 구조"가 이 파일이다.

## 두 겹으로 막는다

- **정적**(`_GRAPH_MODULES` 대상) — 필터 타입을 import 하지 않고, 필터 모양 타입을 정의하지 않고,
  공개 함수가 반환형을 밝힌다. glob 이라 **내일 추가되는 모듈도 자동으로 대상**이 되는 것이 핵심이다.
- **행동**(`run_buyer_turn`) — 그래프가 꽉 찬 회원과 게스트의 decompose 프롬프트·검색 페이로드·
  스레드 필터 저장소가 같다.

두 겹이 필요한 이유는 서로의 사각지대가 다르기 때문이다. 정적 검사는 **`-> dict` 반환을 막지
못한다**(파이썬 타입만으로는 원리상 불가능하다). 행동 검사는 fake LLM 이 프롬프트와 무관하게 고정
JSON 을 돌려주므로 **LLM 을 경유한 유출을 직접 재지 못한다** — 대신 프롬프트 바이트 동등성이 그
입력을 봉인하고, 페이로드·저장소 동등성이 **decompose 이후 코드가 필터를 만지는 경로**(승계·완화·
사후 가공)를 잡는다. 어느 한쪽만 두면 나머지 절반이 열린다.

정적 검사가 언젠가 **정당한** import 를 막게 되면 — 예컨대 프로필 쪽이 `SpringProduct` 를 쓰게
되면 — 테스트를 지우지 말고 **심볼 단위 예외 + 근거 주석**을 남긴다. 경계가 왜 있는지 읽게 만드는
것이 이 테스트의 목적이다.

## 살아 있음을 실측으로 확인했다 (2026-08-10)

decompose 직후 회원에게만 `filters.brand=["소니"]` · `filters.price_min=50000` 을 심는 변이를
넣고 돌렸더니 **행동 테스트 5건이 전부 실패**했고, 되돌리니 전부 통과했다. 그 과정에서 fake 검색이
필터를 무시하면 후보 집합 비교가 아무것도 재지 못한다는 사실이 드러나 `_RecordingSearch` 가 필터를
실제로 적용하도록 고쳤다 — 그 전에는 유출을 심어도 후보가 3건 그대로였다.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.agents.buyer.graph import get_thread_store
from app.core.config import get_settings
from app.schemas.profile_graph import GraphEdgeView
from app.schemas.spring import ProductSearchFilters, ProductSearchResult
from app.services.spring_client import _search_query_params
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM
from tests._graph_fixtures import GRAPH_LABELS, seed_full_graph
from tests.unit.test_recommendation import (
    _RecordingPush,
    _collect,
    _fast_prompt,
    _guest,
    _member,
    _req,
    _smart_prompt,
    _thread_key,
    run_buyer_turn,
)

_REPO_ROOT = Path(__file__).parents[2]

# glob 이 빗나가면(디렉터리 이동·경로 오타) 아래 정적 검사가 **빈 집합을 훑고 통과**한다.
# 하한을 박아 그 실패 모드를 실패로 만든다 — 실측 19개.
_MIN_GRAPH_MODULES = 15

# 그래프가 사는 곳 전부. **glob 이라 새 모듈이 자동으로 들어온다** — 목록을 손으로 유지하면
# 내일 추가되는 `graph_filters.py` 가 검사를 비껴간다.
_GRAPH_MODULE_PATHS = sorted(
    (_REPO_ROOT / "app" / "agents" / "profile").glob("*.py"),
) + [
    _REPO_ROOT / "app" / "api" / "profile_graph.py",
    _REPO_ROOT / "app" / "schemas" / "profile_graph.py",
]

# 검색 필터 축의 **단일 출처**. 목록을 복제하면 새 축이 생겼을 때 한쪽만 늘어난다
# (`spring_client.search_filter_axes` 가 payload 축의 단일 출처인 것과 같은 규약).
_FILTER_AXES = set(ProductSearchFilters.model_fields)

# 필터 타입이 사는 모듈. 이 하나만 금지한다 — 상위 패키지로 넓히면 투영 계층이 쓰는
# `app.schemas.profile_graph` 가 함께 걸린다.
_FILTER_MODULE = ProductSearchFilters.__module__

# 필터 모양 판정 임계. 1 로 두면 `limit`·`category` 같은 흔한 이름 하나로 오탐이 난다.
_FILTER_SHAPE_MIN_OVERLAP = 2


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts)


def _parse(path: Path) -> ast.Module:
    # encoding 명시 — 미지정이면 로케일 기본(Windows 한국어는 cp949)이라 소스의 한국어 주석에서
    # UnicodeDecodeError 가 난다. 저장소 소스는 전부 UTF-8 이다(`test_order_status.py` 와 동일).
    return ast.parse(path.read_text(encoding="utf-8"))


def _public_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """모듈 최상위 공개 함수. `__all__` 이 없는 모듈이 대부분이라 이름 규칙으로 가른다."""
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    ]


def _import_targets(tree: ast.Module, module_name: str) -> set[str]:
    """이 모듈이 끌어들이는 대상의 **완전한 점 표기 경로**를 전부 낸다.

    `node.module` 문자열을 그대로 대조하면 같은 물건을 가리키는 다른 표기를 놓친다 —
    `from app.schemas import spring`(`node.module` 이 `"app.schemas"`)나 상대 import
    (`from ..schemas.spring import X`)가 그렇다. 그래서 `module` 과 `alias` 를 이어 붙인
    **대상 경로**를 만들어 대조한다. 상대 경로는 파일 위치로 절대 경로를 복원한다.
    """
    package = module_name.rsplit(".", 1)[0]
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - (node.level - 1)])
                if node.module:
                    base = f"{base}.{node.module}"
            else:
                base = node.module or ""
            targets.add(base)
            targets.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return targets


def _references_filter_type(tree: ast.Module) -> bool:
    """`ProductSearchFilters` 라는 이름을 **어떤 형태로든** 만지는가.

    import 경로 대조만으로는 반환형에도 안 드러나고 파라미터·내부 변수로만 쓰이는 경로가 남는다
    (`from app import schemas` 후 `schemas.spring.ProductSearchFilters` 같은 표기). 식별자
    자체를 보면 import 표기와 무관하게 잡힌다. AST 라 주석·문자열은 세지 않는다 — docstring 에서
    이 타입을 **언급**하는 것은 위반이 아니라 설명이다.
    """
    return any(
        (isinstance(node, ast.Name) and node.id == "ProductSearchFilters")
        or (isinstance(node, ast.Attribute) and node.attr == "ProductSearchFilters")
        for node in ast.walk(tree)
    )


def _field_names(obj: type) -> set[str]:
    """필터 모양 판정을 위한 필드명 집합 — BaseModel · dataclass · TypedDict 를 모두 본다."""
    if isinstance(obj, type) and issubclass(obj, BaseModel):
        return set(obj.model_fields)
    if dataclasses.is_dataclass(obj):
        return {field.name for field in dataclasses.fields(obj)}
    if isinstance(getattr(obj, "__annotations__", None), dict):
        return set(obj.__annotations__)
    return set()


# ─────────── 정적 — REQ-PGRAPH-110 ───────────


def test_graph_modules_do_not_reach_the_search_filter_type() -> None:
    """그래프 모듈은 검색 필터 타입을 **알지 못한다** (REQ-PGRAPH-110).

    함수 안 지연 import 도 `ast.walk` 가 잡는다. **`app.schemas` 전체를 금지하지는 않는다** —
    투영 계층(`graph_projection`)이 와이어 모델 `app.schemas.profile_graph` 를 정당하게 쓴다.
    금지 대상은 필터 타입이 사는 모듈 하나이고, 접두 매칭(`"app.schemas" in ...`)으로 넓히면
    그 정당한 import 가 즉시 오탐이 된다.

    두 각도로 본다 — **경로**(`_import_targets`)와 **식별자**(`_references_filter_type`).
    경로만 보면 `from app import schemas` 후 `schemas.spring.X` 같은 표기를 놓치고, 식별자만
    보면 모듈을 통째로 끌어와 다른 이름으로 쓰는 경로를 놓친다. 둘을 겹쳐야 "어떤 표기로도
    닿지 못한다"가 된다(PR #565 리뷰).
    """
    assert len(_GRAPH_MODULE_PATHS) >= _MIN_GRAPH_MODULES, "검사 대상 모듈이 사라졌다 — glob 확인"
    offenders: list[str] = []
    for path in _GRAPH_MODULE_PATHS:
        tree, where = _parse(path), _module_name(path)
        reached = sorted(
            target
            for target in _import_targets(tree, where)
            if target == _FILTER_MODULE or target.startswith(f"{_FILTER_MODULE}.")
        )
        if reached:
            offenders.append(f"{where}: imports {reached}")
        if _references_filter_type(tree):
            offenders.append(f"{where}: ProductSearchFilters 식별자를 쓴다")
    assert offenders == [], (
        f"그래프 모듈이 검색 필터 타입에 닿는다 — INV-PGRAPH-ORDER 위반 경로가 열렸다: {offenders}"
    )


@pytest.mark.parametrize(
    ("spelling", "source"),
    [
        # `node.module` 이 "app.schemas" 라 경로 문자열 대조를 빠져나간다. 반환형에도 안 드러난다.
        (
            "from-package",
            "from app.schemas import spring\ndef f(x: spring.ProductSearchFilters): ...",
        ),
        # 상대 경로 — `node.module` 이 "schemas.spring" 이라 절대 경로 대조가 빗나간다.
        (
            "relative",
            "from ...schemas.spring import ProductSearchFilters\ndef f(x):\n    y: ProductSearchFilters = x",
        ),
        # 패키지만 끌어와 점 접근 — **식별자 검사만** 잡는다.
        (
            "attribute-chain",
            "from app import schemas\ndef f(x):\n    y = schemas.spring.ProductSearchFilters()",
        ),
        # 별칭 — **경로 검사만** 잡는다(식별자가 소스에 안 나온다).
        ("aliased", "import app.schemas.spring as sp\ndef f(): ..."),
        # 함수 안 지연 import — 모듈 최상위만 보면 놓친다.
        ("lazy", "def f():\n    from app.schemas.spring import ProductSearchFilters"),
    ],
)
def test_import_guard_catches_evasive_spellings(spelling: str, source: str) -> None:
    """가드가 **표기를 바꾼 우회**를 잡는가 (PR #565 리뷰).

    위 가드는 통과 상태만으로는 "지키고 있다"와 "안 보고 있다"가 구분되지 않는다. 우회 표기를
    실제로 심어 잡히는 것을 여기서 고정한다 — 리뷰가 지적한 두 형태(`from-package`·`relative`)와
    그 지적을 일반화한 셋을 함께 둔다.

    `attribute-chain` 은 식별자 검사만, `aliased`·`lazy` 는 경로 검사만 잡는다 — **두 각도가
    모두 필요하다는 실증**이라 한쪽을 지우면 이 표에서 바로 드러난다.
    """
    tree = ast.parse(source)
    targets = _import_targets(tree, "app.agents.profile.probe")
    reached = any(t == _FILTER_MODULE or t.startswith(f"{_FILTER_MODULE}.") for t in targets)
    assert reached or _references_filter_type(tree), f"{spelling} 표기가 가드를 빠져나간다"


def test_import_guard_allows_the_wire_schema() -> None:
    """투영 계층이 쓰는 `app.schemas.profile_graph` 는 걸리지 않는다.

    접두 매칭(`"app.schemas" in ...`)으로 넓히자는 안이 여기서 깨진다 — 금지 대상은 필터 타입이
    사는 모듈 하나다.
    """
    tree = ast.parse("from app.schemas.profile_graph import GraphEdgeView\ndef f(): ...")
    targets = _import_targets(tree, "app.agents.profile.probe")
    assert not any(t == _FILTER_MODULE or t.startswith(f"{_FILTER_MODULE}.") for t in targets)
    assert not _references_filter_type(tree)


def test_graph_modules_define_no_filter_shaped_type() -> None:
    """자작 필터 객체도 막는다 (REQ-PGRAPH-110).

    import 검사만으로는 그래프 모듈이 **자기 필터 모양 타입을 새로 정의해** 돌려주는 경로가
    남는다. 필드명이 검색 축과 겹치는 정도로 판정한다 — 이름 대조가 아니라 구조 대조여야
    `GraphSearchHints` 같은 다른 이름의 같은 물건을 잡을 수 있다.
    """
    offenders: list[str] = []
    inspected = 0
    for path in _GRAPH_MODULE_PATHS:
        module = importlib.import_module(_module_name(path))
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__ or name.startswith("_"):
                continue
            inspected += 1
            overlap = _field_names(obj) & _FILTER_AXES
            if len(overlap) >= _FILTER_SHAPE_MIN_OVERLAP:
                offenders.append(f"{module.__name__}.{name} -> {sorted(overlap)}")
    assert inspected > 0, "공개 클래스를 하나도 못 찾았다 — 검사가 공허하다"
    assert offenders == [], (
        "그래프 모듈이 필터 모양 타입을 정의한다 — 후보를 좁히는 물건은 검색 계약이 소유한다"
        f"(REQ-PGRAPH-115): {offenders}"
    )


def test_every_public_graph_function_declares_a_return_type() -> None:
    """반환형을 밝히지 않으면 위 검사가 조용히 비어 버린다.

    애노테이션이 있으면 그것이 필터 타입을 이름으로 언급하는지도 함께 본다 — 문자열 애노테이션
    (`-> "ProductSearchFilters"`)이나 별칭 import 를 통한 우회를 여기서 잡는다.
    """
    missing: list[str] = []
    named_filter: list[str] = []
    inspected = 0
    for path in _GRAPH_MODULE_PATHS:
        for node in _public_functions(_parse(path)):
            inspected += 1
            where = f"{_module_name(path)}.{node.name}"
            if node.returns is None:
                missing.append(where)
                continue
            if "ProductSearchFilters" in ast.unparse(node.returns):
                named_filter.append(where)
    assert inspected > 0, "공개 함수를 하나도 못 찾았다 — 검사가 공허하다"
    assert missing == [], f"반환형이 없는 공개 함수: {missing}"
    assert named_filter == [], f"필터 타입을 반환하는 공개 함수: {named_filter}"


def test_graph_wire_view_carries_no_filter_axis() -> None:
    """와이어 항목(I-32 `edges[]`)에 필터 축이 붙지 못하게 잠근다.

    #360 이 확정한 5필드 계약에 `priceMin` 같은 축이 하나라도 얹히면, 소비자가 "필터로 적용"을
    **계약 변경 없이** 구현할 수 있게 된다 — 폐기된 `usagePolicy.filterSafe` 가 막으려던 바로
    그 일이다(api-spec §3.8 v0.32.0).
    """
    assert set(GraphEdgeView.model_fields) & _FILTER_AXES == set()


# ─────────── 행동 — REQ-PGRAPH-112·113·115·116 ───────────


class _RecordingSearch:
    """필터를 보관하고 **실제로 적용한다** — 회원·게스트 페이로드/후보 대조용.

    `tests._fakes.FakeBackend` 처럼 필터를 무시하면 후보 집합이 언제나 3건이라
    `test_avoids_does_not_shrink_the_candidate_set` 이 아무것도 재지 못한다(유출을 심어도
    후보가 안 줄어드니 통과한다 — 실측으로 확인했다). 그래서 여기서는 Spring 축(category ·
    price · brand)과 AI 사후필터 축(rating · exclude)을 모두 적용해, 필터로 새는 값이
    **후보 감소로 드러나게** 한다.
    """

    def __init__(self, products: list | None = None) -> None:
        self._products = DEFAULT_PRODUCTS if products is None else products
        self.calls: list[ProductSearchFilters] = []

    async def __call__(self, filters, exclude_product_ids=None):  # noqa: ANN001
        self.calls.append(filters)
        excluded = set(exclude_product_ids or []) | set(filters.exclude_product_ids or [])
        matched = [
            product
            for product in self._products
            if (filters.category is None or product.category == filters.category)
            and (filters.price_min is None or product.price >= filters.price_min)
            and (filters.price_max is None or product.price <= filters.price_max)
            and (not filters.brand or product.brand in set(filters.brand))
            and (filters.rating_min is None or product.rating >= filters.rating_min)
            and product.product_id not in excluded
        ]
        return ProductSearchResult(products=matched, total_count=len(matched))

    def payloads(self) -> list[dict]:
        """실제로 Spring I-1 에 나가는 쿼리 파라미터 — 완료 조건의 "검색 요청 페이로드"다."""
        return [_search_query_params(filters) for filters in self.calls]

    def dumps(self) -> list[dict]:
        """필터 전량. `rating_min`·`attr_conditions`·`exclude_product_ids` 는 Spring 축이
        아니지만 `search_service.apply_ai_side_filters` 가 **후보를 실제로 줄이므로**
        REQ-PGRAPH-115 의 "후보 집합"은 이쪽까지다."""
        return [filters.model_dump() for filters in self.calls]


async def _turn(identity, *, session: str, thread: str, llm: FakeLLM, search: _RecordingSearch):  # noqa: ANN001
    request = _req(session_id=session, thread_id=thread)
    await _collect(
        run_buyer_turn(request, identity, llm=llm, search=search, push_fn=_RecordingPush())
    )
    return request


async def _member_and_guest() -> tuple[dict, dict]:
    """꽉 찬 그래프 회원과 게스트를 각각 한 턴 돌리고 관측치를 모은다."""
    await seed_full_graph()
    observed = []
    for identity, session, thread in ((_member(), "s-m", "t-m"), (_guest(), "s-g", "t-g")):
        llm, search = FakeLLM(), _RecordingSearch()
        request = await _turn(identity, session=session, thread=thread, llm=llm, search=search)
        thread_store = await get_thread_store()
        observed.append(
            {
                "fast": _fast_prompt(llm),
                "smart": _smart_prompt(llm),
                "payloads": search.payloads(),
                "dumps": search.dumps(),
                "stored": await thread_store.get(await _thread_key(request, identity)),
            }
        )
    return observed[0], observed[1]


async def test_full_graph_member_decompose_prompt_is_identical_to_guest() -> None:
    """[REQ-PGRAPH-113] 그래프가 꽉 찬 회원의 decompose 프롬프트가 게스트와 **바이트 동일**하다.

    기존 #119 회귀(`test_member_decompose_prompt_is_identical_to_guest`)는 요약 한 줄만 심고
    잰다 — 프로필이 사실상 비어 있어 통과하는 것일 수 있다. 여기서는 모든 `NodeType` 이 채워진
    그래프와 그 파생 요약을 심고 같은 것을 잰다.
    """
    member, guest = await _member_and_guest()

    assert member["fast"] == guest["fast"]
    leaked = [label for label in GRAPH_LABELS if label in member["fast"]]
    assert leaked == [], f"그래프 라벨이 필터 생성 프롬프트에 실렸다: {leaked}"


async def test_full_graph_member_search_payload_is_identical_to_guest() -> None:
    """[REQ-PGRAPH-115] 검색 요청 페이로드가 동등하다 — 회원의 후보가 좁아질 수 없다.

    프롬프트 동등성이 LLM 입력을 봉인한다면, 이쪽은 **decompose 이후 코드가 필터를 만지는 경로**
    (카테고리 승계·완화·사후 가공)를 봉인한다. 축 집합이 아니라 **값까지** 비교한다 —
    유출된 `maxPrice=50000` 과 정상 `maxPrice=50000` 은 축이 같아 구분되지 않는다.
    """
    member, guest = await _member_and_guest()

    assert member["dumps"] == guest["dumps"]
    assert member["payloads"] == guest["payloads"]
    assert member["payloads"], "검색이 한 번도 호출되지 않으면 이 테스트는 아무것도 재지 않는다"


async def test_full_graph_member_thread_filters_are_identical_to_guest() -> None:
    """[REQ-PGRAPH-116] 영속되는 필터가 동등하고 그래프 유래 값을 담지 않는다.

    **동등성과 부재가 둘 다 필요하다.** 동등성만 보면 "회원과 게스트가 똑같이 오염된" 상태를
    통과시키고, 부재(라벨 검사)만 보면 숫자 축(`price_max=100000`)을 놓친다. 여기가 #119 의
    증폭 루프가 시작되는 지점이라 두 겹으로 잠근다.
    """
    member, guest = await _member_and_guest()

    assert member["stored"] is not None, "필터가 저장되지 않으면 이 테스트는 공허하다"
    assert member["stored"].model_dump() == guest["stored"].model_dump()

    blob = json.dumps(member["stored"].model_dump(), ensure_ascii=False)
    leaked = [label for label in GRAPH_LABELS if label in blob]
    assert leaked == [], f"그래프 유래 값이 스레드 필터 저장소에 영속됐다: {leaked}"


async def test_avoids_does_not_shrink_the_candidate_set() -> None:
    """[REQ-PGRAPH-112] `avoids` 는 순위 강등까지만 — 후보 배제로 승격되지 않는다.

    픽스처의 회피 브랜드 `BrandY` 는 fake 카탈로그에 실재하므로, 배제로 승격되면 후보가 실제로
    준다(`test_graph_fixtures` 가 그 성질을 고정한다).

    **"순위만 다르다"까지 주장하지는 않는다** — `FakeLLM` 의 rerank 는 프롬프트와 무관하게 고정
    순서를 돌려주므로 이 하네스에서 순위가 갈릴 수 없다. 여기서 증명 가능한 것은 ① 후보 집합이
    같다 ② 취향이 순위 경로에는 여전히 도달한다 둘이고, 순위 효과의 실측은 #147 평가 하네스
    (`evals/personalization`)의 몫이다.
    """
    member, guest = await _member_and_guest()

    def _candidate_ids(observed: dict) -> list[int]:
        # rerank 프롬프트에 실린 후보가 곧 "좁혀진 뒤" 남은 집합이다.
        candidates = json.loads(observed["smart"].split("CANDIDATES: ")[-1])
        assert candidates, "rerank 후보가 비면 집합 비교가 공허하다"
        return sorted(product["productId"] for product in candidates)

    assert _candidate_ids(member) == _candidate_ids(guest)
    assert "BrandY" in member["smart"], "회피 취향이 순위 경로에도 안 닿으면 기능이 사라진 것이다"
    assert "BrandY" not in member["fast"]


async def test_full_graph_fixture_is_potent_when_scope_is_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**유효성 대조군 — 이 파일에서 가장 중요한 테스트.**

    위 동등성 단언들은 픽스처가 무기력해도(요약이 비었거나, 시딩이 실패했거나, 하네스가 프로필을
    아예 안 읽거나) 전부 초록불이다. `profile_injection_scope="both"` 로 #119 이전 배선을 되살리면
    프롬프트가 **실제로 갈라지고 그래프 라벨이 나타나야** 한다 — 그래야 위 초록불이 의미를 갖는다.

    이 레버(`app/core/config.py` `profile_injection_scope`)는 #119 의 롤백 경로라 유지된다.
    """
    monkeypatch.setattr(get_settings(), "profile_injection_scope", "both")
    member, guest = await _member_and_guest()

    assert member["fast"] != guest["fast"], (
        "scope=both 로 열었는데도 프롬프트가 같다 — 픽스처가 회원 경로에 닿지 않고 있다"
    )
    assert "소니" in member["fast"]
