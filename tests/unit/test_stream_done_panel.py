"""app/core/stream.py `_done_stop_frame` role별 payload 검증 (이슈 #621 ③).

총 상한(90s) 절단 시 판매자 스트림에서 `panel` 신호가 유실되던 문제 — 판매자
`_done(panel=...)`(app/api/seller.py)는 정상 종료에서만 panel 을 실었고, 절단
경로(stream.py 단독)는 role 을 몰라 항상 구매자용 DoneData 만 만들었다. FE 계약
(Notion S-4)의 절단 done 은 confirm 이 잘리면 `panel:"refresh"` 재조회 신호가
사라져, 쓰기가 반영된 뒤에도 우측 목록이 옛 값으로 남는다.
"""

from __future__ import annotations

import json

from app.core import stream


def _parse(frame: str) -> dict:
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    return json.loads(frame[len("data: ") : -2])


def test_done_stop_frame_seller_includes_panel_keep() -> None:
    """role="seller" 는 finishReason=stop + panel=keep 을 낸다 — 구매자 DoneData 스키마는
    건드리지 않는다(app/api/seller.py::_done() 과 동일한 판매자 전용 payload 구성)."""
    payload = _parse(stream._done_stop_frame(role="seller"))

    assert payload == {
        "type": "done",
        "data": {"finishReason": "stop", "panel": "keep"},
    }


def test_done_stop_frame_buyer_omits_panel() -> None:
    """role="buyer" 는 기존 구매자 DoneData 그대로 — panel 필드가 없다(계약 무변경)."""
    payload = _parse(stream._done_stop_frame(role="buyer"))

    assert payload == {"type": "done", "data": {"finishReason": "stop"}}
    assert "panel" not in payload["data"]


def test_done_stop_frame_default_role_matches_buyer() -> None:
    """role 미지정은 기존 동작(구매자 형태) 그대로 유지한다 — 호출부 하위호환."""
    assert _parse(stream._done_stop_frame()) == _parse(stream._done_stop_frame(role="buyer"))
