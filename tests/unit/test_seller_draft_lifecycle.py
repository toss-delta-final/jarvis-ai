"""이슈 #622 — draft 수명주기 단일 입구 아키텍처 테스트.

`draft_lifecycle.publish_draft`/`cancel_pending`가 `hitl.start_draft`·
`hitl.invalidate_draft`·`draft_session.save_pending`를 부르는 유일한 지점이어야
한다(draft_lifecycle 모듈독스트링). 이 셋을 밖에서 다시 직접 부르면 무효화·
checkpoint 저장·pending 갱신 순서가 어긋나 draft 가 동시 생존할 수 있다 —
#622 이전 실제로 이 조합이 두 번 사고를 냈다(모듈독스트링 ①·③).

`draft_session.load_pending`/`load_pending_state`/`clear_pending`은 이 제약의
대상이 아니다 — 대기 *조회*·*폐기*는 여러 소비처가 안전하게 직접 부를 수 있고
(모듈독스트링 "세 가지 소비처"), 실제로 `_confirm_stream`(api/seller.py)이 confirm
완료 후 정리 목적으로 `load_pending`/`clear_pending`을 직접 부른다. 오직 *발급*
경로(start_draft/invalidate_draft/save_pending)만 단일 입구로 강제한다.

소스 스캔 방식: AST 로 실제 호출(Call) 노드만 본다 — 문자열/정규식 비교라면 주석·
독스트링에 등장하는 예시 텍스트(예: draft_lifecycle.py 자신의 독스트링, api/seller.py
899행 주석의 "start_draft(...)")까지 걸려 오탐이 난다. 함수 정의(FunctionDef)는
Call 노드가 아니므로 자연히 스캔 대상에서 제외된다 — hitl.py/draft_session.py 안의
`async def start_draft(...)` 자체는 위반으로 잡히지 않는다.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"
_LIFECYCLE_FILE = _APP_ROOT / "agents" / "seller" / "draft_lifecycle.py"
_RESTRICTED = {"start_draft", "invalidate_draft", "save_pending"}


def _called_names(source: str) -> set[str]:
    """소스 안에서 실제로 호출된 함수/메서드 이름 집합 — `foo(...)`·`obj.foo(...)` 둘 다."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_restricted_lifecycle_calls_are_confined_to_draft_lifecycle() -> None:
    """app/ 전체(운영 코드)에서 이 셋은 draft_lifecycle.py 밖에서 호출되지 않는다."""
    violations: list[str] = []
    for path in _APP_ROOT.rglob("*.py"):
        if path == _LIFECYCLE_FILE:
            continue
        source = path.read_text(encoding="utf-8")
        called = _called_names(source) & _RESTRICTED
        if called:
            violations.append(f"{path.relative_to(_APP_ROOT.parent)}: {sorted(called)}")
    assert not violations, (
        "draft 수명주기 함수(start_draft/invalidate_draft/save_pending)는 "
        "app/agents/seller/draft_lifecycle.py 를 통해서만 호출되어야 합니다 — "
        "직접 호출하면 무효화·checkpoint 저장·pending 갱신 순서가 어긋나 draft 가 "
        f"동시 생존할 수 있습니다(draft_lifecycle 모듈독스트링 ①·③ 참조): {violations}"
    )


def test_draft_lifecycle_module_is_confirmed_as_the_single_entry_point() -> None:
    """양성 대조 — draft_lifecycle.py 는 실제로 셋을 전부 호출한다(스캐너가 살아있는지 확인).

    위 테스트가 두 파일을 혼동해 통과 상태로 죽어있는 게 아님을 보장한다 — 함수 이름이
    바뀌거나 draft_lifecycle.py 자체가 우회되면 이 테스트가 먼저 깨진다.
    """
    source = _LIFECYCLE_FILE.read_text(encoding="utf-8")
    called = _called_names(source)
    assert _RESTRICTED <= called
