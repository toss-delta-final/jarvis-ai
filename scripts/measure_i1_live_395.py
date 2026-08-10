"""#395 §11 배포 후 라이브 검증 재현 스크립트.

`docs/specs/PROPOSAL-I1-DIET-395.md` §11("배포 후 라이브 검증")과 `docs/api-spec.md` §4.6
`[#395 종결]` 불릿이 인용하는 2026-08-10 운영 실측을 재현한다. BE I-1(`GET
/internal/products/search`)을 **프로덕션과 동일한 httpx 경로**(`app.services.spring_client.
_client`)로 호출한다 — `urllib`로 직접 호출하면 403이 난다(2026-08-10 운영 실측 중 확인).

이 스크립트는 **BE가 떠 있어야 도는 실측 도구**다(네트워크 호출). pytest 수집 대상이 아니며
(`pyproject.toml` `testpaths = ["tests"]`), DB·LLM은 쓰지 않는다.

사용법
    uv run python scripts/measure_i1_live_395.py                      # 무필터(최악)
    uv run python scripts/measure_i1_live_395.py --keyword 셔츠 --repeat 3
    uv run python scripts/measure_i1_live_395.py --repeat 2            # productId 순서 동일 여부 확인

[HARD] 시크릿(내부 토큰·base URL 전문)을 절대 출력하지 않는다 — 토큰은 설정에서 꺼내 헤더로만
쓰고, 로그에는 `token_set: True/False` 수준만 남긴다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from app.core.config import get_settings
from app.services.spring_client import _client

# [§11] AI 가 소비하지 않는 attributes 내부 키(#395) — 실측에서 4키 잔존 항목 수를 센다.
_DIET_ATTR_KEYS = ("_extra", "_source_pid", "_domain", "_category")


def _item_bytes(item: dict) -> int:
    """항목 1건의 근사 바이트 수 — `json.dumps` 기본 구분자(`, `/`: `) 기준이라 실제 와이어보다
    약간 크다(`measure_i1_field_bytes_395`와 동일 규약이므로 비교는 유효하다)."""
    return len(json.dumps(item, ensure_ascii=False).encode("utf-8"))


def _fmt_bytes(n: float) -> str:
    return f"{n / 1024 / 1024:.3f} MiB" if n >= 1024 * 1024 else f"{n / 1024:.1f} KiB"


def _top_field_bytes(items: list[dict]) -> list[tuple[str, int]]:
    """최상위 필드별 바이트 기여(값만 — 필드 delta 가 아니라 각 항목에서 그 필드 값만 뽑아 합산)."""
    totals: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            totals[key] = totals.get(key, 0) + len(
                json.dumps(value, ensure_ascii=False).encode("utf-8")
            )
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def _attr_key_bytes(items: list[dict], top_n: int) -> list[tuple[str, int]]:
    """attributes 내부 키별 바이트 기여 상위 N."""
    totals: dict[str, int] = {}
    for item in items:
        attrs = item.get("attributes")
        if not isinstance(attrs, dict):
            continue
        for key, value in attrs.items():
            totals[key] = totals.get(key, 0) + len(
                json.dumps(value, ensure_ascii=False).encode("utf-8")
            )
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top_n]


def _diet_key_remaining_count(items: list[dict]) -> dict[str, int]:
    """`_extra`·`_source_pid`·`_domain`·`_category` 각 키가 남아 있는 항목 수."""
    counts: dict[str, int] = dict.fromkeys(_DIET_ATTR_KEYS, 0)
    for item in items:
        attrs = item.get("attributes")
        if not isinstance(attrs, dict):
            continue
        for key in _DIET_ATTR_KEYS:
            if key in attrs:
                counts[key] += 1
    return counts


async def _fetch_once(params: dict, timeout: float) -> tuple[list[dict], int, float]:
    """I-1 을 1회 호출해 (항목 목록, HTTP 바디 바이트, 소요 초)를 돌려준다."""
    async with _client(timeout=timeout) as client:
        t0 = time.perf_counter()
        resp = await client.get("/internal/products/search", params=params)
        body = resp.content
        elapsed = time.perf_counter() - t0
        resp.raise_for_status()
        payload = json.loads(body)
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            items = []
        return items, len(body), elapsed


def _print_report(items: list[dict], body_bytes: int, top_n: int) -> None:
    n = len(items)
    print(f"항목 수: {n:,}")
    print(f"HTTP 바디: {body_bytes:,} B ({_fmt_bytes(body_bytes)})")
    if n == 0:
        return
    per_item = [_item_bytes(it) for it in items]
    print(
        f"항목당 바이트: 평균 {sum(per_item) / n:,.1f} / 최소 {min(per_item):,} / 최대 {max(per_item):,}"
    )

    print("\n최상위 필드별 바이트(값만)")
    print(f"{'field':<16} {'bytes':>14}")
    print("-" * 32)
    for key, nbytes in _top_field_bytes(items):
        print(f"{key:<16} {nbytes:>14,}")

    print(f"\nattributes 내부 키 상위 {top_n}")
    print(f"{'key':<16} {'bytes':>14}")
    print("-" * 32)
    for key, nbytes in _attr_key_bytes(items, top_n):
        print(f"{key:<16} {nbytes:>14,}")

    remaining = _diet_key_remaining_count(items)
    print("\n#395 다이어트 4키 잔존 항목 수(0 이어야 배포 확인)")
    for key, count in remaining.items():
        print(f"  {key}: {count} / {n}")


async def _main_async(args: argparse.Namespace) -> None:
    settings = get_settings()
    print(f"token_set: {bool(settings.internal_api_token)}")

    params: dict[str, object] = {}
    if args.keyword:
        params["keyword"] = args.keyword

    durations: list[float] = []
    all_items: list[list[dict]] = []
    body_bytes = 0
    for i in range(args.repeat):
        items, nbytes, elapsed = await _fetch_once(params, args.timeout)
        durations.append(elapsed)
        all_items.append(items)
        body_bytes = nbytes  # 리포트는 마지막 응답 기준(직전 호출들과 크기는 사실상 동일)
        print(f"회차 {i + 1}/{args.repeat}: {elapsed:.3f}s, {nbytes:,} B, {len(items):,}건")

    print(f"\n소요시간: {[f'{d:.3f}' for d in durations]}")
    print(f"중앙값 {statistics.median(durations):.3f}s / 최댓값 {max(durations):.3f}s")

    if args.repeat >= 2:
        id_sequences = [[it.get("productId") for it in items] for items in all_items]
        identical = all(seq == id_sequences[0] for seq in id_sequences)
        print(f"\nproductId 순서 동일 여부(회차간): {identical}")

    print()
    _print_report(all_items[-1], body_bytes, args.top_n)


def main() -> None:
    parser = argparse.ArgumentParser(description="I-1 배포 후 라이브 검증 재현(#395 §11)")
    parser.add_argument("--keyword", default=None, help="검색 키워드(기본 없음=무필터)")
    parser.add_argument("--repeat", type=int, default=1, help="반복 호출 횟수(기본 1)")
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="httpx 타임아웃 초(기본 120.0)"
    )
    parser.add_argument("--top-n", type=int, default=10, help="attributes 내부 키 상위 N(기본 10)")
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
