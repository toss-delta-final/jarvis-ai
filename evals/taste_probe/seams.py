"""dry-run 전용 모듈 패치 시임 — 최후 수단이다 (#462).

`resolver._resolve_category` 는 임베딩·pg 콜백을 인자로 받지만, 그 위 호출부인
`builder._resolve_delta` 는 `resolve_triple` 을 호출할 때 그 주입 인자를 넘기지 않는다
(builder.py 를 고치는 것은 이 이슈 범위 밖 — app/ 수정 금지, §1). 그래서 `category` kind 는
`--dry-run`·유닛 테스트에서도 실 임베딩·실 pg 를 타게 된다.

그 경로를 API·pg 콜 0 으로 막는 유일한 방법은 **모듈 속성 패치**뿐이다. `resolver._resolve_category`
가 함수 본문 안에서 `from app.pipelines.category_search import exact_lookup, search_categories_pg`
/ `from app.pipelines.embedding import embed_texts` 를 **호출 시점에** 지연 import 하므로(모듈
로드 시가 아니라), 그 모듈 속성을 미리 패치해 두면 호출 시점의 지연 import 가 패치된 객체를
그대로 읽는다.

이 선택은 프로덕션에 주입점을 뚫는 것보다 하네스 쪽에서 감당하는 쪽을 택한 것이다 — 라이브 런
(`--dry-run` 없이)은 이 시임을 **전혀 쓰지 않는다.** dry-run·유닛 테스트 전용이다.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Protocol
from unittest.mock import patch


class FakeCatalogProtocol(Protocol):
    def exact(self, values: list[str], dsn: str) -> set[str]: ...
    def search(self, vec: list[float], dsn: str, *, k: int) -> list[tuple[str, float]]: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@contextmanager
def fake_catalog_seam(catalog: FakeCatalogProtocol) -> Generator[None]:
    """`category` kind resolve 경로의 임베딩·pg 콜을 결정론 가짜로 임시 교체한다.

    **라이브 런에서는 절대 쓰지 않는다** — dry-run(`cli.py --dry-run`)과 유닛 테스트
    (`tests/unit/test_taste_probe_runner.py`) 전용이다. `try/finally` 는 `patch(...)` 컨텍스트
    매니저 자체가 보장하므로(예외가 나도 원복) 별도 처리를 두지 않는다.
    """
    with (
        patch("app.pipelines.category_search.exact_lookup", catalog.exact),
        patch("app.pipelines.category_search.search_categories_pg", catalog.search),
        patch("app.pipelines.embedding.embed_texts", catalog.embed),
    ):
        yield


EmbedFn = Callable[[list[str]], list[list[float]]]
SearchFn = Callable[..., list[tuple[str, float]]]
ExactFn = Callable[..., set[str]]
