"""INV/DIR/회원-게스트(#119) 필터 축 probe — 수동 실 LLM 도구다. **CI 에 넣지 않는다**(규약 3).

uv run python -m evals.filter_axes.probe --out artifacts/fax-run1

`decompose()`(프로덕션 코드, 무수정)를 probe 케이스의 base/variant 발화에 호출해 산출 필터에
`evals/filter_axes/metrics.py`의 judge 함수를 적용하고, 위반이면 exit code 1로 리포트를
`--out`에 쓴다. LLM 클라이언트는 주입 가능하다 — 테스트는 fake 클라이언트만 쓴다(#334).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Protocol

from app.agents.buyer.recommendation.decompose import decompose
from app.agents.buyer.recommendation.state import RouteDecision
from app.core.config import Settings, get_settings
from app.schemas.spring import ProductSearchFilters, ProductSearchResult, SpringProduct
from app.services import search_service
from evals.filter_axes.cases import ProbeCase, load_cases_manifest, load_probe_cases
from evals.filter_axes.metrics import (
    judge_candidate_subset,
    judge_direction,
    judge_invariance,
    judge_profile_leak,
)
from evals.filter_axes.spec import AXES_SPEC_PATH, axes_spec_sha256, load_axes_spec
from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.metrics.run_manifest import build_run_manifest

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_REJECTED = 2


class ProbeLLM(Protocol):
    """probe가 필요로 하는 최소 LLM 계약 — `app.core.llm.LLMClient`와 호환된다."""

    async def complete(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024, json_output: bool = True
    ) -> str: ...

    def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]: ...


def decision_filters(decision: RouteDecision) -> dict[str, object]:
    """RouteDecision → probe judge 함수가 받는 camelCase 필터 dict(totalBudget·buyAll 포함)."""
    merged = decision.filters.model_dump(by_alias=True)
    merged["totalBudget"] = decision.total_budget
    merged["buyAll"] = decision.buy_all
    return merged


def persona_profile_summary(persona_id: str, purchase_history: Mapping[str, Any]) -> str | None:
    """구매 이력 fixture(`evals/goldenset/fixtures/purchase_history.json`)에서 결정론 요약을 만든다.

    실제 파이프라인의 프로필 요약 생성기를 흉내내지 않는다 — probe 목적은 "이력이 실리면
    decompose가 하드필터를 승격하는가"(#119)이지 요약 문구 품질이 아니라서, 카테고리·평균가만
    담은 최소 문장으로 충분하다.
    """
    history = purchase_history.get(persona_id)
    if not history or not history.get("orders"):
        return None
    items = [item for order in history["orders"] for item in order.get("items", [])]
    if not items:
        return None
    categories = sorted({item["categoryName"] for item in items if item.get("categoryName")})
    prices = [item["price"] for item in items if item.get("price") is not None]
    avg_price = round(sum(prices) / len(prices)) if prices else None
    parts = [f"최근 구매 카테고리: {', '.join(categories)}"] if categories else []
    if avg_price is not None:
        parts.append(f"평균 구매가: {avg_price}원")
    return " / ".join(parts) or None


class LocalCatalogSearchBackend:
    """DIR 후보 집합 검사용 결정론 fixture 검색 백엔드 — Spring 없이 카탈로그 스냅샷을 직접 매칭한다.

    `search_service.search_catalog`가 받는 `SearchBackend` 프로토콜을 구현한다(app 코드 무수정).
    매칭 규칙은 real Spring I-1 재현이 아니라 "제약을 더하면 후보가 줄거나 같다"는 DIR 성질만
    검증할 수 있으면 충분한 단순 substring/range 매칭이다.
    """

    def __init__(self, catalog: Mapping[str, Mapping[str, Any]]) -> None:
        self._catalog = catalog

    async def search(self, filters: ProductSearchFilters) -> ProductSearchResult:
        matched = [product for product in self._catalog.values() if self._matches(filters, product)]
        return ProductSearchResult(
            products=[SpringProduct.model_validate(product) for product in matched],
            total_count=len(matched),
        )

    @staticmethod
    def _matches(filters: ProductSearchFilters, product: Mapping[str, Any]) -> bool:
        if filters.category and filters.category not in str(product.get("categoryName") or ""):
            return False
        if filters.brand:
            brand_name = str(product.get("brandName") or "")
            if not any(candidate in brand_name for candidate in filters.brand):
                return False
        if filters.keyword and filters.keyword not in str(product.get("name") or ""):
            return False
        price = product.get("price")
        if filters.price_min is not None and (price is None or price < filters.price_min):
            return False
        if filters.price_max is not None and (price is None or price > filters.price_max):
            return False
        if filters.rating_min is not None:
            rating = product.get("rating")
            if rating is None or rating < filters.rating_min:
                return False
        if filters.color:
            haystack = json.dumps(product.get("attributes") or {}, ensure_ascii=False) + str(
                product.get("name") or ""
            )
            if filters.color not in haystack:
                return False
        return True


async def _decompose_filters(
    llm: ProbeLLM, *, query: str, profile_summary: str | None, settings: Settings
) -> RouteDecision:
    return await decompose(
        llm,  # type: ignore[arg-type]
        query=query,
        prior_filters=None,
        profile_summary=profile_summary,
        tier="fast",
        category_fanout_max=settings.category_fanout_max,
        repurchase_max=settings.dedup_repurchase_max,
    )


async def run_inv_case(
    llm: ProbeLLM, case: ProbeCase, *, settings: Settings, spec: Mapping[str, Any]
) -> dict[str, Any]:
    base = await _decompose_filters(
        llm, query=case.base_query, profile_summary=None, settings=settings
    )
    variant = await _decompose_filters(
        llm, query=case.variant_query, profile_summary=None, settings=settings
    )
    base_filters = decision_filters(base)
    variant_filters = decision_filters(variant)
    verdict = judge_invariance(base_filters, variant_filters, spec)
    return {
        "caseId": case.case_id,
        "kind": "inv",
        "variantKind": case.variant_kind,
        "baseFilters": base_filters,
        "variantFilters": variant_filters,
        "verdict": verdict,
        "ok": verdict["ok"],
    }


async def run_dir_case(
    llm: ProbeLLM,
    case: ProbeCase,
    *,
    settings: Settings,
    spec: Mapping[str, Any],
    search_backend: LocalCatalogSearchBackend,
) -> dict[str, Any]:
    base = await _decompose_filters(
        llm, query=case.base_query, profile_summary=None, settings=settings
    )
    variant = await _decompose_filters(
        llm, query=case.variant_query, profile_summary=None, settings=settings
    )
    base_filters = decision_filters(base)
    variant_filters = decision_filters(variant)
    verdict = judge_direction(base_filters, variant_filters, case.expected_new_axes or [], spec)
    base_result = await search_service.search_catalog(base.filters, backend=search_backend)
    variant_result = await search_service.search_catalog(variant.filters, backend=search_backend)
    subset = judge_candidate_subset(
        [product.product_id for product in base_result.products],
        [product.product_id for product in variant_result.products],
    )
    return {
        "caseId": case.case_id,
        "kind": "dir",
        "variantKind": case.variant_kind,
        "expectedNewAxes": case.expected_new_axes or [],
        "baseFilters": base_filters,
        "variantFilters": variant_filters,
        "verdict": verdict,
        "candidateSubset": subset,
        "ok": verdict["ok"] and subset["ok"],
    }


async def run_pair_case(
    llm: ProbeLLM,
    case: ProbeCase,
    *,
    settings: Settings,
    spec: Mapping[str, Any],
    purchase_history: Mapping[str, Any],
) -> dict[str, Any]:
    assert case.identity_pair is not None
    guest = await _decompose_filters(
        llm, query=case.base_query, profile_summary=None, settings=settings
    )
    member_persona = case.identity_pair.member.persona_id
    member_summary = (
        persona_profile_summary(member_persona, purchase_history) if member_persona else None
    )
    member = await _decompose_filters(
        llm, query=case.variant_query, profile_summary=member_summary, settings=settings
    )
    guest_filters = decision_filters(guest)
    member_filters = decision_filters(member)
    verdict = judge_profile_leak(guest_filters, member_filters, spec)
    return {
        "caseId": case.case_id,
        "kind": "pair",
        "personaId": member_persona,
        "guestFilters": guest_filters,
        "memberFilters": member_filters,
        "verdict": verdict,
        "ok": not verdict["leak"],
    }


async def run_probe(
    llm: ProbeLLM,
    cases: list[ProbeCase],
    *,
    settings: Settings,
    spec: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
    purchase_history: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """전 케이스를 kind별 핸들러로 실행한다. 케이스 순서를 보존해 결과 순서를 결정론으로 만든다."""
    backend = LocalCatalogSearchBackend(catalog)
    results: list[dict[str, Any]] = []
    for case in cases:
        if case.kind == "inv":
            results.append(await run_inv_case(llm, case, settings=settings, spec=spec))
        elif case.kind == "dir":
            results.append(
                await run_dir_case(llm, case, settings=settings, spec=spec, search_backend=backend)
            )
        else:
            results.append(
                await run_pair_case(
                    llm, case, settings=settings, spec=spec, purchase_history=purchase_history
                )
            )
    return results


def build_report(
    results: list[dict[str, Any]],
    *,
    spec: Mapping[str, Any],
    dataset_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """`dataset_manifest`는 `evals.filter_axes.cases.load_cases_manifest()`의 반환값이다 —
    probe_cases.jsonl의 datasetVersion·datasetHash·실측 해시 일치 여부를 산출물에 동봉한다
    (#334 리뷰 F4, 규약8)."""
    violations = [row for row in results if not row["ok"]]
    return {
        "axesSpecVersion": spec["version"],
        "axesSpecSha256": axes_spec_sha256(AXES_SPEC_PATH),
        "probeDatasetVersion": dataset_manifest["datasetVersion"],
        "probeDatasetHash": dataset_manifest["datasetHash"],
        "probeDatasetHashVerified": dataset_manifest["datasetHashVerified"],
        "caseCount": len(results),
        "caseIds": sorted(row["caseId"] for row in results),
        "violationCount": len(violations),
        "violationCaseIds": sorted(row["caseId"] for row in violations),
        "results": sorted(results, key=lambda row: row["caseId"]),
    }


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# 필터 축 INV/DIR/pair probe(#334)",
        "",
        f"- axes spec: `{report['axesSpecVersion']}` / `{report['axesSpecSha256']}`",
        f"- probe dataset: `{report['probeDatasetVersion']}` / `{report['probeDatasetHash']}` "
        f"(datasetHashVerified={report['probeDatasetHashVerified']})",
        f"- cases: {report['caseCount']} · violations: {report['violationCount']}",
        f"- caseIds: {', '.join(report['caseIds'])}",
        "",
        "## 위반 케이스" if report["violationCount"] else "## 위반 없음",
        "",
    ]
    if report["violationCaseIds"]:
        lines.extend(f"- {case_id}" for case_id in report["violationCaseIds"])
    return "\n".join(lines) + "\n"


