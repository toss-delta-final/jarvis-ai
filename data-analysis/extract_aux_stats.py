"""REES46 보조 데이터셋 통계 추출 — cosmetics(remove_from_cart 행동) · orders(주문 구성).

extract_stats.py(multi-category 메인 통계)를 보완하는 두 가지 모드:

  --mode cosmetics : Kaggle mkechinov/ecommerce-events-history-in-cosmetics-shop
                     remove_from_cart 행동 파라미터 추출 → stats_cosmetics.json
                     (메인 데이터(2019-Oct multi-category)에는 remove 이벤트가 0건이라
                      화장품 몰 데이터에서 remove/cart 비율·타이밍·재담기 확률을 가져온다)

  --mode orders    : Kaggle mkechinov/ecommerce-purchase-history-from-electronics-store
                     주문(order_id) 단위 실측 → stats_orders.json
                     (주문당 라인 수·수량(동일 상품 반복행)·카테고리 교차 구매·
                      주문 금액·재구매 간격 — 이벤트 근사가 아닌 주문 실측)

사용 예 (PC):

    uv run --with duckdb python extract_aux_stats.py --mode cosmetics ^
        --input "C:\\Users\\vssea\\Downloads\\cosmetics.zip"

    uv run --with duckdb python extract_aux_stats.py --mode orders ^
        --input "C:\\Users\\vssea\\Downloads\\electronics_orders.zip"

  - .zip / .csv 모두 허용, 여러 개 지정 가능. zip은 "<zip이름>_csv" 하위 폴더에
    해제한다(cosmetics의 2019-Oct.csv가 multi-category 파일명과 겹치는 것 방지).
  - 결과물은 기본적으로 스크립트 폴더(data-analysis)에 저장.
  - 무거운 작업 파일(DuckDB·스필)은 --temp-dir 로 이동 가능, 종료 시 자동 삭제.
"""

from __future__ import annotations

import argparse
import csv as csv_mod
import json
import math
import shutil
import sys
import time
import zipfile
from pathlib import Path

try:
    import duckdb
except ImportError:  # pragma: no cover
    sys.exit("duckdb 가 필요합니다: pip install duckdb")

QUANTILES = [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 0.999]
BOT_MIN_THRESHOLD = 200

EVENT_COLUMNS = ["event_time", "event_type", "product_id", "category_id",
                 "category_code", "brand", "price", "user_id", "user_session"]
ORDER_COLUMNS = ["event_time", "order_id", "product_id", "category_id",
                 "category_code", "brand", "price", "user_id"]

_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - _T0:7.1f}s] {msg}", flush=True)


def resolve_inputs(paths: list[str]) -> list[Path]:
    """zip이면 '<zip이름>_csv' 폴더에 1회 해제하고 최종 CSV 목록을 돌려준다."""
    csvs: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            sys.exit(f"입력 파일이 없습니다: {p}")
        if p.suffix.lower() == ".zip":
            target_dir = p.parent / f"{p.stem}_csv"
            target_dir.mkdir(exist_ok=True)
            with zipfile.ZipFile(p) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
                if not members:
                    sys.exit(f"zip 안에 CSV 가 없습니다: {p}")
                for m in members:
                    target = target_dir / Path(m).name
                    if target.exists():
                        log(f"이미 해제됨, 건너뜀: {target.name}")
                    else:
                        log(f"압축 해제 중: {p.name} -> {target_dir.name}/{target.name}")
                        with zf.open(m) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst, length=1024 * 1024 * 8)
                        log(f"압축 해제 완료: {target.name} ({target.stat().st_size / 1e9:.2f} GB)")
                    csvs.append(target)
        elif p.suffix.lower() == ".csv":
            csvs.append(p)
        else:
            sys.exit(f"지원하지 않는 입력 형식(.zip/.csv 만): {p}")
    return csvs


def check_header(path: Path, expected: list[str]) -> None:
    """CSV 헤더가 기대 스키마와 일치하는지 검증 — 다른 데이터셋 오입력을 초기에 잡는다."""
    with open(path, encoding="utf-8", newline="") as f:
        header = next(csv_mod.reader(f))
    if [h.strip() for h in header] != expected:
        sys.exit(
            f"{path.name} 헤더가 기대 스키마와 다릅니다.\n"
            f"  기대: {expected}\n  실제: {header}\n"
            "  --mode 와 데이터셋이 맞는지 확인하세요."
        )


def sql_str_list(paths: list[Path]) -> str:
    quoted = ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths)
    return f"[{quoted}]"


