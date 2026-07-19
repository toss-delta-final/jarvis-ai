"""판매자 챗 SSE 스모크 스크립트 (4-1c — 실 LLM 라우팅 수동 확인용).

로컬 서버(uv run uvicorn app.main:app --reload)를 띄운 뒤 실행한다:

    uv run python scripts/smoke_seller_chat.py "지난달 매출 어때?"
    uv run python scripts/smoke_seller_chat.py --all   # 대표 발화 셋 일괄

Spring 없이도 동작한다 — 도구 호출은 "Error:" degrade 로 수렴하고 스트림
계약(token/draft/done/error)만 확인하는 것이 목적이다. 라우팅 판정은 서버
로그의 "판매자 라우팅: <category>" 라인과 대조한다(SMOKE-SELLER-41.md 체크리스트).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

BASE_URL = "http://localhost:8000"

# Spring 주입 신원(메아리) 흉내 — dev(SERVICE_TOKEN 미설정)에선 토큰 헤더 불필요.
HEADERS = {"X-Seller-Id": "7", "X-Brand-Id": "3"}

# 대표 발화 셋 — (메시지, 기대 분기) 서버 로그와 대조한다.
CASES: list[tuple[str, str]] = [
    ("지난달 매출 어때?", "analysis"),
    ("전환율이 왜 떨어졌는지 분석해줘", "analysis"),
    ("감귤청 가격 12,900원으로 바꿔줘", "product"),
    ("신상품 등록해줘", "product"),
    ("장바구니 전환율이 뭐야?", "general"),
    ("안녕", "general"),
    ('{"action": "confirm", "draftId": "d-1"}', "(입구① confirm 안내 — 라우팅 없음)"),
    ("경쟁사 매출 알려줘", "(입구② scope 거절 — 라우팅 없음)"),
]


async def stream_chat(client: httpx.AsyncClient, message: str) -> list[str]:
    """한 메시지를 보내고 SSE 이벤트 타입 순서를 반환한다(본문은 콘솔 출력)."""
    body = {"sessionId": "smoke-1", "threadId": "smoke-t1", "message": message}
    types: list[str] = []
    async with client.stream(
        "POST", f"{BASE_URL}/seller/chat", json=body, headers=HEADERS, timeout=180
    ) as response:
        if response.status_code != 200:
            await response.aread()
            print(f"  !! HTTP {response.status_code}: {response.text}")
            return types
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: ") :])
            types.append(payload["type"])
            data = payload.get("data", {})
            text = data.get("text") or data.get("summary") or data.get("code") or ""
            print(f"  [{payload['type']}] {str(text)[:100]}")
    return types


async def main() -> None:
    parser = argparse.ArgumentParser(description="판매자 챗 SSE 스모크")
    parser.add_argument("message", nargs="?", help="보낼 메시지(생략 + --all 로 일괄)")
    parser.add_argument("--all", action="store_true", help="대표 발화 셋 일괄 실행")
    args = parser.parse_args()

    async with httpx.AsyncClient() as client:
        if args.all:
            for message, expected in CASES:
                print(f"\n=== {message!r} (기대: {expected})")
                types = await stream_chat(client, message)
                ok = types and types[-1] in ("done", "error")
                print(f"  → 이벤트: {types} {'OK' if ok else 'FAIL(종료 이벤트 없음)'}")
        elif args.message:
            types = await stream_chat(client, args.message)
            print(f"→ 이벤트: {types}")
        else:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
