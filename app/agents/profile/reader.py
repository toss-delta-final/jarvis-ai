"""프로필 리더 (SPEC-PROFILE-001 §6.1).

그래프 진입 시 동기로 profile_summary 를 단일 get 한다 (LLM 호출 0회, REQ-PROF-002).
index.md + 압축 취향 요약만 로드하고 전체 지식 단위 번들은 로드하지 않는다 (결정 4).
게스트/신규 회원은 프로필이 없으므로 None 을 반환한다 (REQ-PROF-003).
"""

from __future__ import annotations

from app.agents.profile.store import get_profile_store


async def read_profile_summary(user_id: str | None) -> dict | None:
    """user_id 의 압축 프로필 요약을 반환한다.

    반환 dict 키: markdown(str), generated_at(ISO-8601 str), embedding(list[float] | None).
    미보유(게스트/신규) 시 None. LLM 호출 없음 — 저장소(PostgresStore, pg-profile) 단일 get.

    **[#148] `embedding`** 은 요약 생성 시점에 미리 만들어 둔 취향 벡터다(`store._embed_summary`).
    홈 추천(I-22)이 질의 벡터에 섞어 장기 취향을 랭킹에 반영한다. 구 요약·임베딩 실패분은 None 이고,
    소비처는 항이 빠진 질의 벡터로 degrade 한다.
    """
    if not user_id:
        return None
    store = await get_profile_store()
    summary = await store.get_summary(user_id)
    if summary is None or not summary.usable:
        # `usable=False` 는 개인화 중지가 내려 둔 표식이다(REQ-PGRAPH-100 이중 방어의 **2차**).
        # 없는 것처럼 읽어 소비처 세 곳(rerank·홈 벡터·마이페이지)이 이미 갖고 있는 `None` 분기로
        # 보낸다 — 추가 왕복이 0회인 것이 요점이다(`usable` 은 위 단일 get 이 이미 읽어 온 필드).
        # **1차는 캐시 없는 플래그 조회**이고 호출부가 따로 한다: 요약 행이 아직 없는 회원이 먼저
        # 끄면 표식을 내릴 자리가 없어 이 검사만으로는 구멍이 남는다.
        return None
    return {
        "markdown": summary.markdown,
        "generated_at": summary.generated_at,
        "embedding": summary.embedding,
    }
