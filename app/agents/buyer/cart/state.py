"""장바구니 서브그래프 스레드 상태 (이슈 #3, 이슈 #33 — pg-profile BaseStore 이관).

두 가지를 스레드 스코프(신원 스코프 키)로 보관한다 — LangGraph BaseStore(pg-profile) 백엔드:
  - last_reco    : 직전 추천 후보의 productId — "그거 담아줘"의 productId 해소 소스(경로 B라
                   SSE엔 카드가 없으므로 AI가 문맥으로 상품을 확정한다). 상품명은 pg-profile 에
                   저장하지 않고 프로세스 로컬 휘발성 캐시에만 둔다(아래 _last_reco_names 참조).
  - pending_add  : 옵션 되물음 진행 상태(CART_OPTION_REQUIRED/INVALID) — 다음 턴에서 사용자 답을
                   optionId 로 해석해 재담기(§4.1 멀티턴).
프로덕션은 app.core.pg_store 공유 연결(pg-profile), 테스트는 기본 InMemoryStore()(무인자 생성자)
또는 명시 주입 — app/agents/seller/history.py 와 동일한 BaseStore 이관 패턴(§6.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.core import pg_store
from app.core.config import get_settings
from app.core.pg_resilience import BoundedLRUCache, run_with_query_timeout
from app.schemas.spring import CartOption

_NAMESPACE_ROOT = "buyer_cart_v2"
_LAST_RECO_KEY = "last_reco"
_PENDING_KEY = "pending"

# last_reco 의 상품명(product.name 원본 컬럼 사본)은 pg-profile 에 저장하지 않는다 — CLAUDE.md
# "AI Postgres 에는 AI 생성물만 저장, 상품 원본 컬럼 사본 금지"(PR #46 후속 리뷰). pg-profile 엔
# productId(참조 식별자)만 영속하고, 이름은 decompose 프롬프트 문맥용으로만 쓰이므로 이 프로세스
# 로컬 휘발성 캐시(thread key → {productId: name})에 둔다. 같은 프로세스 수명 동안에는 이름
# disambiguation("파란 니트 담아줘")이 정상 동작하고, 재시작(캐시 소실) 후에는 pid 만 pg-profile
# 에서 복원되어 이름은 "" 로 degrade 한다("그거 담아줘" pid 해소는 계속 작동).
# 이름 캐시는 config 기반 bounded LRU라 무제한 증가하지 않는다(이슈 #50). 다중 인스턴스에서
# 추천/담기가 다른 인스턴스에 걸리거나 LRU miss가 나면 이름만 ""로 degrade하고, pg-profile의
# productId 허용 목록은 그대로 유지한다.
_last_reco_names: BoundedLRUCache[str, dict[int, str]] = BoundedLRUCache(
    max_entries=get_settings().state_store_local_cache_max_entries
)


@dataclass(frozen=True, slots=True)
class LastReco:
    """누적 추천 목록과 **이번 턴 경계**.

    `items` 는 최근 언급 순 누적 전체 — 담기 가드(`allowed`)가 쓰는 목록이다(정본 §3.1 [보안]).
    `turn_count` 는 그중 앞에서 몇 건이 **직전 추천 턴 하나**에서 온 것인지다. 프롬프트에 승계분을
    실을지 말지를 호출부가 이걸로 가른다 — 왜 필요한지는 `app/agents/buyer/graph.py` 의
    `prompt_reco` 주석에 실측 수치와 함께 적어 뒀다.
    """

    items: list[tuple[int, str]]
    turn_count: int


@dataclass
class PendingAdd:
    """옵션 되물음 진행 상태. attempts = CART_OPTION_INVALID 재질문 횟수(상한 config)."""

    product_id: int
    quantity: int
    options: list[CartOption] = field(default_factory=list)
    attempts: int = 0


class CartStateStore:
    """스레드별 last_reco + pending_add. 키는 신원 스코프(IDOR 방지)."""

    def __init__(self, store: BaseStore | None = None) -> None:
        self._store = store or InMemoryStore()

    def _ns(self, key: str) -> tuple[str, str]:
        return (_NAMESPACE_ROOT, key)

    async def set_last_reco(self, key: str, items: list[tuple[int, str]]) -> None:
        """이번 턴 추천을 **스레드 누적 목록에 합류**시킨다 (#118 — 담기 가드의 시간 축 보존).

        덮어쓰기였을 때 이 시나리오가 깨졌다: 추천 A(101·102·103) → "101 방수야?" → 추천 B
        (301·302) → "이거 담아줘". 3단계에서 101 이 사라져 담기가 차단된다. 정본 §3.1 [보안]이
        담기 허용 목록을 **"(누적 추천 목록 ∪ screen.products)"** 로 정의하므로 누적이 계약이다.

        순서는 **최근 언급 순** — 이번 턴 항목이 앞, 그다음이 직전 턴이다. 같은 productId 는
        최신 위치로 승격하고 중복을 남기지 않는다(프롬프트 LAST_RECOMMENDATIONS 가 이 순서로
        실리므로, 모델이 "직전에 본 것"을 앞에서 찾는다).

        **[HARD] 이번 턴 항목은 상한에 잘리지 않는다.** I-21 은 한 턴에 최대 MAX_LISTS(10) ×
        LIST_MAX_PRODUCTS(9) = 90건을 밀어 넣을 수 있어, 단순 `merged[:cap]` 이면 **방금 추천한
        상품이 담기 차단되는 회귀**가 된다. 그래서 상한은 `max(cap, len(이번 턴))` 로 걸어
        **승계분에만** 실효적으로 적용한다.

        저장 값에 `turn_count`(이번 턴 건수)를 **덧붙이기만** 한다 — 기존 `product_ids` 키는 그대로라
        구버전 인스턴스는 새 키를 무시하고 지금처럼 읽는다. 반대로 새 인스턴스가 구버전이 쓴 값을
        읽으면 키가 없으므로 `get_last_reco_state` 가 **전량을 이번 턴으로 간주**해 오늘 동작으로
        degrade 한다(KeyError 로 담기 턴을 죽이지 않는다). 롤링 배포 중 신구 혼재 양방향이 안전하다.

        동시성: 읽고-고쳐-쓰기(RMW)지만 락을 두지 않는다. 같은 threadId 의 동시 스트림은 §2.9 a
        의 "방당 1스트림" 레지스트리가 **409 로 먼저 막고**(app/core/stream.py), 이 함수는 그
        스트림 안에서만 불린다 — 경합할 두 번째 쓰기가 애초에 존재하지 않는다.
        """
        cap = get_settings().last_reco_max
        current = await self.get_last_reco(key)
        fresh_ids = {pid for pid, _ in items}
        merged = items + [(pid, name) for pid, name in current if pid not in fresh_ids]
        capped = merged[: max(cap, len(items))]

        # pg-profile 엔 productId 만 영속(상품명 사본 금지). 이름은 휘발성 캐시에만 둔다.
        await run_with_query_timeout(
            self._store.aput(
                self._ns(key),
                _LAST_RECO_KEY,
                {
                    "product_ids": [pid for pid, _ in capped],
                    # merged 는 이번 턴 항목으로 시작하고 상한이 그것들을 자르지 않으므로
                    # 앞 len(items) 건이 곧 이번 턴 분량이다(방어적으로 clamp).
                    "turn_count": min(len(items), len(capped)),
                },
            )
        )
        # 이름 캐시도 **병합**한다 — 통째로 교체하면 승계된 옛 productId 의 이름이 사라져
        # 이름 지목("파란 니트 담아줘")이 조용히 degrade 한다. 병합 후 capped 에 남은 id 로
        # 정리(prune)해 스레드당 dict 가 무한히 자라지 않게 한다(항목당 dict 를 담는
        # BoundedLRUCache 는 **엔트리 수**만 제한하지 엔트리 크기는 제한하지 않는다).
        # 빈 이름으로는 덮지 않는다 — 이번 턴 push 가 이름을 못 실어 온 상품(name_by_id 미스)이
        # 캐시에 있던 멀쩡한 이름을 지우면 이름 지목이 이유 없이 나빠진다.
        names = dict(_last_reco_names.get(key) or {})
        for pid, name in capped:
            if name or pid not in names:
                names[pid] = name
        kept = {pid for pid, _ in capped}
        _last_reco_names[key] = {pid: name for pid, name in names.items() if pid in kept}

    async def get_last_reco(self, key: str) -> list[tuple[int, str]]:
        """누적 추천 목록(최근 언급 순). 담기 가드가 쓰는 목록이다."""
        return (await self.get_last_reco_state(key)).items

    async def get_last_reco_state(self, key: str) -> LastReco:
        """누적 목록 + 이번 턴 경계를 **한 번의 조회로** 돌려준다.

        `turn_count` 가 저장돼 있지 않으면(구버전 인스턴스가 쓴 행) **전량을 이번 턴으로 간주**한다 —
        오늘 동작과 같아지는 쪽으로 degrade 하고, 롤링 배포 중 KeyError 로 담기 턴이 죽지 않게 한다.
        """
        item = await run_with_query_timeout(self._store.aget(self._ns(key), _LAST_RECO_KEY))
        if not item:
            return LastReco(items=[], turn_count=0)
        names = _last_reco_names.get(key, {})  # 재시작·타 인스턴스면 미스 → 이름 "" degrade
        items = [(pid, names.get(pid, "")) for pid in item.value["product_ids"]]
        raw_turn_count = item.value.get("turn_count")
        # bool 은 int 의 서브클래스라 명시적으로 뺀다(_coerce_positive_int 와 같은 규약). 범위를
        # 벗어난 값도 못 믿을 값이므로 전량-이번-턴 폴백으로 보낸다.
        usable = (
            isinstance(raw_turn_count, int)
            and not isinstance(raw_turn_count, bool)
            and 0 <= raw_turn_count <= len(items)
        )
        turn_count = raw_turn_count if usable else len(items)
        return LastReco(items=items, turn_count=turn_count)

    async def set_pending(self, key: str, pending: PendingAdd) -> None:
        await run_with_query_timeout(
            self._store.aput(
                self._ns(key),
                _PENDING_KEY,
                {
                    "product_id": pending.product_id,
                    "quantity": pending.quantity,
                    "options": [o.model_dump() for o in pending.options],
                    "attempts": pending.attempts,
                },
            )
        )

    async def get_pending(self, key: str) -> PendingAdd | None:
        item = await run_with_query_timeout(self._store.aget(self._ns(key), _PENDING_KEY))
        if not item:
            return None
        value = item.value
        return PendingAdd(
            product_id=value["product_id"],
            quantity=value["quantity"],
            options=[CartOption.model_validate(o) for o in value["options"]],
            attempts=value["attempts"],
        )

    async def clear_pending(self, key: str) -> None:
        await run_with_query_timeout(self._store.adelete(self._ns(key), _PENDING_KEY))


async def get_cart_store() -> CartStateStore:
    """장바구니 상태 스토어 — pg-profile 공유 연결 백엔드(요청마다 얇은 래퍼 재생성)."""
    return CartStateStore(await pg_store.get_store())


def reset_cart_store() -> None:
    """테스트 격리용 — 공유 pg-profile store(InMemoryStore)와 휘발성 이름 캐시를 비운다."""
    global _last_reco_names
    pg_store.reset_store()
    _last_reco_names = BoundedLRUCache(
        max_entries=get_settings().state_store_local_cache_max_entries
    )
