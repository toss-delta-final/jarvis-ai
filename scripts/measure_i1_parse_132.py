"""#132 — I-1 전량반환 응답의 AI측 수신·파싱 비용 실측 (Spring 불필요).

BE I-1 은 `size` 제거(api-spec §4.6, 2026-07-27 BE 합의)로 매칭 전량을 반환한다. 그 응답을 AI 가
받아 `resp.json()` + N× `SpringProduct.model_validate` 로 푸는 비용에는 **개수 상한도 시간 상한도
없고, 둘 다 이벤트루프 위에서 돈다**(`spring_client.search_products`). 그래서 한 요청의 파싱이
같은 워커의 **다른 모든 SSE 스트림**을 멈춰 세운다 — 이 스크립트가 재는 것이 그 정지 시간이다.

실 카탈로그(7,220건)가 못 만드는 규모까지 곡선을 그리려고 응답을 합성한다. `httpx.MockTransport`
로 **HTTP 경계에서만** 대역을 세우므로(tests/integration/conftest.py 와 같은 규약) 파싱 경로는
프로덕션 코드 그대로다 — 스크립트가 파싱을 흉내내면 그 흉내가 틀렸을 때 측정이 무의미해진다.

전제
    uv run python scripts/measure_i1_parse_132.py [--concurrency 1,5,20]

LLM·DB·네트워크를 일절 쓰지 않는다 — 비용 없이 상시 재실행할 수 있다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import time

import httpx

import app.services.spring_client as spring_client
from app.schemas.spring import ProductSearchFilters

# 실 카탈로그(7,220)를 가운데 두고 아래위로 벌린다 — 캡이 아니라 곡선을 보려는 것이라
# "지금 규모"와 "카탈로그가 몇 배 커진 뒤"를 한 표에서 비교할 수 있어야 한다.
DEFAULT_SIZES = (30, 300, 1_000, 3_000, 7_245, 20_000)

# 항목 1건의 모양은 실 카탈로그 분포에 맞춘다 — attributes(dict)·options(list)·summary(장문)가
# 있어 검증 비용이 스칼라만 있는 항목과 다르다. 여기를 가볍게 잡으면 측정이 낙관 편향된다.
_ATTRS = {
    "색상": "네이비",
    "소재": "면 100%",
    "핏": "레귤러",
    "제조국": "대한민국",
    "세탁방법": "찬물 단독세탁",
    "패턴": "무지",
    "계절": "봄/가을",
    "_extra": {"visual_features": "차분한 톤의 베이직한 실루엣"},
}
_OPTIONS = ["S", "M", "L", "XL", "2XL"]


def _item(product_id: int) -> dict:
    """I-1 응답 항목 1건 — 와이어 포맷(camelCase) 그대로."""
    return {
        "productId": product_id,
        "name": f"베이직 코튼 티셔츠 {product_id}호 남녀공용 데일리",
        "summary": (
            "부드러운 코튼 소재로 사계절 편하게 입기 좋은 기본 티셔츠입니다. "
            "군더더기 없는 실루엣이라 어디에나 어울립니다."
        ),
        "attributes": _ATTRS,
        "price": 19_900 + (product_id % 50_000),
        "rating": None if product_id % 7 == 0 else 3.5 + (product_id % 15) / 10,
        "reviewCount": product_id % 300,
        "options": _OPTIONS,
        "optionCount": len(_OPTIONS),
        "categoryName": "패션의류/잡화 > 남성의류 > 티셔츠",
        "brandName": f"브랜드{product_id % 500}",
    }


def _body(size: int) -> bytes:
    """BE `ApiResponse<List<...>>` 형태({success, data:[...]})의 합성 응답 바디."""
    payload = {"success": True, "data": [_item(i) for i in range(1, size + 1)]}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _install_transport(body: bytes) -> None:
    """`spring_client._client` 를 MockTransport 팩토리로 교체한다(HTTP 경계 대역).

    tests/integration/conftest.py 와 같은 규약 — spring_client 함수를 직접 patch 하면 재시도·span·
    예외 변환 같은 실제 경로를 건너뛰어 측정 대상이 사라진다.
    """

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://spring.measure",
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=body)),
        )

    spring_client._client = _factory  # noqa: SLF001 - 측정용 경계 교체(테스트 규약과 동일)


async def _heartbeat(stop: asyncio.Event, interval: float = 0.01) -> list[float]:
    """`interval` 주기 tick 이 얼마나 밀렸는지 — 이벤트루프가 막힌 시간의 직접 측정.

    루프가 막히면 `asyncio.sleep` 이 깨어나지 못해 그 초과분이 그대로 갭으로 남는다. 이 값이
    "같은 워커에 붙은 다른 사용자의 SSE 가 멈춰 있던 시간"이다.
    """
    gaps: list[float] = []
    prev = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(interval)
        now = time.perf_counter()
        # sleep 이 예정보다 살짝 일찍 깨면 음수가 나온다 — 지연이 아니므로 0 으로 접는다.
        gaps.append(max(0.0, now - prev - interval))
        prev = now
    return gaps


async def _measure(size: int, concurrency: int, body: bytes | None = None) -> dict:
    """size 건 응답을 concurrency 개 동시 수신·파싱하고 소요·루프갭을 잰다.

    `body` 를 주면 합성 대신 그 바이트를 쓴다(`--body-file`) — 실 BE 응답을 그대로 통과시켜
    합성 항목 모양이 실제와 다를 위험을 없앤다.
    """
    body = _body(size) if body is None else body
    _install_transport(body)
    filters = ProductSearchFilters(keyword="티셔츠")

    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(stop))
    await asyncio.sleep(0.05)  # 하트비트 정상화 구간(측정 전 워밍업)

    started = time.perf_counter()
    results = await asyncio.gather(
        *(spring_client.search_products(filters) for _ in range(concurrency))
    )
    elapsed = time.perf_counter() - started

    stop.set()
    gaps = await hb
    return {
        "size": size,
        "concurrency": concurrency,
        "bytes": len(body),
        "elapsed_ms": elapsed * 1000,
        "max_gap_ms": (max(gaps) if gaps else 0.0) * 1000,
        "p50_gap_ms": (statistics.median(gaps) if gaps else 0.0) * 1000,
        "parsed": sum(len(r.products) for r in results),
    }


async def _main(
    sizes: tuple[int, ...], concurrencies: tuple[int, ...], body: bytes | None = None
) -> None:
    # 표 머리글만 ASCII — Windows 콘솔(cp949)에서 한글이 깨져 수치 판독을 방해한다.
    print(f"{'N':>7} {'conc':>5} {'body':>10} {'elapsed_ms':>11} {'gap_p50':>9} {'gap_max':>9}")
    print("-" * 56)
    for concurrency in concurrencies:
        for size in sizes:
            row = await _measure(size, concurrency, body)
            mib = row["bytes"] / 1024 / 1024
            print(
                f"{row['size']:>7,} {row['concurrency']:>5} {mib:>9.2f}M "
                f"{row['elapsed_ms']:>11.1f} {row['p50_gap_ms']:>9.1f} {row['max_gap_ms']:>9.1f}"
            )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="I-1 전량반환 응답의 AI측 파싱 비용 실측(#132)")
    parser.add_argument(
        "--sizes",
        default=",".join(str(s) for s in DEFAULT_SIZES),
        help="응답 항목 수 스윕(쉼표 구분)",
    )
    parser.add_argument(
        "--concurrency",
        default="1",
        help="동시 호출 수(쉼표 구분) — fan-out 5 leg·완화 probe 20 동시를 재현하려면 1,5,20",
    )
    parser.add_argument(
        "--body-file",
        default=None,
        help="실 BE I-1 응답 바디 파일 — 주면 합성 대신 이걸 쓰고 --sizes 는 무시된다",
    )
    args = parser.parse_args()
    concurrencies = tuple(int(c) for c in args.concurrency.split(",") if c.strip())
    if args.body_file:
        body = pathlib.Path(args.body_file).read_bytes()
        # 표의 N 칸에는 실제 항목 수를 보여준다 — 바디가 무엇이었는지 표에서 바로 읽히게.
        items = len(json.loads(body).get("data") or [])
        asyncio.run(_main((items,), concurrencies, body))
        return
    sizes = tuple(int(s) for s in args.sizes.split(",") if s.strip())
    asyncio.run(_main(sizes, concurrencies))


if __name__ == "__main__":
    main()
