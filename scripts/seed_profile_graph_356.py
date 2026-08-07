"""#356 개인화 그래프 시드 — 발표·수동 검증용 계정 만들기.

그래프는 **다음 배치부터 누적**되고 과거 fact 로는 복원할 수 없다(SPEC-PROFILE-GRAPH-149 §7.3):
델타 추출이 낸 현저성·명시성·반복 신호는 승격 판정 직후 버려지고 저장소에는 fact 문자열과 생성
시각만 남기 때문이다. 게다가 게이트를 통과한 fact 만 트리플이 되므로 **즉석 대화 몇 마디로는
노드가 모이지 않는다.** 그래서 시연·수동 검증에는 미리 채운 계정이 필요하다.

이 스크립트는 흉내를 내지 않는다 — 운영 코드(`resolver.resolve_triple` · `graph_merge` ·
`ProfileStore`)를 그대로 호출한다. 시드가 만든 문서와 운영 배치가 만드는 문서가 같은 함수에서
나와야 "시드로 확인한 동작"이 의미를 갖는다(docs/lessons.md 2026-07-31 시드 덤프 재생산 항목).

전제
    docker compose up -d pg-profile           # 그래프·fact 저장소
    docker compose up -d pg-catalog           # category kind 어휘 스냅(없으면 category 노드만 드롭)
    GOOGLE_API_KEY 구성                       # **필수** — 아래 참조
    uv run python scripts/seed_profile_graph_356.py --user-id 7

    ⚠️ 워크트리에서 `docker compose ps` 가 비어 있어도 컨테이너가 떠 있을 수 있다 — compose
    프로젝트 이름이 디렉터리마다 달라서 메인 체크아웃에서 띄운 pg-profile 은 여기 목록에
    안 잡힌다. `docker ps` 로 확인하라. "DB 가 없으니 InMemory 폴백이겠지"라고 가정하면
    실제로는 실 DB 에 쓰게 된다.

비용
    임베딩 API 를 두 군데서 호출한다:
      1. **fact 적재마다 1회** — `add_fact` 가 store 의 semantic 인덱스(`fields: ["fact"]`,
         REQ-PROF-070/071)를 타기 때문이다. 그래서 실 pg-profile 을 쓰는 한 GOOGLE_API_KEY 는
         선택이 아니라 **필수**다(없으면 EmbeddingError 로 적재 자체가 실패한다).
      2. category kind 의 어휘 스냅 — 앵커 1건당 1회, 기본 시나리오에 2건.
    LLM 은 호출하지 않는다 — 델타 추출을 건너뛰고 이미 구조화된 제안을 직접 넣는다(게이트는
    시나리오가 명시적 고현저성이라 어차피 전부 통과한다).
    키 없이 배선만 확인하려면 `--dry-run` 을 쓴다(저장하지 않으므로 인덱스도 안 탄다).

멱등
    fact dedup 이 문자열 완전 일치라 같은 시나리오를 두 번 넣어도 fact 는 늘지 않는다.
    그래프는 매번 전량 재계산이므로 2회 실행 결과가 같아야 한다(--verify-idempotent 로 확인).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# [이슈 #33] Windows 기본 ProactorEventLoop 는 psycopg async 연결을 지원하지 않아
# AsyncPostgresStore 가 조용히 InMemoryStore 로 전락한다 — 그러면 이 스크립트가 아무것도
# 저장하지 않고 성공한 것처럼 끝난다. 이벤트 루프가 만들어지기 전에 정책을 바꾼다.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.agents.profile.graph_merge import build_graph_document, empty_document  # noqa: E402
from app.agents.profile.resolver import resolve_triple  # noqa: E402
from app.agents.profile.store import get_profile_store  # noqa: E402
from app.core.config import get_settings  # noqa: E402

# (fact, kind, label, anchorPhrase, polarity, predicateHint, salience)
# 게이트 통과를 전제로 salience 를 높게 둔다 — 시드의 목적은 게이트 판정 재현이 아니라
# "그래프가 찬 계정"이다. 게이트 자체는 tests/unit/test_profile.py 가 검증한다.
_SCENARIO: list[tuple[str, str, str, str, str, str, float]] = [
    ("소니 브랜드를 선호한다", "brand", "소니", "소니 이어폰이 좋더라", "positive", "likes", 0.9),
    ("애플 브랜드를 회피한다", "brand", "애플", "애플은 좀 별로야", "negative", "avoids", 0.8),
    (
        "3~5만원 가격대를 선호한다",
        "priceBand",
        "30000-50000",
        "3만원에서 5만원 사이로",
        "positive",
        "prefers",
        0.9,
    ),
    (
        "평점 4점 이상을 선호한다",
        "ratingBand",
        "4-5",
        "평점 4점 넘는 걸로",
        "positive",
        "prefers",
        0.85,
    ),
    (
        "노이즈캔슬링을 중요하게 본다",
        "attribute",
        "노이즈캔슬링",
        "노이즈캔슬링 없으면 안 써",
        "positive",
        "prefers",
        0.95,
    ),
    (
        "블루투스 이어폰에 관심이 있다",
        "category",
        "블루투스 이어폰",
        "블루투스 이어폰 찾고 있어",
        "positive",
        "likes",
        0.9,
    ),
    (
        "키보드에 관심이 있다",
        "category",
        "키보드",
        "기계식 키보드도 하나 볼까",
        "positive",
        "likes",
        0.7,
    ),
    # 트리플이 안 붙는 fact — unprojected_count 가 0이 아닌 상태를 만든다(REQ-PGRAPH-004).
    # "기억해" hot-path 로 들어온 발화가 실제로 이 모양이다.
    ("매운 음식을 싫어한다고 기억해줘", "", "", "", "positive", "", 0.9),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def seed(user_id: str, *, dry_run: bool) -> dict:
    """시나리오를 fact + 트리플로 적재하고 그래프를 산출한다. 요약(LLM)은 만들지 않는다."""
    settings = get_settings()
    store = await get_profile_store()
    now = _now_iso()

    resolved_count = 0
    for fact, kind, label, anchor, polarity, hint, salience in _SCENARIO:
        triples: list[dict] = []
        if kind:
            resolved = await resolve_triple(
                kind=kind,
                label=label,
                anchor_phrase=anchor,
                polarity=polarity,
                predicate_hint=hint,
                settings=settings,
                now=now,
            )
            if resolved is not None:
                triples = [resolved.as_payload(salience=salience, source="conversation")]
                resolved_count += 1
            else:
                print(f"  [드롭] {kind}:{label} — 어휘 스냅 실패 또는 파싱 불가")
        if not dry_run:
            await store.add_fact(
                user_id, fact, cap=settings.profile_max_facts, graph_triples=triples
            )

    if dry_run:
        return {"resolved": resolved_count, "dry_run": True}

    async with store.graph_lock(user_id):
        facts = await store.get_fact_records(user_id)
        existing = await store.get_graph(user_id) or empty_document(now)
        document = build_graph_document(facts, existing=existing, settings=settings, now=now)
        await store.set_graph(user_id, document)

    return {
        "resolved": resolved_count,
        "facts": len(facts),
        "revision": document.revision,
        "nodes": len(document.nodes),
        "edges": len(document.edges),
        "promoted": sum(1 for e in document.edges if e.promoted),
        "verified_nodes": sum(1 for n in document.nodes if n.verified),
        "unprojected": document.unprojected_count,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="#356 개인화 그래프 시드")
    parser.add_argument("--user-id", required=True, help="회원 id(JWT sub 와 같은 값)")
    parser.add_argument(
        "--dry-run", action="store_true", help="resolve 만 하고 저장하지 않는다(어휘 스냅 확인용)"
    )
    parser.add_argument(
        "--verify-idempotent",
        action="store_true",
        help="두 번 실행해 그래프가 같은지 확인한다(재생 동일성)",
    )
    args = parser.parse_args()

    print(f"[시드] user_id={args.user_id} dry_run={args.dry_run}")
    result = await seed(args.user_id, dry_run=args.dry_run)
    print(f"[결과] {result}")

    if args.verify_idempotent and not args.dry_run:
        store = await get_profile_store()
        before = await store.get_graph(args.user_id)
        await seed(args.user_id, dry_run=False)
        after = await store.get_graph(args.user_id)
        assert before is not None and after is not None
        # decay_evaluated_at·updated_at 은 배치마다 바뀌는 시계라 비교에서 뺀다.
        same = before.revision == after.revision and [e.edge_key for e in before.edges] == [
            e.edge_key for e in after.edges
        ]
        print(f"[멱등] revision·edge 집합 동일: {same}")
        if not same:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
