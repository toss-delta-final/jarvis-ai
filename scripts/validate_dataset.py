#!/usr/bin/env python3
"""구매자 추천 adversarial dataset과 20% 수동검토 기록을 검증한다."""

from __future__ import annotations

import argparse
from pathlib import Path

from evals.adversarial_recommendation.generator import CASES_PATH, REVIEW_PATH, load_cases
from evals.adversarial_recommendation.validation import validate_cases, validate_manual_review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=CASES_PATH)
    args = parser.parse_args()
    try:
        cases = load_cases(args.path)
    except (OSError, ValueError) as exc:
        print(f"INVALID: {exc}")
        return 1
    issues = validate_cases(cases)
    if args.path == CASES_PATH:
        issues.extend(validate_manual_review(cases, REVIEW_PATH))
    if issues:
        print(f"INVALID: {len(issues)} issue(s)")
        for issue in issues:
            print(f"- {issue.message}")
        return 1
    families = {case.family_id for case in cases}
    print(f"VALID: {len(families)} families, {len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
