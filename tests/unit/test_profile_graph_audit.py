"""변경 감사 — 지문만 남기고 원문은 남기지 않는다 (#358, REQ-PGRAPH-080~082).

이 파일의 핵심은 마지막 테스트다: **레코드의 모든 값을 순회해 원문이 없음을 단언**한다.
필드를 하나씩 검사하는 방식은 새 필드가 추가될 때 조용히 통과하는데, 감사 레코드는
"나중에 필드가 하나 더 붙는" 일이 실제로 일어나는 자리다(#359·#360 이 뒤에 온다).

감사에 남기는 것은 *무엇을* 지웠는지가 아니라 *언제·누가(지문)* 지웠는지다 — 전체 초기화가
모든 개인화 데이터를 지워도 이 테이블만은 남기 때문에(REQ-PGRAPH-062), 여기에 원문이 있으면
"삭제했다"는 약속 자체가 거짓이 된다.
"""

from __future__ import annotations

import pytest

from app.agents.profile import graph_journal
from app.core.config import get_settings

USER_ID = 358
LABEL = "소니"


@pytest.fixture(autouse=True)
def _reset_journal():
    graph_journal.reset()
    yield
    graph_journal.reset()


async def test_audit_row_carries_fingerprints_not_raw_values() -> None:
    """주체·대상은 지문으로, predicate 만 원문으로 남는다 (§5.4 GraphAuditRecord)."""
    await graph_journal.record_audit(
        user_id=USER_ID,
        request_id="req-1",
        action="edgeUpdate",
        graph_version_before="g42",
        graph_version_after="g43",
        edge_id_before="e_old",
        edge_id_after="e_new",
        predicate="avoids",
        object_label=LABEL,
    )

    (row,) = await graph_journal.list_audit(user_id=USER_ID)

    assert row.action == "edgeUpdate"
    assert row.predicate == "avoids"  # 고정 enum 이라 원문 기록 가능
    assert row.edge_id_before == "e_old"
    assert row.edge_id_after == "e_new"
    assert row.graph_version_before == "g42"
    assert row.graph_version_after == "g43"
    # 지문은 되돌릴 수 없고, 같은 입력이면 같은 값이라 사후 대조는 가능하다.
    assert row.actor_fp and row.actor_fp != str(USER_ID)
    assert row.object_fp and row.object_fp != LABEL


async def test_no_column_contains_the_raw_user_id_or_label() -> None:
    """**전 컬럼 순회** — 새 필드가 붙어도 원문 유출이 조용히 통과하지 못하게 한다.

    REQ-PGRAPH-081 [HARD]. 필드별 단언은 필드가 늘 때 커버리지가 조용히 새는데, 이 레코드는
    #359·#360 이 뒤에 오는 자리라 실제로 늘어난다.
    """
    await graph_journal.record_audit(
        user_id=USER_ID,
        request_id="req-1",
        action="edgeDelete",
        graph_version_before="g42",
        graph_version_after="g43",
        edge_id_before="e_old",
        predicate="prefers",
        object_label=LABEL,
    )

    (row,) = await graph_journal.list_audit(user_id=USER_ID)

    rendered = " ".join(str(value) for value in vars(row).values())
    assert LABEL not in rendered
    assert str(USER_ID) not in rendered


async def test_same_input_yields_the_same_fingerprint() -> None:
    """지문이 안정적이어야 "이 사용자가 지운 적 있나"를 사후에 대조할 수 있다."""
    for request_id in ("req-1", "req-2"):
        await graph_journal.record_audit(
            user_id=USER_ID,
            request_id=request_id,
            action="edgeDelete",
            graph_version_before="g42",
            graph_version_after="g43",
            object_label=LABEL,
        )

    first, second = await graph_journal.list_audit(user_id=USER_ID)

    assert first.actor_fp == second.actor_fp
    assert first.object_fp == second.object_fp


async def test_missing_label_leaves_the_object_fingerprint_empty() -> None:
    """대상 없는 변경(전체 초기화·중지 토글)은 `object_fp` 가 없다 — 빈 문자열을 지문화하지 않는다.

    빈 값을 지문화하면 "대상 없음"과 "라벨이 빈 문자열인 대상"이 같은 값이 되어, 감사에서
    둘을 구분할 수 없다.
    """
    await graph_journal.record_audit(
        user_id=USER_ID,
        request_id="req-1",
        action="graphReset",
        graph_version_before="g42",
        graph_version_after="g43",
    )

    (row,) = await graph_journal.list_audit(user_id=USER_ID)

    assert row.object_fp is None
    assert row.predicate is None


async def test_unknown_action_is_rejected_before_it_reaches_the_database() -> None:
    """액션 어휘는 4종 — `edgeRestore` 는 #499 로 폐기됐다.

    DB CHECK 도 걸려 있지만(통합 테스트가 확인) 폴백 경로에는 DB 가 없다. 두 경로가 같은
    어휘를 강제해야 dev 에서 통과하고 운영에서 거부되는 일이 안 생긴다.
    """
    with pytest.raises(ValueError, match="edgeRestore"):
        await graph_journal.record_audit(
            user_id=USER_ID,
            request_id="req-1",
            action="edgeRestore",
            graph_version_before="g42",
            graph_version_after="g43",
        )


async def test_audit_survives_when_the_pepper_is_unset() -> None:
    """dev 는 pepper 가 비어 있을 수 있다 — 그래도 감사가 죽으면 안 된다.

    운영에서는 `config.py` 가 빈 pepper 로 기동하는 것을 막으므로(`_require_pepper_in_prod`)
    약한 지문이 실서비스로 새지 않는다. 여기서 재는 것은 dev 가 조용히 예외로 죽지 않는다는 것뿐.
    """
    assert get_settings().pii_hash_pepper == ""

    await graph_journal.record_audit(
        user_id=USER_ID,
        request_id="req-1",
        action="personalizationToggle",
        graph_version_before="g42",
        graph_version_after="g42",
    )

    (row,) = await graph_journal.list_audit(user_id=USER_ID)
    assert row.actor_fp
