"""개인화 중지 게이트 (SPEC-PROFILE-GRAPH-149 §6.6, REQ-PGRAPH-051/052/054, 이슈 #359).

#358 이 플래그 테이블과 읽기 함수를 깔았지만 **프로덕션 호출자가 0건**이었다. 이 모듈이 그
플래그를 소비 지점·수집 지점이 읽을 수 있는 모양으로 감싼다.

핵심은 **경로별 실패 정책**이다 — 조회가 실패했을 때 무엇을 돌려줄지는 호출부가 정한다.
같은 "판정 불가"라도 잃는 것이 경로마다 다르기 때문이다(아래 테스트 docstring 참조).
"""

import pytest

from app.agents.profile import graph_journal
from app.agents.profile.personalization_gate import personalization_enabled
from app.agents.profile.store import reset_profile_store


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


async def test_enabled_by_default_when_the_user_never_toggled() -> None:
    """플래그 행이 없으면 켜짐이다 — 개인화는 기본값이고 중지가 명시적 행위다."""
    assert await personalization_enabled("123", on_error=False) is True


async def test_reads_the_flag_after_the_user_disables_it() -> None:
    await graph_journal.set_personalization_flag(
        user_id=123, enabled=False, now="2026-08-10T00:00:00+00:00"
    )

    assert await personalization_enabled("123", on_error=False) is False


async def test_hot_path_gets_false_when_the_lookup_fails(monkeypatch) -> None:
    """hot-path·소비는 **fail-closed** — 조회가 실패하면 개인화하지 않는다.

    사용자가 명시적으로 껐는데 저장소 장애 동안 시스템이 몰래 재개하는 것이, 이 기능이 막으려는
    바로 그 상황이다. 반대 방향의 대가는 그 턴에 한해 개인화를 잃는 것뿐이고 **게스트와 동등하게
    안전 열화**한다 — 비대칭이 명확하다.
    """

    async def _boom(*, user_id: int) -> bool:
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(graph_journal, "get_personalization_flag", _boom)

    assert await personalization_enabled("123", on_error=False) is False


async def test_batch_gets_none_when_the_lookup_fails(monkeypatch) -> None:
    """배치는 **fail-unknown** — `None`(판정 불가)을 돌려줘 degrade/retry 로 보낸다.

    배치까지 `False` 로 두면 `builder.generate_session_delta` 가 "처리됨, 승격 0건"을 반환하고
    `finalizer` 가 그 뒤 `clear_session_ctx_upto` 까지 진행한다 — **DB 블립 한 번에 개인화가
    켜져 있는 사용자의 세션 버퍼가 영구 삭제**된다. 기존 계약상 `None` 이 "degrade, 버퍼 보존,
    RETRYABLE" 이라(`builder.generate_session_delta` docstring) 그 어휘를 그대로 쓴다.

    hot-path 는 잃는 것이 발화 1건이고 배치는 누적 버퍼 전체다 — 그래서 정책이 갈린다.
    """

    async def _boom(*, user_id: int) -> bool:
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(graph_journal, "get_personalization_flag", _boom)

    assert await personalization_enabled("123", on_error=None) is None


async def test_lookup_failure_is_observed_without_leaking_the_flag_state(
    monkeypatch, caplog
) -> None:
    """조회 **실패**만 관측치로 남긴다 — 중지 자체는 정상 동작이라 로그를 남기지 않는다.

    REQ-PGRAPH-054: 중지 여부가 어떤 요청의 실패로도 추론되지 않아야 한다. 중지에 degrade 어휘를
    붙이면 그 조항이 와이어가 아닌 **관측 계층에서** 깨진다. 반대로 조회 실패를 조용히 넘기면
    fail-closed 로 인한 개인화 손실이 영원히 안 보인다.
    """
    import logging

    async def _boom(*, user_id: int) -> bool:
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(graph_journal, "get_personalization_flag", _boom)

    with caplog.at_level(logging.WARNING, logger="app.agents.profile.personalization_gate"):
        await personalization_enabled("123", on_error=False)
    assert "personalization_flag_unavailable" in caplog.text

    caplog.clear()
    # 정상적으로 꺼진 사용자 — 아무 경고도 남기지 않는다.
    monkeypatch.undo()
    await graph_journal.set_personalization_flag(
        user_id=123, enabled=False, now="2026-08-10T00:00:00+00:00"
    )
    with caplog.at_level(logging.WARNING, logger="app.agents.profile.personalization_gate"):
        await personalization_enabled("123", on_error=False)
    assert caplog.text == ""


@pytest.mark.parametrize("user_id", [None, "", "guest-uuid-abc", "not-a-number"])
async def test_non_numeric_identity_is_treated_as_enabled(user_id) -> None:
    """숫자가 아닌 신원은 플래그 대상이 아니다 — 조회하지 않고 켜짐으로 본다.

    플래그 테이블의 PK 가 `bigint` 라 게스트(UUID 문자열)는 애초에 행을 가질 수 없다. 호출부
    (`buyer/graph.py` 의 `profile_eligible`)가 게스트를 이미 걸러 이 경로에 안 오지만, JWT `sub`
    는 값 형식을 검증하지 않으므로(`app/core/auth.py`) 회원 id 가 숫자 문자열이 아닐 수 있다.
    `int()` 가 그대로 터지면 500 이 된다 — `builder._bootstrap_document` 의 3분기 패턴을 따른다.
    """
    assert await personalization_enabled(user_id, on_error=False) is True


async def test_accepts_int_identity() -> None:
    """호출부에 따라 `int` 로 들어오기도 한다 — 문자열만 받는 시그니처는 곧 깨진다."""
    await graph_journal.set_personalization_flag(
        user_id=123, enabled=False, now="2026-08-10T00:00:00+00:00"
    )

    assert await personalization_enabled(123, on_error=False) is False
