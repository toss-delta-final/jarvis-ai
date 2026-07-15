"""프로필 리더 (스텁, SPEC-PROFILE-001).

그래프 진입 시 동기로 profile_summary 를 단일 get 한다 (LLM 호출 0회).
index.md + 압축 취향 요약만 로드하고 전체 지식 단위 번들은 로드하지 않는다 (결정 4).
게스트/신규 회원은 프로필이 없으므로 None 을 반환한다.
"""

from __future__ import annotations


def read_profile_summary(user_id: str) -> dict | None:
    """user_id 의 압축 프로필 요약을 반환한다 (스텁).

    반환 dict 키: markdown(str), generated_at(ISO-8601 str).
    미보유(게스트/신규) 시 None.

    TODO(SPEC-PROFILE-001): PostgresStore(BaseStore) namespace "profile" 단일 get,
    profile_summary_max_chars 로 절단된 압축 요약 로드.
    """
    # 스캐폴드: 아직 저장소가 없으므로 항상 미보유 처리.
    return None