def quantile_dict(values: list | None) -> dict:
    if not values:
        return {}
    return {f"p{int(q * 1000) / 10:g}": v for q, v in zip(QUANTILES, values)}


def ratio(a, b):
    return round(a / b, 6) if b else None


# ---------------------------------------------------------------------------
# cosmetics — remove_from_cart 행동 통계
# ---------------------------------------------------------------------------

def run_cosmetics(con: duckdb.DuckDBPyConnection, csvs: list[Path]) -> dict:
    log("events 적재 (cosmetics)")
    con.execute(
        f"""
        CREATE TABLE events AS
        SELECT strptime(substr(event_time, 1, 19), '%Y-%m-%d %H:%M:%S') AS event_time,
               event_type, product_id, user_id,
               hash(user_session) AS session_key
        FROM read_csv({sql_str_list(csvs)}, header=true, columns={{
            'event_time': 'VARCHAR', 'event_type': 'VARCHAR',
            'product_id': 'BIGINT', 'category_id': 'BIGINT',
            'category_code': 'VARCHAR', 'brand': 'VARCHAR',
            'price': 'DOUBLE', 'user_id': 'BIGINT', 'user_session': 'VARCHAR'
        }}, strict_mode=false, ignore_errors=true)
        """
    )
    total = con.execute("SELECT count(*) FROM events").fetchone()[0]
    log(f"적재 완료: {total:,}행")

    log("세션 집계·봇 격리")
    con.execute(
        """
        CREATE TABLE session_stats AS
        SELECT session_key, count(*) AS n_events,
               count(*) FILTER (WHERE event_type = 'cart') AS n_cart,
               count(*) FILTER (WHERE event_type = 'remove_from_cart') AS n_remove,
               count(*) FILTER (WHERE event_type = 'purchase') AS n_purchase
        FROM events GROUP BY session_key
        """
    )
    p999 = con.execute("SELECT approx_quantile(n_events, 0.999) FROM session_stats").fetchone()[0]
    threshold = max(BOT_MIN_THRESHOLD, int(math.ceil(p999)))
    con.execute(f"CREATE TABLE bots AS SELECT session_key FROM session_stats WHERE n_events > {threshold}")
    n_bots = con.execute("SELECT count(*) FROM bots").fetchone()[0]
    con.execute(
        """
        CREATE VIEW clean AS
        SELECT e.* FROM events e
        WHERE NOT EXISTS (SELECT 1 FROM bots b WHERE b.session_key = e.session_key)
        """
    )

    stats: dict = {"meta_local": {
        "rows": total,
        "bot_rule": f"세션당 이벤트 수 > max({BOT_MIN_THRESHOLD}, p99.9={p999:.0f}) = {threshold}",
        "bot_sessions_excluded": n_bots,
    }}

    row = con.execute(
        "SELECT min(event_time), max(event_time), count(DISTINCT session_key), count(DISTINCT user_id) FROM clean"
    ).fetchone()
    stats["meta_local"] |= {"event_time_min": str(row[0]), "event_time_max": str(row[1]),
                           "sessions": row[2], "users": row[3]}

    log("이벤트 비율 (4종)")
    counts = dict(con.execute("SELECT event_type, count(*) FROM clean GROUP BY 1").fetchall())
    tot = sum(counts.values())
    stats["event_ratio"] = {et: {"count": c, "ratio": ratio(c, tot)}
                            for et, c in sorted(counts.items(), key=lambda x: -x[1])}
    stats["remove_per_cart"] = ratio(counts.get("remove_from_cart", 0), counts.get("cart", 0))

    log("세션 수준 remove 지표")
    row = con.execute(
        f"""
        SELECT count(*) FILTER (WHERE n_cart > 0),
               count(*) FILTER (WHERE n_cart > 0 AND n_remove > 0),
               count(*) FILTER (WHERE n_remove > 0 AND n_cart = 0),
               approx_quantile(n_remove, {QUANTILES}) FILTER (WHERE n_remove > 0),
               avg(n_remove) FILTER (WHERE n_remove > 0),
               count(*) FILTER (WHERE n_remove > 0 AND n_purchase > 0),
               count(*) FILTER (WHERE n_remove > 0),
               count(*) FILTER (WHERE n_cart > 0 AND n_remove = 0 AND n_purchase > 0),
               count(*) FILTER (WHERE n_cart > 0 AND n_remove = 0)
        FROM session_stats WHERE n_events <= {threshold}
        """
    ).fetchone()
    cart_s, cart_remove_s, remove_only_s, q_rm, avg_rm, rm_buy, rm_s, nc_buy, nc_s = row
    stats["session_level"] = {
        "p_remove_session_given_cart_session": ratio(cart_remove_s, cart_s),
        "remove_without_any_cart_session_share": ratio(remove_only_s, rm_s),
        "removes_per_remove_session": quantile_dict(q_rm) | {"mean": round(avg_rm or 0, 3)},
        "p_purchase_given_remove_session": ratio(rm_buy, rm_s),
        "p_purchase_given_cart_session_no_remove": ratio(nc_buy, nc_s),
        "note": "마지막 두 값 비교 = remove가 구매로 이어지는 세션인지(장바구니 정리) 이탈 신호인지",
    }

    log("상품 단위 remove 타이밍·재담기")
    con.execute(
        """
        CREATE TABLE sp AS
        SELECT session_key, product_id,
               min(CASE WHEN event_type = 'cart' THEN event_time END)             AS first_cart,
               min(CASE WHEN event_type = 'remove_from_cart' THEN event_time END) AS first_remove,
               max(CASE WHEN event_type = 'cart' THEN event_time END)             AS last_cart,
               max(CASE WHEN event_type = 'purchase' THEN event_time END)         AS last_purchase
        FROM clean
        WHERE event_type IN ('cart', 'remove_from_cart', 'purchase')
        GROUP BY 1, 2
        """
    )
    row = con.execute(
        f"""
        SELECT count(*) FILTER (WHERE first_remove IS NOT NULL),
               count(*) FILTER (WHERE first_remove IS NOT NULL AND first_cart IS NOT NULL
                                AND first_remove >= first_cart),
               approx_quantile(date_diff('second', first_cart, first_remove), {QUANTILES})
                   FILTER (WHERE first_remove IS NOT NULL AND first_cart IS NOT NULL
                           AND first_remove >= first_cart),
               count(*) FILTER (WHERE first_remove IS NOT NULL AND last_cart > first_remove),
               count(*) FILTER (WHERE first_remove IS NOT NULL AND last_purchase > first_remove)
        FROM sp
        """
    ).fetchone()
    removed, removed_after_cart, q_ttl, recart, buy_after = row
    stats["product_level"] = {
        "removed_pairs": removed,
        "removed_with_prior_cart_share": ratio(removed_after_cart, removed),
        "seconds_cart_to_remove": quantile_dict(q_ttl),
        "p_recart_same_product_after_remove": ratio(recart, removed),
        "p_purchase_same_product_after_remove": ratio(buy_after, removed),
        "note": "removed_with_prior_cart_share < 1 인 만큼은 이전 세션 장바구니 정리(세션 경계 밖 cart)",
    }
    stats["_summary"] = (
        f"remove/cart={stats['remove_per_cart']}, "
        f"cart세션 중 remove 발생={stats['session_level']['p_remove_session_given_cart_session']}, "
        f"remove 후 재담기={stats['product_level']['p_recart_same_product_after_remove']}"
    )
    return stats


