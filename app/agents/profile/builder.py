"""프로필 빌더 (스텁, SPEC-PROFILE-001, 결정 4-A).

2단 비동기 쓰기:
  (1) 세션 종료 트리거 시 LLM 이 후보 델타 생성 (transient 게이트 적용)
  (2) 유휴시간(sleep-time) 배치에서 위키 병합·중복 제거·모순 해소(recency-wins)
턴 중에는 write 하지 않고 세션 버퍼만 누적한다.

TODO(SPEC-PROFILE-001): 델타 생성 노드, sleep-time consolidation 배치,
미처리 스레드 스캔(checkpointer 기반) 회수 경로(REQ-PROF-050/051).
"""

from __future__ import annotations


def generate_session_delta(user_id: str, thread_id: str) -> None:
    """세션 종료 시 후보 델타 생성 (스텁)."""
    raise NotImplementedError("profile builder not implemented yet (SPEC-PROFILE-001)")


def consolidate(user_id: str) -> None:
    """sleep-time 위키 병합/중복 제거/모순 해소 (스텁)."""
    raise NotImplementedError("profile consolidation not implemented yet (SPEC-PROFILE-001)")
