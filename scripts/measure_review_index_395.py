#!/usr/bin/env python3
"""이슈 #395 후속 — BE PR #133(jarvis-backend f0a329f) review 커버링 인덱스의 효과를
운영 백업(리뷰 11만 건대)에서 실측한다.

BE가 노션 댓글에 남긴 한계: "로컬 DB에 리뷰가 0건이라 실행계획만 확인했고 소요 시간은
재지 못했습니다." 리뷰가 있는 운영 백업이 우리에게만 있어 대신 잰다.

사전 준비(1회, 이 스크립트 밖):
    cd /home/uuser/inte-final/_backup/20260809 && ./restore-mariadb.sh
    (jarvis-restore-maria 컨테이너, 포트 3307에 리뷰 114,077건 복원)
    BE를 --network host 로 그 DB를 보게 띄운다(측정 문서 §1 참고).

재현:
    uv run python scripts/measure_review_index_395.py --apply-index
        # A(before) → B(after) → A(before) 전체 스윕. --apply-index 없이는
        # ALTER TABLE을 절대 실행하지 않는다(사고 방지) — 이 경우 현재 인덱스
        # 상태 그대로 1회만 측정한다.

    uv run python scripts/measure_review_index_395.py --skip-http
        # BE 컨테이너가 없을 때 DB 레벨만 측정.

접속 정보는 인자로 받는다(기본값은 restore-mariadb.sh가 스스로 박아 넣는 로컬 전용
더미 자격이라 소스에 있어도 무해하다 — 운영 자격과 무관). 환경변수로 덮어쓸 수 있다:
    MEASURE_DB_PASSWORD, MEASURE_INTERNAL_TOKEN

DB 드라이버 신규 의존성을 피하려고 `docker exec <container> mariadb -e ...`를
subprocess로 호출한다(표준 라이브러리만 사용).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

WARMUP_RUNS = 3
MEASURED_RUNS = 10

# 2026-08-07 덤프 기준 — §3.1 "leaf" 케이스. 40건(재고 있는 ON_SALE 기준).
LEAF_CATEGORY_ID = 157392844414274913
LEAF_CATEGORY_NAME = "선케어 > 선크림/선블록"

QUERIES = {
    "worst": {
        "label": "무필터(최악)",
        "where_extra": "",
        "http_params": {},
    },
    "keyword": {
        "label": "keyword=세트",
        "where_extra": (
            "AND (LOWER(p1_0.name) LIKE LOWER('%세트%') "
            "OR LOWER(p1_0.summary) LIKE LOWER('%세트%') "
            "OR LOWER(p1_0.attributes) LIKE LOWER('%세트%'))"
        ),
        "http_params": {"keyword": "세트"},
    },
    "leaf": {
        "label": f"leaf categoryName={LEAF_CATEGORY_NAME}",
        "where_extra": f"AND p1_0.category_id = {LEAF_CATEGORY_ID}",
        "http_params": {"categoryName": LEAF_CATEGORY_NAME},
    },
}


def build_query(where_extra: str) -> str:
    """§2 에서 general log로 캡처한 Hibernate 생성 SQL과 같은 join·group by 형태.
    캡처본의 파라미터 자리(카테고리/브랜드/가격/색상 sentinel)는 실제로 쓰지 않는
    질의 3종 각각에 맞춰 리터럴 조건으로 대체했다 — 구조는 그대로, 값만 구체화."""
    return (
        "SELECT p1_0.id, COUNT(r1_0.id), AVG(r1_0.rating) "
        "FROM product p1_0 "
        "LEFT JOIN review r1_0 ON r1_0.product_id = p1_0.id AND r1_0.status = 'VISIBLE' "
        "WHERE p1_0.status = 'ON_SALE' AND p1_0.stock_quantity > 0 "
        f"{where_extra} "
        "GROUP BY p1_0.id"
    )


class Db:
    def __init__(self, container: str, user: str, password: str, name: str):
        self.container = container
        self.user = user
        self.password = password
        self.name = name

    def run(self, sql: str) -> str:
        cmd = [
            "docker", "exec", self.container, "mariadb",
            "-N", "-B", f"-u{self.user}", f"-p{self.password}", self.name,
            "-e", sql,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"SQL 실패:\n{sql}\n---stderr---\n{result.stderr}")
        return result.stdout

    def run_verbose(self, sql: str) -> str:
        """헤더·표 형태 그대로(EXPLAIN·SHOW INDEX 사람이 읽을 출력용)."""
        cmd = [
            "docker", "exec", self.container, "mariadb",
            f"-u{self.user}", f"-p{self.password}", self.name,
            "-e", sql,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"SQL 실패:\n{sql}\n---stderr---\n{result.stderr}")
        return result.stdout


def show_index(db: Db) -> str:
    return db.run_verbose("SHOW INDEX FROM review;")


def index_is_covering(db: Db) -> bool:
    out = db.run("SHOW INDEX FROM review WHERE Key_name='idx_review_product';")
    cols = [line.split("\t")[4] for line in out.strip().splitlines() if line.strip()]
    return cols == ["product_id", "status", "rating"]


def apply_index(db: Db, covering: bool) -> None:
    if covering:
        ddl = (
            "ALTER TABLE review DROP INDEX idx_review_product, "
            "ADD INDEX idx_review_product (product_id, status, rating);"
        )
    else:
        ddl = (
            "ALTER TABLE review DROP INDEX idx_review_product, "
            "ADD INDEX idx_review_product (product_id);"
        )
    db.run(ddl)


def explain_capture(db: Db, query_sql: str) -> dict:
    traditional = db.run_verbose(f"EXPLAIN {query_sql};")
    try:
        analyze = db.run_verbose(f"ANALYZE FORMAT=JSON {query_sql};")
    except RuntimeError as e:  # ANALYZE FORMAT=JSON 미지원 버전 대비
        analyze = f"(ANALYZE FORMAT=JSON 미지원 또는 실패: {e})"
    return {"explain": traditional, "analyze_json": analyze}


def match_count(db: Db, query_sql: str) -> int:
    out = db.run(f"SELECT COUNT(*) FROM ({query_sql}) x;")
    return int(out.strip())


def timed_runs(db: Db, query_sql: str, warmup: int, measured: int) -> list[float]:
    """단일 docker exec/세션 안에서 서버 사이드 NOW(6) 델타로 잰다 — docker exec를
    매 회 새로 fork하는 오버헤드가 특히 leaf(수십 건)처럼 빠른 질의의 신호를
    파묻지 않도록. 결과 행은 COUNT(*)로 감싸 클라이언트로 넘어오는 바이트도 줄인다."""
    stmts = []
    total = warmup + measured
    for i in range(total):
        stmts.append(f"SET @t{i}_0 = NOW(6);")
        stmts.append(f"SELECT COUNT(*) FROM ({query_sql}) x_{i};")
        stmts.append(f"SET @t{i}_1 = NOW(6);")
    select_all = "SELECT " + ", ".join(
        f"TIMESTAMPDIFF(MICROSECOND, @t{i}_0, @t{i}_1)" for i in range(total)
    ) + ";"
    stmts.append(select_all)
    script = "\n".join(stmts)
    out = db.run(script)
    lines = [line for line in out.strip().splitlines() if line.strip()]
    # 각 SELECT COUNT(*) 가 자기 결과(1개 값)를 한 줄씩 찍고, 마지막 줄이 전체 델타 묶음이다.
    deltas_line = lines[-1]
    all_us = [int(x) for x in deltas_line.split("\t")]
    return [us / 1000.0 for us in all_us[warmup:]]  # ms로, 워밍업 버림


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def measure_state(db: Db, state_label: str, http_base: str | None, http_token: str) -> dict:
    result = {"state": state_label, "queries": {}, "http": {}}
    for key, spec in QUERIES.items():
        query_sql = build_query(spec["where_extra"])
        rows = match_count(db, query_sql)
        durations = timed_runs(db, query_sql, WARMUP_RUNS, MEASURED_RUNS)
        explain = explain_capture(db, query_sql)
        result["queries"][key] = {
            "label": spec["label"],
            "matched_rows": rows,
            "median_ms": statistics.median(durations),
            "p95_ms": percentile(durations, 0.95),
            "all_ms": durations,
            "explain": explain,
            "sql": query_sql,
        }
    if http_base and http_token:
        try:
            result["http"] = http_measure(http_base, http_token)
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            result["http"] = {"error": str(e)}
    return result


def http_measure(base_url: str, token: str) -> dict:
    result = {}
    for key, spec in QUERIES.items():
        qs = urllib.parse.urlencode(spec["http_params"])
        url = f"{base_url}/internal/products/search"
        if qs:
            url += f"?{qs}"
        durations = []
        body_size = None
        status = None
        for i in range(WARMUP_RUNS + MEASURED_RUNS):
            req = urllib.request.Request(url, headers={"X-Internal-Token": token})
            import time
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                status = resp.status
            t1 = time.perf_counter()
            if i >= WARMUP_RUNS:
                durations.append((t1 - t0) * 1000.0)
                body_size = len(body)
        result[key] = {
            "label": spec["label"],
            "status": status,
            "body_bytes": body_size,
            "median_ms": statistics.median(durations),
            "p95_ms": percentile(durations, 0.95),
        }
    return result


def print_markdown_table(before: dict, after: dict, before2: dict | None) -> None:
    print("\n### DB 레벨")
    print("| 질의 | 상태 | 매칭 행 | 중앙값(ms) | p95(ms) |")
    print("|---|---|---:|---:|---:|")
    for key in QUERIES:
        for label, snap in (("before(A1)", before), ("after(B)", after), ("before(A2)", before2)):
            if snap is None:
                continue
            q = snap["queries"][key]
            print(f"| {q['label']} | {label} | {q['matched_rows']} | {q['median_ms']:.2f} | {q['p95_ms']:.2f} |")

    if any(snap and snap.get("http") and "error" not in snap["http"] for snap in (before, after, before2)):
        print("\n### 종단(HTTP)")
        print("| 질의 | 상태 | 바디(B) | 중앙값(ms) | p95(ms) |")
        print("|---|---|---:|---:|---:|")
        for key in QUERIES:
            for label, snap in (("before(A1)", before), ("after(B)", after), ("before(A2)", before2)):
                if not snap or not snap.get("http") or "error" in snap["http"]:
                    continue
                h = snap["http"][key]
                print(f"| {h['label']} | {label} | {h['body_bytes']} | {h['median_ms']:.2f} | {h['p95_ms']:.2f} |")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--container", default="jarvis-restore-maria")
    ap.add_argument("--db-user", default="root")
    ap.add_argument("--db-password", default=os.environ.get("MEASURE_DB_PASSWORD", "jarvis"))
    ap.add_argument("--db-name", default="jarvis")
    ap.add_argument("--apply-index", action="store_true",
                     help="명시하지 않으면 ALTER TABLE을 절대 실행하지 않는다(A/B/A 스윕 불가, 현재 상태만 1회 측정)")
    ap.add_argument("--skip-http", action="store_true", help="BE 컨테이너가 없을 때 DB 레벨만 측정")
    ap.add_argument("--http-base", default="http://localhost:8099")
    ap.add_argument("--internal-token", default=os.environ.get("MEASURE_INTERNAL_TOKEN", ""))
    ap.add_argument("--json-out", default=None, help="전체 결과를 JSON으로도 저장할 경로")
    args = ap.parse_args()

    db = Db(args.container, args.db_user, args.db_password, args.db_name)

    print("=== 접속 확인 ===")
    print(db.run_verbose("SELECT COUNT(*) AS review_rows FROM review; SELECT COUNT(*) AS product_rows FROM product;"))

    report: dict = {"db_states": []}

    if not args.skip_http and not args.internal_token:
        print("\n⚠ --internal-token(또는 MEASURE_INTERNAL_TOKEN) 미설정 — 종단 측정 skip.")
    http_base = None if args.skip_http or not args.internal_token else args.http_base
    http_token = args.internal_token

    if not args.apply_index:
        print("⚠ --apply-index 미지정 — DDL 실행 없이 현재 인덱스 상태만 1회 측정한다.")
        print("현재 SHOW INDEX FROM review:")
        print(show_index(db))
        snap = measure_state(db, "current(toggle 없음)", http_base, http_token)
        report["db_states"].append(snap)
        print_markdown_table(snap, snap, None)
    else:
        # A(before)
        if index_is_covering(db):
            print("현재 인덱스가 이미 커버링 상태 — before로 되돌린다.")
            apply_index(db, covering=False)
        print("\n=== A1 (before: product_id 단독) ===")
        print(show_index(db))
        a1 = measure_state(db, "A1-before", http_base, http_token)
        report["db_states"].append(a1)

        # B(after)
        print("\n▶ 인덱스를 커버링으로 확장")
        apply_index(db, covering=True)
        print("\n=== B (after: product_id, status, rating) ===")
        print(show_index(db))
        b = measure_state(db, "B-after", http_base, http_token)
        report["db_states"].append(b)

        # A(before) 재확인
        print("\n▶ 인덱스를 before로 되돌림")
        apply_index(db, covering=False)
        print("\n=== A2 (before 재확인) ===")
        print(show_index(db))
        a2 = measure_state(db, "A2-before-repeat", http_base, http_token)
        report["db_states"].append(a2)

        print_markdown_table(a1, b, a2)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n전체 결과 JSON: {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
