"""#356 델타 프롬프트 전환 분포 비교 — OPEN-G8 리스크 계측.

타입 붙은 제안 필드를 추가하는 것은 **동작 중인 LLM 계약을 바꾸는 일**이고, 프롬프트 변경이
측정 가능한 회귀를 만든 전례가 있다(#198 rerank 지시 한 줄로 성공률 3/3 -> 1/3, #115 앵커 교체로
12건 중 11건 오분류). 유닛 테스트는 fake LLM 이 고정 JSON 을 돌려주므로 이 회귀를 **구조적으로**
잡지 못한다 — 실제로 재려면 실 LLM 을 불러야 한다.

이 프로브는 **정답 라벨을 요구하지 않는다.** 구·신 프롬프트에 같은 입력을 넣고 분포만 비교한다:
    - 승격률       게이트를 통과한 델타 / 전체 델타. 이게 움직이면 프로필 누적 속도가 통째로 바뀐다.
                   profile_gate_threshold 는 현행 분포에 맞춰진 값이라 분포가 이동하면 임계의
                   의미도 함께 변한다.
    - kind 분포    구조화 필드가 어느 축으로 쏠리는지. priceBand 가 0건이면 엄격 파서가 받을
                   라벨이 안 나온다는 뜻이라 밴드 노드가 영영 안 생긴다.
    - JSON 위반율  필드가 늘수록 스키마 위반 확률이 오르고, extract_json 실패는 그 세션 델타
                   **전량 소실**이다.
    - 델타 수/세션 과분해(없던 취향 생성)·누락(분류 안 되는 신호 폐기)의 1차 지표.

"대화 세션 -> 기대 트리플" 골든셋은 저장소에 없다(evals/goldenset 은 단발 질의 + 상품 순위
라벨이다). 그 구축은 별도 이슈이며, 여기서는 **라벨 없이 잴 수 있는 것만** 잰다.

전제
    설정된 provider 의 API key      # LLM_PROVIDER=openai 면 OPENAI_API_KEY, anthropic 이면
                                    # ANTHROPIC_API_KEY. 어느 쪽인지 하드코딩하지 않는다 —
                                    # get_llm() 이 settings.llm_provider 로 갈리므로 한쪽을
                                    # 단정하면 키를 엉뚱한 곳에 채우게 안내한다.
    uv run python scripts/probe_delta_prompt_356.py [--sessions 12] [--turns 3]

비용
    **실 LLM 을 호출한다.** 세션당 2회(구 프롬프트 1 + 신 프롬프트 1) × --sessions.
    기본값이면 24회다. resolver 는 부르지 않으므로 임베딩 비용은 없다.
    표본을 줄여 먼저 감을 보려면 --sessions 4(8회).

판정
    이 스크립트는 합격/불합격을 정하지 않는다. 출력은 PR 본문에 붙여 사람이 읽는 근거다 —
    임계를 정할 표본이 없는 상태에서 자동 판정을 만들면 마술 상수가 하나 더 생긴다.
    그 근거에는 **어느 provider·모델로 쟀는지**가 포함된다 — 모델이 바뀌면 분포도 바뀌므로,
    provider·모델 없는 승격률 숫자는 나중에 재현·비교가 안 된다. 그래서 실행 첫 줄에 찍는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.agents.buyer.recommendation.state import extract_json  # noqa: E402
from app.agents.profile.builder import _DELTA_SYSTEM, _DELTA_SYSTEM_LEGACY  # noqa: E402
from app.agents.profile.gate import should_promote  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.llm import get_llm, resolve_model_id  # noqa: E402

_GOLDENSET = REPO_ROOT / "evals" / "goldenset" / "cases" / "buyer_dev.jsonl"


def load_sessions(count: int, turns: int) -> list[list[str]]:
    """골든셋 발화를 유사 세션으로 묶는다.

    프로필 파이프라인의 입력은 **세션 버퍼**(발화 여러 개)라 단발 질의 하나로는 델타 추출이
    실제 운영과 다른 모양의 입력을 받는다. 라벨은 쓰지 않고 `query` 문자열만 재료로 쓴다 —
    상품 순위 라벨은 여기서 잴 대상이 아니다.

    슬라이스를 섞지 않고 파일 순서를 그대로 쓴다: 표본 선택에 임의성이 들어가면 구·신 비교가
    프롬프트 차이가 아니라 표본 차이를 잴 수 있다.
    """
    queries: list[str] = []
    with _GOLDENSET.open(encoding="utf-8") as handle:
        for line in handle:
            case = json.loads(line)
            query = (case.get("query") or "").strip()
            if query:
                queries.append(query)
    sessions = [queries[i : i + turns] for i in range(0, len(queries), turns)]
    return [s for s in sessions if len(s) == turns][:count]


async def run_prompt(llm, system: str, buffer: list[str], settings) -> dict:
    """한 세션에 프롬프트 하나를 적용하고 관측치를 뽑는다."""
    try:
        raw = await llm.complete(
            system=system, user="\n".join(buffer), tier="smart", max_tokens=800
        )
    except Exception as exc:  # noqa: BLE001 - 전송 실패와 출력 실패를 갈라 기록한다
        return {"transport_error": type(exc).__name__}

    data = extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("deltas"), list):
        return {"schema_violation": True, "raw_head": (raw or "")[:120]}

    deltas = [d for d in data["deltas"] if isinstance(d, dict)]
    promoted = 0
    kinds: Counter[str] = Counter()
    missing_fields = 0
    for delta in deltas:
        if should_promote(
            salience=_as_float(delta.get("salience")),
            explicit=bool(delta.get("explicit")),
            repetition_ema=_as_float(delta.get("repetitionEma")),
            threshold=settings.profile_gate_threshold,
        ):
            promoted += 1
        kind = str(delta.get("kind") or "")
        if kind:
            kinds[kind] += 1
        elif system is _DELTA_SYSTEM:
            missing_fields += 1  # 신 프롬프트인데 구조화 필드가 없다
    return {
        "deltas": len(deltas),
        "promoted": promoted,
        "kinds": kinds,
        "missing_structured": missing_fields,
    }


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def summarize(label: str, results: list[dict]) -> None:
    sessions = len(results)
    transport = sum(1 for r in results if "transport_error" in r)
    violations = sum(1 for r in results if r.get("schema_violation"))
    usable = [r for r in results if "deltas" in r]
    deltas = sum(r["deltas"] for r in usable)
    promoted = sum(r["promoted"] for r in usable)
    kinds: Counter[str] = Counter()
    for r in usable:
        kinds.update(r.get("kinds", {}))

    print(f"\n── {label} ──")
    print(f"  세션               {sessions}")
    print(f"  전송 실패          {transport}")  # 모델 출력 실패와 구분해서 센다
    print(f"  JSON 스키마 위반   {violations} ({_pct(violations, sessions)})")
    print(f"  델타 총계          {deltas} (세션당 {deltas / max(1, len(usable)):.2f})")
    print(f"  승격               {promoted} ({_pct(promoted, deltas)})")
    if kinds:
        print(f"  kind 분포          {dict(kinds.most_common())}")
    missing = sum(r.get("missing_structured", 0) for r in usable)
    if missing:
        print(f"  구조화 필드 누락   {missing}")


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


async def main() -> int:
    parser = argparse.ArgumentParser(description="#356 델타 프롬프트 분포 비교(OPEN-G8)")
    parser.add_argument("--sessions", type=int, default=12, help="비교할 유사 세션 수")
    parser.add_argument("--turns", type=int, default=3, help="세션당 발화 수")
    args = parser.parse_args()

    settings = get_settings()
    sessions = load_sessions(args.sessions, args.turns)
    if not sessions:
        print("표본 0 — 골든셋 경로를 확인하라(표본 0 은 근거가 아니라 질문이다)")
        return 1

    provider = settings.llm_provider
    llm = get_llm()
    if llm is None:
        # provider 를 하드코딩하면 키를 엉뚱한 곳에 채우게 안내한다 — 실제 설정을 읽어 알린다.
        key_env = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        print(f"LLM 미구성(LLM_PROVIDER={provider}) — {key_env} 를 설정하라")
        return 1

    model = resolve_model_id(settings, "smart")  # 프로브는 smart tier 로만 부른다
    print(f"provider={provider} model={model}  # 이 분포는 이 모델에서 잰 값이다")
    print(f"세션 {len(sessions)}개 × 발화 {args.turns}개, LLM 호출 {2 * len(sessions)}회")
    legacy = [await run_prompt(llm, _DELTA_SYSTEM_LEGACY, s, settings) for s in sessions]
    structured = [await run_prompt(llm, _DELTA_SYSTEM, s, settings) for s in sessions]

    summarize("구 프롬프트(_DELTA_SYSTEM_LEGACY)", legacy)
    summarize("신 프롬프트(_DELTA_SYSTEM)", structured)
    print(
        "\n판정은 사람이 한다 — 승격률이 크게 움직였으면 profile_gate_threshold 의 의미가 함께"
        "\n바뀐 것이고, priceBand 가 0건이면 엄격 파서가 받을 라벨이 안 나온다는 뜻이다."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