# ---------------------------------------------------------------------------
# orders — 주문 단위 실측 (electronics purchase history)
# ---------------------------------------------------------------------------

def run_orders(con: duckdb.DuckDBPyConnection, csvs: list[Path]) -> dict:
    log("주문 라인 적재 (orders)")
    # kz.csv 데이터 품질 이슈 대응: 컬럼이 모자란 행(null_padding), 표준 미준수 행
    # (strict_mode=false), user_id·category_id 의 지수 표기("1.51e+18") — 전부 VARCHAR 로
    # 읽은 뒤 TRY_CAST 로 정규화한다. 지수 표기는 파일 자체가 정밀도를 잃은 상태라
    # DOUBLE 경유 BIGINT 근사가 최선이다(사용자 구분에는 실용상 충분).
    con.execute(
        f"""
        CREATE TABLE raw_lines AS
        SELECT * FROM read_csv({sql_str_list(csvs)}, header=true, delim=',', quote='"',
            null_padding=true, strict_mode=false, ignore_errors=true, columns={{
            'event_time': 'VARCHAR', 'order_id': 'VARCHAR', 'product_id': 'VARCHAR',
            'category_id': 'VARCHAR', 'category_code': 'VARCHAR', 'brand': 'VARCHAR',
            'price': 'VARCHAR', 'user_id': 'VARCHAR'
        }})
        """
    )
    raw_total = con.execute("SELECT count(*) FROM raw_lines").fetchone()[0]
    con.execute(
        """
        CREATE TABLE lines AS
        SELECT strptime(substr(event_time, 1, 19), '%Y-%m-%d %H:%M:%S') AS event_time,
               coalesce(TRY_CAST(order_id AS BIGINT),
                        TRY_CAST(TRY_CAST(order_id AS DOUBLE) AS BIGINT))   AS order_id,
               coalesce(TRY_CAST(product_id AS BIGINT),
                        TRY_CAST(TRY_CAST(product_id AS DOUBLE) AS BIGINT)) AS product_id,
               nullif(trim(category_code), '') AS category_code,
               nullif(trim(brand), '') AS brand,
               TRY_CAST(price AS DOUBLE) AS price,
               coalesce(TRY_CAST(user_id AS BIGINT),
                        TRY_CAST(TRY_CAST(user_id AS DOUBLE) AS BIGINT))    AS user_id
        FROM raw_lines
        WHERE TRY_CAST(substr(event_time, 1, 4) AS INT) IS NOT NULL
          AND coalesce(TRY_CAST(order_id AS BIGINT),
                       TRY_CAST(TRY_CAST(order_id AS DOUBLE) AS BIGINT)) IS NOT NULL
          AND coalesce(TRY_CAST(product_id AS BIGINT),
                       TRY_CAST(TRY_CAST(product_id AS DOUBLE) AS BIGINT)) IS NOT NULL
        """
    )
    con.execute("DROP TABLE raw_lines")
    total = con.execute("SELECT count(*) FROM lines").fetchone()[0]
    log(f"적재 완료: {total:,}행(주문 라인) — 원본 {raw_total:,}행 중 "
        f"{raw_total - total:,}행 제외(시각/주문/상품 식별 불가)")

    stats: dict = {}
    row = con.execute(
        """
        SELECT min(event_time), max(event_time), count(DISTINCT order_id),
               count(DISTINCT user_id), count(*) FILTER (WHERE user_id IS NULL)
        FROM lines
        """
    ).fetchone()
    t_min, t_max, n_orders, n_users, null_user_rows = row
    period_days = max(1, (t_max - t_min).days)
    stats["meta_local"] = {
        "rows": total, "raw_rows": raw_total,
        "dropped_row_share": ratio(raw_total - total, raw_total),
        "orders": n_orders, "users_non_null": n_users,
        "event_time_min": str(t_min), "event_time_max": str(t_max),
        "period_days": period_days,
        "null_user_row_share": ratio(null_user_rows, total),
    }

    log("주문 구성 (라인·수량·카테고리 교차)")
    con.execute(
        """
        CREATE TABLE orders_agg AS
        SELECT order_id, any_value(user_id) AS user_id, min(event_time) AS t,
               count(*) AS n_lines,
               count(DISTINCT product_id) AS n_products,
               count(DISTINCT category_code) AS n_categories,
               sum(price) AS order_value
        FROM lines GROUP BY order_id
        """
    )
    row = con.execute(
        f"""
        SELECT approx_quantile(n_lines, {QUANTILES}), avg(n_lines),
               approx_quantile(n_products, {QUANTILES}),
               count(*) FILTER (WHERE n_products > 1),
               count(*) FILTER (WHERE n_categories > 1),
               count(*) FILTER (WHERE n_categories >= 1),
               approx_quantile(order_value, {QUANTILES}), avg(order_value),
               count(*)
        FROM orders_agg
        """
    ).fetchone()
    stats["order_composition"] = {
        "lines_per_order": quantile_dict(row[0]) | {"mean": round(row[1], 3)},
        "distinct_products_per_order": quantile_dict(row[2]),
        "multi_product_order_share": ratio(row[3], row[8]),
        "multi_category_order_share_of_categorized": ratio(row[4], row[5]),
        "order_value": quantile_dict(row[6]) | {"mean": round(row[7], 2)},
    }

    row = con.execute(
        f"""
        SELECT approx_quantile(q, {QUANTILES}), avg(q),
               count(*) FILTER (WHERE q > 1), count(*)
        FROM (SELECT count(*) AS q FROM lines GROUP BY order_id, product_id)
        """
    ).fetchone()
    stats["quantity_per_order_product"] = quantile_dict(row[0]) | {
        "mean": round(row[1], 3),
        "share_qty_gt_1": ratio(row[2], row[3]),
        "note": "동일 주문 내 같은 product_id 반복 행 수 = 수량 근사 (이 데이터셋에 수량 컬럼 없음)",
    }

    log("사용자 재구매 간격")
    row = con.execute(
        f"""
        WITH uo AS (
            SELECT user_id, t,
                   lag(t) OVER (PARTITION BY user_id ORDER BY t) AS prev_t
            FROM orders_agg WHERE user_id IS NOT NULL
        )
        SELECT approx_quantile(date_diff('day', prev_t, t), {QUANTILES}),
               avg(date_diff('day', prev_t, t)), count(*)
        FROM uo WHERE prev_t IS NOT NULL
        """
    ).fetchone()
    stats["reorder_gap_days"] = quantile_dict(row[0]) | {
        "mean": round(row[1] or 0, 2), "pairs_measured": row[2],
        "note": f"관측 기간 {period_days}일 내 연속 주문 간격 — 우측 절단(censoring) 있음",
    }
    row = con.execute(
        f"""
        SELECT approx_quantile(c, {QUANTILES}), avg(c),
               count(*) FILTER (WHERE c >= 2), count(*)
        FROM (SELECT user_id, count(*) c FROM orders_agg WHERE user_id IS NOT NULL GROUP BY 1)
        """
    ).fetchone()
    stats["orders_per_user_period"] = quantile_dict(row[0]) | {
        "mean": round(row[1], 3),
        "repeat_buyer_share": ratio(row[2], row[3]),
        "period_days": period_days,
    }

    log("카테고리 동시구매 상위 쌍")
    stats["category_copurchase_top30"] = [
        {"a": a, "b": b, "orders": c}
        for a, b, c in con.execute(
            """
            WITH oc AS (
                SELECT DISTINCT order_id, category_code FROM lines
                WHERE category_code IS NOT NULL
            )
            SELECT x.category_code, y.category_code, count(*) c
            FROM oc x JOIN oc y
              ON x.order_id = y.order_id AND x.category_code < y.category_code
            GROUP BY 1, 2 ORDER BY c DESC LIMIT 30
            """
        ).fetchall()
    ]
    stats["_summary"] = (
        f"주문 {n_orders:,}건, 라인/주문 평균 {stats['order_composition']['lines_per_order']['mean']}, "
        f"수량>1 비율 {stats['quantity_per_order_product']['share_qty_gt_1']}, "
        f"재구매 간격 중앙값 {stats['reorder_gap_days'].get('p50')}일"
    )
    return stats