def write_artifacts(
    output_dir: Path, report: Mapping[str, Any], run_manifest: Mapping[str, Any]
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        _json_text(report) + "\n", encoding="utf-8", newline="\n"
    )
    (output_dir / "run_manifest.json").write_text(
        _json_text(run_manifest) + "\n", encoding="utf-8", newline="\n"
    )
    (output_dir / "report.md").write_text(_markdown(report), encoding="utf-8", newline="\n")
    # 리뷰 F3 — hash만으로는 당시 정규화 규칙을 복원할 수 없다. axes.json 바이트를 동봉한다.
    (output_dir / "filter_axes_spec.json").write_bytes(AXES_SPEC_PATH.read_bytes())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="필터 축 INV/DIR/회원-게스트(#119) probe — 수동 실행 도구(#334)"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case-ids", help="caseId 쉼표 목록(부분 실행)")
    return parser


def _select_cases(cases: list[ProbeCase], case_ids: str | None) -> list[ProbeCase]:
    if not case_ids:
        return cases
    requested = {value.strip() for value in case_ids.split(",") if value.strip()}
    known = {case.case_id for case in cases}
    missing = sorted(requested - known)
    if missing:
        raise ValueError(f"알 수 없는 caseId: {missing}")
    return [case for case in cases if case.case_id in requested]


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((GOLDENSET_ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.out.exists():
        print(f"--out 디렉터리가 이미 있습니다: {args.out}")
        return EXIT_REJECTED

    try:
        cases = _select_cases(load_probe_cases(), args.case_ids)
        dataset_manifest = load_cases_manifest()
    except Exception as exc:
        print(f"입력 거절: {exc}")
        return EXIT_REJECTED

    settings = get_settings()
    spec = load_axes_spec()
    catalog = _load_fixture("catalog_snapshot.json")
    purchase_history = _load_fixture("purchase_history.json")

    from evals.intent_probe.client import build_live_delegate
    from evals.model_eval.budget import BudgetLimits, BudgetTracker
    from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook

    budget = BudgetTracker(
        BudgetLimits(
            max_calls=len(cases) * 4,
            max_total_tokens=settings.model_eval_max_total_tokens_per_run,
            max_cost_usd=settings.model_eval_max_cost_usd_per_run,
        )
    )
    delegate, model_config = build_live_delegate(
        runtime_settings=settings, budget=budget, pricing=PriceBook.load(DEFAULT_PRICING_PATH)
    )

    results = asyncio.run(
        run_probe(
            delegate,
            cases,
            settings=settings,
            spec=spec,
            catalog=catalog,
            purchase_history=purchase_history,
        )
    )
    report = build_report(results, spec=spec, dataset_manifest=dataset_manifest)
    manifest = build_run_manifest(
        command="uv run python -m evals.filter_axes.probe " + " ".join(argv), seed=20260806
    )
    manifest["filterAxesProbe"] = {
        "modelConfig": model_config,
        "caseCount": len(cases),
        "caseIds": sorted(case.case_id for case in cases),
        "datasetVersion": dataset_manifest["datasetVersion"],
        "datasetHash": dataset_manifest["datasetHash"],
        "datasetHashVerified": dataset_manifest["datasetHashVerified"],
    }
    write_artifacts(args.out, report, manifest)
    print(f"cases={report['caseCount']} violations={report['violationCount']} → {args.out}")
    return EXIT_VIOLATION if report["violationCount"] else EXIT_OK
