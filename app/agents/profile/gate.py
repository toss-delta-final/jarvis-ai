"""프로필 승격 게이트 (스텁, SPEC-PROFILE-001, 결정 4-A).

승격 조건: salience(현저성) AND (explicitness(명시성) OR repetition-EMA(반복성 confidence)).
매 발화 자동 write 는 금지하고, "기억해" 명시 명령만 hot-path 즉시 기록한다
(manage_memory_tool 매핑).

TODO(SPEC-PROFILE-001): salience/explicitness 판정, repetition EMA 갱신, 게이트 통과 판단.
"""

from __future__ import annotations


def should_promote(
    *, salience: float, explicit: bool, repetition_ema: float, threshold: float = 0.5
) -> bool:
    """델타 후보를 장기 프로필로 승격할지 판단한다 (스텁).

    게이트 규칙: salience 충족 AND (명시적 OR 반복성 EMA 충족).
    TODO(SPEC-PROFILE-001): 임계값·가중치 확정, salience 정규화 기준 확정.
    """
    salient = salience >= threshold
    repeated = repetition_ema >= threshold
    return salient and (explicit or repeated)