# ---------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    script_dir = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", required=True, choices=["cosmetics", "orders"])
    ap.add_argument("--input", nargs="+", required=True, help="CSV 또는 zip 경로 (여러 개 가능)")
    ap.add_argument("--output-dir", type=Path, default=script_dir)
    ap.add_argument("--memory-limit", default="4GB")
    ap.add_argument("--max-temp-size", default="30GiB")
    ap.add_argument("--temp-dir", type=Path, default=None,
                    help="스필·작업 DB 폴더 (여유 있는 드라이브 지정 가능)")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--keep-db", action="store_true")
    args = ap.parse_args()

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    csvs = resolve_inputs(args.input)
    expected = EVENT_COLUMNS if args.mode == "cosmetics" else ORDER_COLUMNS
    for c in csvs:
        check_header(c, expected)
    log(f"[{args.mode}] 입력 CSV {len(csvs)}개: " + ", ".join(p.name for p in csvs))

    tmp_dir = (args.temp_dir or (out / "_duckdb_tmp"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / f"_aux_{args.mode}_work.duckdb"
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    con.execute(f"SET memory_limit='{args.memory_limit}'")
    if args.threads > 0:
        con.execute(f"SET threads={args.threads}")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{tmp_dir.as_posix()}'")
    con.execute(f"SET max_temp_directory_size='{args.max_temp_size}'")

    dataset = ("Kaggle mkechinov/ecommerce-events-history-in-cosmetics-shop"
               if args.mode == "cosmetics"
               else "Kaggle mkechinov/ecommerce-purchase-history-from-electronics-store")
    try:
        stats = run_cosmetics(con, csvs) if args.mode == "cosmetics" else run_orders(con, csvs)
        summary = stats.pop("_summary", "")
        payload = {"meta": {"source_files": [p.name for p in csvs], "dataset": dataset,
                            "script": Path(__file__).name, "mode": args.mode}} | stats
        out_file = out / f"stats_{args.mode}.json"
        out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        log(f"완료: {out_file}")
        print(f"\n요약: {summary}")
        print("다음 단계: 결과 JSON 이 data-analysis 폴더에 저장됐는지 확인하고 "
              "Cowork 세션에 알려주세요.")
    finally:
        con.close()
        if not args.keep_db and db_path.exists():
            db_path.unlink()
        if args.temp_dir is None and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
