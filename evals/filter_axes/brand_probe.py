"""브랜드 추출 축 실 LLM 프로브 (#466) — **수동 도구다. CI 에 넣지 않는다**(evals/README.md 규약3).

    uv run python -m evals.filter_axes.brand_probe --n 3 --label before
    uv run python -m evals.filter_axes.brand_probe --n 3 --prompt cand.txt --label after

`decompose()`(프로덕션 코드, 무수정)를 `brand_cases.json` 의 발화에 호출해 브랜드 축 4종을
센다. 앵커는 데이터 파일이고 이 스크립트는 그것만 읽는다(규약7).

## 왜 새로 세웠나 (규약1·이슈 #466 「할 일」①: 기존 축부터 확인할 것)

- `evals/filter_axes` 의 `brand` 축은 **정의만 있고 실질 표본이 없다** — goldenset dev 109건
  중 `expectedFilters.brand` 라벨은 `buy-over-0003` **1건뿐**이라 support≈1 이다.
- `evals/combo_matrix` 의 `brand` 축은 absent/present **관측 전용**이다.
- `evals/underspecified_probe` 에는 브랜드-only 앵커가 2건(`buy-under-0005`·`under-wa-0002`)
  뿐이고, 재는 것도 브랜드 추출이 아니라 **하류 판정**(`is_underspecified_turn`)이다.
즉 "브랜드가 실제로 뽑히는가"를 재는 축은 없었다. 그래서 라벨 없이 규모를 늘릴 수 있는
MFT(positive) + 오추출 대조(negative) 구조로 세운다(규약6).

## 축 4종 — 분자/분모 (규약8)

분모는 전부 `positives × n`(negative 축만 `negatives × n`)이다.

- `present`  = `filters.brand` 가 비어 있지 않은 표본 수. **추출 자체**.
- `verbatim` = 산출 값이 **전부** 발화 안에 그대로(정규화 후 부분문자열) 있는 표본 수.
  `brandName` 은 exact IN 이라(api-spec §4.6) 번안값("애플"→"Apple")은 조용히 빗나간다 —
  이 축이 그 결함을 잡는다. `present` 의 부분집합이다.
- `expected` = 케이스가 라벨한 브랜드 표기와 정규화 동등한 값을 포함한 표본 수.
  `verbatim` 이 "발화에서 왔는가"라면 이쪽은 "**그** 브랜드인가"다(예: "삼성 제품"에서
  `["제품"]` 을 뽑으면 verbatim 은 통과하지만 expected 는 실패한다).
- `spurious` = **negative** 발화에서 `filters.brand` 가 채워진 표본 수. **낮을수록 좋다.**
  brand 는 `underspecified._WHAT_FILTER_AXES` 라 오추출 하나가 과소지정 되물음을 잠재운다.

`spurious` 를 함께 재지 않으면 "전부 브랜드로 찍기"가 만점을 받는다 — trivial baseline 을
못 넘는 개선을 개선이라 부르지 않기 위한 대조군이다(규약1).

## 실측 (gpt-5-nano = 배포 fast 티어, n=3 → 60표본, 전/후 각 2런)

| 축 | before | after(브랜드 절) |
|---|---|---|
| present  | 17·19/60 | 45·42/60 |
| verbatim | 13·11/60 | 45·42/60 |
| expected | 13·11/60 | 45·42/60 |
| spurious | 0·0/12   | 0·0/12   |

사전 등록 문턱은 "after 두 런 모두 before **최댓값** 이상"이었고 45·42 ≫ 19 로 통과했다
(인접 레인 #443/#465 가 같은 티어에서 잰 런간 폭 ≈5/48 보다 효과가 한 자릿수 크다).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import unicodedata
from pathlib import Path
from typing import Any

from app.agents.buyer.recommendation.decompose import decompose

CASES_PATH = Path(__file__).parent / "brand_cases.json"


def norm(value: str) -> str:
    """비교용 정규화 — NFC + strip + casefold (`app.pipelines.brand_aliases._norm` 과 동일 관례)."""
    return unicodedata.normalize("NFC", value).strip().casefold()


def load_cases(path: Path = CASES_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [c["caseId"] for c in data["positives"] + data["negatives"]]
    if len(ids) != len(set(ids)):
        raise ValueError("brand_cases.json caseId 가 중복됐습니다")
    for case in data["positives"]:
        if norm(case["brand"]) not in norm(case["utterance"]):
            raise ValueError(
                f"{case['caseId']}: 라벨 brand 가 발화에 없습니다 — verbatim 축의 기준이 깨집니다"
            )
    return data


def score_positive(brand_values: list[str] | None, utterance: str, expected: str) -> dict[str, int]:
    """positive 1표본 → present/verbatim/expected (분자, 위 docstring 규약8)."""
    if not brand_values:
        return {"present": 0, "verbatim": 0, "expected": 0}
    values = [norm(v) for v in brand_values if v and v.strip()]
    if not values:
        return {"present": 0, "verbatim": 0, "expected": 0}
    utter = norm(utterance)
    return {
        "present": 1,
        "verbatim": int(all(v in utter for v in values)),
        "expected": int(norm(expected) in values),
    }


def score_negative(brand_values: list[str] | None) -> dict[str, int]:
    """negative 1표본 → spurious(낮을수록 좋다)."""
    return {"spurious": int(bool(brand_values and any(v and v.strip() for v in brand_values)))}


def aggregate(positive_rows: list[dict], negative_rows: list[dict], n: int) -> dict[str, Any]:
    """축별 분자·분모를 함께 돌려준다(규약8 — 분모 없이 비율만 적지 않는다)."""
    pos_denom, neg_denom = len(positive_rows) * n, len(negative_rows) * n
    totals = {
        axis: sum(row[axis] for row in positive_rows)
        for axis in ("present", "verbatim", "expected")
    }
    return {
        "present": {"numerator": totals["present"], "denominator": pos_denom},
        "verbatim": {"numerator": totals["verbatim"], "denominator": pos_denom},
        "expected": {"numerator": totals["expected"], "denominator": pos_denom},
        "spurious": {
            "numerator": sum(row["spurious"] for row in negative_rows),
            "denominator": neg_denom,
        },
    }


def _is_rate_limited(exc: BaseException) -> bool:
    text = str(exc)
    return "429" in text or "rate_limit" in text


async def _sample(
    llm,
    utterance: str,
    tier: str,
    sem: asyncio.Semaphore,
    *,
    max_retries: int = 8,
    sleep=asyncio.sleep,
) -> list[str] | None:
    """표본 1건. TPM 상한(429)에는 지수 백오프로 재시도한다.

    **이게 없으면 이 프로브는 이 환경에서 쓸 수 없다** — OpenAI TPM 은 org 단위라 동시에 도는
    다른 레인과 공유되고, 첫 429 한 번에 런 전체가 죽는다(#466 에서 실제로 4런을 통째로
    날렸다). 429 **외의** 오류는 삼키지 않고 그대로 올린다 — 조용한 표본 유실은 분모를
    왜곡해 전/후 비교를 망친다.
    """
    for attempt in range(max_retries):
        try:
            async with sem:
                decision = await decompose(
                    llm,
                    query=utterance,
                    prior_filters=None,
                    profile_summary=None,
                    tier=tier,
                    category_fanout_max=5,
                )
            return decision.filters.brand
        except Exception as exc:  # noqa: BLE001 - 429 만 재시도, 나머지는 아래에서 재전파
            if not _is_rate_limited(exc) or attempt == max_retries - 1:
                raise
            await sleep(min(2**attempt, 30))
    raise RuntimeError("도달 불가 — 루프는 반환하거나 raise 한다")


async def run(llm, cases: dict[str, Any], *, n: int, tier: str, concurrency: int) -> dict[str, Any]:
    """앵커 전부를 n 회씩 돌려 축을 집계한다. LLM 은 주입받는다 — 테스트는 fake 만 쓴다."""
    sem = asyncio.Semaphore(concurrency)
    positive_rows, negative_rows = [], []
    for case in cases["positives"]:
        got = await asyncio.gather(*[_sample(llm, case["utterance"], tier, sem) for _ in range(n)])
        scored = [score_positive(b, case["utterance"], case["brand"]) for b in got]
        positive_rows.append(
            {
                "caseId": case["caseId"],
                "utterance": case["utterance"],
                "samples": got,
                **{
                    axis: sum(s[axis] for s in scored)
                    for axis in ("present", "verbatim", "expected")
                },
            }
        )
    for case in cases["negatives"]:
        got = await asyncio.gather(*[_sample(llm, case["utterance"], tier, sem) for _ in range(n)])
        negative_rows.append(
            {
                "caseId": case["caseId"],
                "utterance": case["utterance"],
                "samples": got,
                "spurious": sum(score_negative(b)["spurious"] for b in got),
            }
        )
    return {
        "n": n,
        "tier": tier,
        "datasetVersion": cases["datasetVersion"],
        "axes": aggregate(positive_rows, negative_rows, n),
        "positives": positive_rows,
        "negatives": negative_rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="브랜드 추출 축 실 LLM 프로브 (#466)")
    parser.add_argument("--n", type=int, default=3, help="앵커당 표본 수")
    parser.add_argument("--tier", choices=("fast", "smart"), default="fast")
    parser.add_argument("--concurrency", type=int, default=2, help="org TPM 은 레인 간 공유다")
    parser.add_argument("--prompt", type=Path, help="후보 _SYSTEM 파일 — 생략하면 커밋된 프롬프트")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", type=Path, help="results.json 경로")
    return parser


async def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cases = load_cases()

    if args.prompt:
        # A/B 전용 — 커밋된 프롬프트를 후보로 갈아끼운다(`underspecified_probe --prompt` 와 같은 수단).
        import app.agents.buyer.recommendation.decompose as decompose_module

        decompose_module._SYSTEM = args.prompt.read_text(encoding="utf-8")

    from app.core.llm import get_llm

    llm = get_llm()
    if llm is None:
        print("LLM 미구성 — provider 키를 확인하세요")
        return 2

    results = await run(llm, cases, n=args.n, tier=args.tier, concurrency=args.concurrency)
    axes = results["axes"]
    print(f"[{args.label}] n={args.n} tier={args.tier} dataset={results['datasetVersion']}")
    for axis, value in axes.items():
        print(f"  {axis:9s} {value['numerator']}/{value['denominator']}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
