"""REES46 eCommerce behavior CSV -> jarvis-ai 더미데이터용 통계 추출.

Kaggle "eCommerce behavior data from multi category store"(mkechinov/REES46)의
월별 CSV(2019-Oct.csv 등)를 스트리밍 집계해 두 벌의 통계를 산출한다.

  1차 stats_full.json   : 데이터셋 전수 프로파일 (jarvis-ai 사용 여부 무관, 9개 컬럼 전부)
  2차 stats_jarvis.json : jarvis-ai에서 사용 가능한 데이터만 필터링한 통계
                          (remove_from_cart 제외, 봇/이상치 세션 제외)
  REPORT.md             : 사람용 요약 리포트

사용 예 (PC, PowerShell/cmd):

    pip install duckdb
    python extract_stats.py --input "C:\\Users\\vssea\\Downloads\\2019-Oct.csv.zip"

  - --input 은 .zip 또는 .csv 모두 허용, 여러 개 지정 가능(Oct+Nov 확장 대비).
  - .zip 이면 같은 폴더에 CSV 를 1회 압축 해제한다(디스크 여유 ~6GB 필요).
  - 결과물은 기본적으로 이 스크립트가 있는 폴더(data-analysis)에 저장된다.
  - 작업용 DuckDB 파일(~2GB 내외)은 종료 시 자동 삭제된다(--keep-db 로 보존).

메모리에 CSV 전체를 올리지 않는다 — DuckDB 컬럼나 엔진이 스트리밍/스필 처리한다.
"""

from __future__ import annotations

import argparse
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
ZIPF_TOP_FRACTIONS = [0.01, 0.05, 0.10, 0.20, 0.50]
CATEGORY_MIN_EVENTS = 5000   # 2차 카테고리별 통계 최소 이벤트 수
CATEGORY_LIMIT = 200         # 2차 카테고리별 통계 최대 개수
BOT_MIN_THRESHOLD = 200      # 봇 판정 최소 임계(세션당 이벤트 수)

_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - _T0:7.1f}s] {msg}", flush=True)


def resolve_inputs(paths: list[str]) -> list[Path]:
    """zip 이면 같은 폴더에 1회 해제하고, 최종 CSV 경로 목록을 돌려준다."""
    csvs: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            sys.exit(f"입력 파일이 없습니다: {p}")
        if p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
                if not members:
                    sys.exit(f"zip 안에 CSV 가 없습니다: {p}")
                for m in members:
                    target = p.parent / Path(m).name
                    if target.exists():
                        log(f"이미 해제됨, 건너뜀: {target}")
                    else:
                        log(f"압축 해제 중: {p.name} -> {target.name} (수 분 소요)")
                        with zf.open(m) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst, length=1024 * 1024 * 8)
                        log(f"압축 해제 완료: {target.name} ({target.stat().st_size / 1e9:.2f} GB)")
                    csvs.append(target)
        elif p.suffix.lower() == ".csv":
            csvs.append(p)
        else:
            sys.exit(f"지원하지 않는 입력 형식(.zip/.csv 만): {p}")
    return csvs


def sql_str_list(paths: list[Path]) -> str:
    quoted = ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths)
    return f"[{quoted}]"


def quantile_dict(values: list | None) -> dict[str, float | None]:
    if not values:
        return {}
    return {f"p{int(q * 1000) / 10:g}": v for q, v in zip(QUANTILES, values)}


def fit_zipf_alpha(freqs_desc: list[int]) -> float | None:
    """log(freq) = -alpha*log(rank) + c 최소제곱 적합 (rank 10~10000 구간)."""
    lo, hi = 10, min(len(freqs_desc), 10000)
    if hi - lo < 50:
        return None
    xs, ys = [], []
    for rank in range(lo, hi + 1):
        f = freqs_desc[rank - 1]
        if f <= 0:
            break
        xs.append(math.log(rank))
        ys.append(math.log(f))
    n = len(xs)
    if n < 50:
        return None
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    return round(-slope, 4)


# ---------------------------------------------------------------------------
# 1차: 전수 통계
# ---------------------------------------------------------------------------

def compute_full_stats(con: duckdb.DuckDBPyConnection) -> dict:
    stats: dict = {}

    log("1차 A. 기본 프로파일/품질")
    row = con.execute(
        """
        SELECT count(*), min(event_time), max(event_time),
               count(DISTINCT event_time::DATE),
               count(DISTINCT user_id), count(DISTINCT session_key),
               count(DISTINCT product_id), count(DISTINCT category_id),
               count(DISTINCT category_code), count(DISTINCT brand)
        FROM events
        """
    ).fetchone()
    stats["profile"] = {
        "rows": row[0],
        "event_time_min": str(row[1]),
        "event_time_max": str(row[2]),
        "days_covered": row[3],
        "unique_users": row[4],
        "unique_sessions": row[5],
        "unique_products": row[6],
        "unique_category_ids": row[7],
        "unique_category_codes": row[8],
        "unique_brands": row[9],
        "timezone_note": "event_time 은 UTC 기준",
    }

    nulls = con.execute(
        """
        SELECT count(*) FILTER (WHERE event_time IS NULL),
               count(*) FILTER (WHERE event_type IS NULL),
               count(*) FILTER (WHERE product_id IS NULL),
               count(*) FILTER (WHERE category_id IS NULL),
               count(*) FILTER (WHERE category_code IS NULL),
               count(*) FILTER (WHERE brand IS NULL),
               count(*) FILTER (WHERE price IS NULL),
               count(*) FILTER (WHERE user_id IS NULL),
               count(*) FILTER (WHERE session_key IS NULL),
               count(*)
        FROM events
        """
    ).fetchone()
    total = nulls[-1]
    cols = ["event_time", "event_type", "product_id", "category_id",
            "category_code", "brand", "price", "user_id", "user_session"]
    stats["profile"]["null_ratio"] = {
        c: round(nulls[i] / total, 6) for i, c in enumerate(cols)
    }

    dup = con.execute(
        """
        SELECT count(*) - count(DISTINCT hash(concat_ws(chr(31),
            coalesce(strftime(event_time, '%Y-%m-%d %H:%M:%S'), ''),
            coalesce(event_type, ''), coalesce(product_id::VARCHAR, ''),
            coalesce(category_id::VARCHAR, ''), coalesce(category_code, ''),
            coalesce(brand, ''), coalesce(price::VARCHAR, ''),
            coalesce(user_id::VARCHAR, ''), coalesce(session_key::VARCHAR, ''))))
        FROM events
        """
    ).fetchone()[0]
    stats["profile"]["duplicate_rows_approx"] = dup
    stats["profile"]["duplicate_ratio_approx"] = round(dup / total, 6)

    log("1차 B. 이벤트 타입 비율/일별 추이")
    stats["event_types"] = {
        et: {"count": c, "ratio": round(c / total, 6)}
        for et, c in con.execute(
            "SELECT event_type, count(*) FROM events GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    }
    stats["daily_by_type"] = [
        {"date": str(d), "event_type": et, "count": c}
        for d, et, c in con.execute(
            """
            SELECT event_time::DATE, event_type, count(*)
            FROM events GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()
    ]

    log("1차 C. 사용자/세션 구조")
    row = con.execute(
        """
        SELECT count(*),
               approx_quantile(n_events, {q}),
               avg(n_events),
               approx_quantile(dur_s, {q}),
               avg(dur_s),
               count(*) FILTER (WHERE n_events = 1),
               approx_quantile(n_products, {q}),
               approx_quantile(n_categories, {q})
        FROM session_stats
        """.format(q=QUANTILES)
    ).fetchone()
    stats["sessions"] = {
        "total": row[0],
        "events_per_session": quantile_dict(row[1]) | {"mean": round(row[2], 3)},
        "duration_seconds": quantile_dict(row[3]) | {"mean": round(row[4], 1)},
        "bounce_ratio_single_event": round(row[5] / row[0], 6),
        "distinct_products_per_session": quantile_dict(row[6]),
        "distinct_categories_per_session": quantile_dict(row[7]),
    }
    row = con.execute(
        """
        SELECT count(*), approx_quantile(ns, {q}), avg(ns),
               count(*) FILTER (WHERE ns = 1)
        FROM (SELECT user_id, count(*) ns FROM session_stats GROUP BY 1)
        """.format(q=QUANTILES)
    ).fetchone()
    stats["users"] = {
        "total": row[0],
        "sessions_per_user": quantile_dict(row[1]) | {"mean": round(row[2], 3)},
        "single_session_user_ratio": round(row[3] / row[0], 6),
    }

    log("1차 D. 상품/브랜드/카테고리")
    views = [r[0] for r in con.execute(
        "SELECT count(*) c FROM events WHERE event_type='view' GROUP BY product_id ORDER BY c DESC"
    ).fetchall()]
    tot_views = sum(views)
    cum, shares, idx = 0, {}, 0
    for frac in ZIPF_TOP_FRACTIONS:
        k = max(1, math.ceil(len(views) * frac))
        while idx < k:
            cum += views[idx]
            idx += 1
        shares[f"top_{frac:g}"] = round(cum / tot_views, 6) if tot_views else None
    stats["product_popularity"] = {
        "products_with_views": len(views),
        "view_share_by_top_fraction": shares,
        "zipf_alpha_fit": fit_zipf_alpha(views),
    }
    stats["brand"] = {
        "null_ratio": stats["profile"]["null_ratio"]["brand"],
        "top20_by_events": [
            {"brand": b, "events": c, "share": round(c / total, 6)}
            for b, c in con.execute(
                """
                SELECT brand, count(*) c FROM events WHERE brand IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC LIMIT 20
                """
            ).fetchall()
        ],
    }
    stats["category"] = {
        "code_null_ratio": stats["profile"]["null_ratio"]["category_code"],
        "id_only_ratio": round(con.execute(
            "SELECT count(*) FROM events WHERE category_code IS NULL AND category_id IS NOT NULL"
        ).fetchone()[0] / total, 6),
        "depth_distribution": {
            str(d): c for d, c in con.execute(
                """
                SELECT length(category_code) - length(replace(category_code, '.', '')) + 1 AS depth,
                       count(*)
                FROM events WHERE category_code IS NOT NULL GROUP BY 1 ORDER BY 1
                """
            ).fetchall()
        },
        "top30_by_events": [
            {"category_code": cc, "events": c, "share": round(c / total, 6)}
            for cc, c in con.execute(
                """
                SELECT category_code, count(*) c FROM events WHERE category_code IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC LIMIT 30
                """
            ).fetchall()
        ],
    }

    log("1차 E. 가격")
    row = con.execute(
        """
        SELECT approx_quantile(price, {q}), avg(price),
               count(*) FILTER (WHERE price <= 0),
               count(*) FILTER (WHERE price IS NULL OR price <= 0)
        FROM events
        """.format(q=QUANTILES)
    ).fetchone()
    stats["price"] = {
        "quantiles": quantile_dict(row[0]) | {"mean": round(row[1], 2)},
        "zero_or_negative_rows": row[2],
        "invalid_ratio": round(row[3] / total, 6),
        "by_event_type": {
            et: {"mean": round(m, 2), "median": round(md, 2)}
            for et, m, md in con.execute(
                """
                SELECT event_type, avg(price), median(price)
                FROM events WHERE price > 0 GROUP BY 1
                """
            ).fetchall()
        },
    }

    log("1차 F. 시간 패턴 (UTC)")
    stats["time_pattern_utc"] = [
        {"isodow": d, "hour": h, "count": c}
        for d, h, c in con.execute(
            """
            SELECT isodow(event_time), hour(event_time), count(*)
            FROM events GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()
    ]

    log("1차 G. 4-상태 전이 행렬")
    stats["transition_matrix_4state"] = [
        {"from": a, "to": b, "count": c}
        for a, b, c in con.execute(
            """
            WITH seq AS (
                SELECT event_type et,
                       lead(event_type) OVER (PARTITION BY session_key ORDER BY event_time) nxt
                FROM events
            )
            SELECT et, nxt, count(*) FROM seq WHERE nxt IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC
            """
        ).fetchall()
    ]

    log("1차 H. 이상치/봇 후보")
    p999 = con.execute(
        "SELECT approx_quantile(n_events, 0.999) FROM session_stats"
    ).fetchone()[0]
    threshold = max(BOT_MIN_THRESHOLD, int(math.ceil(p999)))
    row = con.execute(
        f"""
        SELECT count(*), coalesce(sum(n_events), 0)
        FROM session_stats WHERE n_events > {threshold}
        """
    ).fetchone()
    stats["bot_candidates"] = {
        "rule": f"세션당 이벤트 수 > max({BOT_MIN_THRESHOLD}, p99.9={p999:.0f}) = {threshold}",
        "threshold_events_per_session": threshold,
        "sessions_flagged": row[0],
        "events_flagged": row[1],
        "events_flagged_ratio": round(row[1] / total, 6),
        "top10_session_sizes": [
            r[0] for r in con.execute(
                "SELECT n_events FROM session_stats ORDER BY n_events DESC LIMIT 10"
            ).fetchall()
        ],
    }
    stats["_bot_threshold"] = threshold
    return stats


# ---------------------------------------------------------------------------
# 2차: jarvis-ai 사용 가능분 통계
# ---------------------------------------------------------------------------

def compute_jarvis_stats(con: duckdb.DuckDBPyConnection, full: dict) -> dict:
    stats: dict = {}
    total_full = full["profile"]["rows"]

    log("2차 필터 적용 결과 집계")
    total = con.execute("SELECT count(*) FROM jarvis_events").fetchone()[0]
    removed_rfc = full["event_types"].get("remove_from_cart", {}).get("count", 0)
    stats["filters"] = {
        "included_event_types": ["view", "cart", "purchase"],
        "excluded_remove_from_cart_events": removed_rfc,
        "excluded_bot_sessions": full["bot_candidates"]["sessions_flagged"],
        "bot_rule": full["bot_candidates"]["rule"],
        "rows_after_filter": total,
        "rows_retained_ratio_of_full": round(total / total_full, 6),
        "mapping_note": {
            "view": "상품 조회 (FunnelResult/BehaviorEventsResult/프로필 신호)",
            "cart": "AddToCartRequest / 장바구니 퍼널",
            "purchase": "OrderHistory / RecentPurchases / SalesResult",
            "user_session": "sessionId", "user_id": "userId/guestId",
            "product_id": "products.product_id",
            "category_code": "categories 'top > mid' 매핑 대상",
            "brand": "SpringProduct.brandName", "price": "가격/매출 통계",
        },
    }

    log("2차 이벤트 비율")
    stats["event_ratio"] = {
        et: {"count": c, "ratio": round(c / total, 6)}
        for et, c in con.execute(
            "SELECT event_type, count(*) FROM jarvis_events GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    }

    log("2차 세션 퍼널")
    row = con.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE n_view > 0),
               count(*) FILTER (WHERE n_cart > 0),
               count(*) FILTER (WHERE n_purchase > 0),
               count(*) FILTER (WHERE n_view > 0 AND n_cart > 0),
               count(*) FILTER (WHERE n_cart > 0 AND n_purchase > 0),
               count(*) FILTER (WHERE n_purchase > 0 AND n_cart = 0)
        FROM jarvis_session_stats
        """
    ).fetchone()
    sessions, s_view, s_cart, s_buy, s_vc, s_cb, s_direct = row
    stats["session_funnel"] = {
        "sessions": sessions,
        "share_with_view": round(s_view / sessions, 6),
        "share_with_cart": round(s_cart / sessions, 6),
        "share_with_purchase": round(s_buy / sessions, 6),
        "p_cart_given_view": round(s_vc / s_view, 6) if s_view else None,
        "p_purchase_given_cart": round(s_cb / s_cart, 6) if s_cart else None,
        "direct_purchase_share_of_buy_sessions":
            round(s_direct / s_buy, 6) if s_buy else None,
    }
    stats["transition_matrix_3state"] = [
        {"from": a, "to": b, "count": c}
        for a, b, c in con.execute(
            """
            WITH seq AS (
                SELECT event_type et,
                       lead(event_type) OVER (PARTITION BY session_key ORDER BY event_time) nxt
                FROM jarvis_events
            )
            SELECT et, nxt, count(*) FROM seq WHERE nxt IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC
            """
        ).fetchall()
    ]

    log("2차 세션 구조 (구매/비구매 분리)")
    stats["session_structure"] = {}
    for label, cond in [("purchase_sessions", "n_purchase > 0"),
                        ("non_purchase_sessions", "n_purchase = 0")]:
        row = con.execute(
            f"""
            SELECT count(*), approx_quantile(n_events, {QUANTILES}), avg(n_events),
                   approx_quantile(dur_s, {QUANTILES}), avg(dur_s),
                   approx_quantile(n_products, {QUANTILES}),
                   approx_quantile(n_categories, {QUANTILES})
            FROM jarvis_session_stats WHERE {cond}
            """
        ).fetchone()
        stats["session_structure"][label] = {
            "sessions": row[0],
            "events_per_session": quantile_dict(row[1]) | {"mean": round(row[2], 3)},
            "duration_seconds": quantile_dict(row[3]) | {"mean": round(row[4], 1)},
            "distinct_products": quantile_dict(row[5]),
            "distinct_categories": quantile_dict(row[6]),
        }
    row = con.execute(
        f"""
        SELECT approx_quantile(n_purchase, {QUANTILES}), avg(n_purchase)
        FROM jarvis_session_stats WHERE n_purchase > 0
        """
    ).fetchone()
    stats["items_per_order"] = quantile_dict(row[0]) | {
        "mean": round(row[1], 3),
        "note": "구매 세션 내 purchase 이벤트 수 = 주문당 아이템 수 근사. REES46에는 수량 컬럼이 없음(1이벤트=1개).",
    }

    log("2차 사용자 구조")
    row = con.execute(
        f"""
        SELECT count(*), approx_quantile(n_sessions, {QUANTILES}), avg(n_sessions),
               count(*) FILTER (WHERE purchase_sessions > 0),
               count(*) FILTER (WHERE purchase_sessions >= 2)
        FROM user_stats
        """
    ).fetchone()
    users, q_ns, avg_ns, buyers, repeat_buyers = row
    stats["user_structure"] = {
        "users": users,
        "sessions_per_user_month": quantile_dict(q_ns) | {"mean": round(avg_ns, 3)},
        "buyer_ratio": round(buyers / users, 6),
        "repeat_buyer_ratio_of_buyers": round(repeat_buyers / buyers, 6) if buyers else None,
        "period_note": "1개월(입력 파일 기간) 기준 값",
    }
    q = con.execute(
        f"""
        WITH uc AS (
            SELECT user_id, category_code, count(*) c
            FROM jarvis_events WHERE category_code IS NOT NULL GROUP BY 1, 2
        ), ut AS (
            SELECT user_id, sum(c) tot, max(c) mx FROM uc GROUP BY 1
        )
        SELECT approx_quantile(mx * 1.0 / tot, {QUANTILES}), avg(mx * 1.0 / tot), count(*)
        FROM ut WHERE tot >= 5
        """
    ).fetchone()
    stats["user_structure"]["top1_category_share"] = quantile_dict(q[0]) | {
        "mean": round(q[1], 4),
        "users_measured": q[2],
        "note": "이벤트 5건 이상 사용자 기준, 사용자별 최다 카테고리 점유율",
    }

    log("2차 시간 가중치")
    rows = con.execute(
        """
        SELECT isodow(event_time), hour(event_time), count(*)
        FROM jarvis_events GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).fetchall()
    t = sum(r[2] for r in rows)
    stats["time_weights_utc"] = [
        {"isodow": d, "hour": h, "weight": round(c / t, 6)} for d, h, c in rows
    ]

    log("2차 상품 인기(지프)")
    views = [r[0] for r in con.execute(
        "SELECT count(*) c FROM jarvis_events WHERE event_type='view' GROUP BY product_id ORDER BY c DESC"
    ).fetchall()]
    tot_views = sum(views)
    cum, shares, idx = 0, {}, 0
    for frac in ZIPF_TOP_FRACTIONS:
        k = max(1, math.ceil(len(views) * frac))
        while idx < k:
            cum += views[idx]
            idx += 1
        shares[f"top_{frac:g}"] = round(cum / tot_views, 6) if tot_views else None
    stats["product_popularity"] = {
        "products_with_views": len(views),
        "view_share_by_top_fraction": shares,
        "zipf_alpha_fit": fit_zipf_alpha(views),
        "transfer_note": "jarvis 카탈로그(~10k 상품)에 순위 기반 전사용 파라미터",
    }

    log("2차 카테고리별 상세")
    stats["categories"] = [
        {
            "category_code": cc, "events": ev, "share": round(ev / total, 6),
            "views": v, "carts": c, "purchases": p,
            "cart_per_view": round(c / v, 6) if v else None,
            "purchase_per_cart": round(p / c, 6) if c else None,
            "purchase_per_view": round(p / v, 6) if v else None,
            "price_p25": round(p25, 2) if p25 is not None else None,
            "price_median": round(md, 2) if md is not None else None,
            "price_p75": round(p75, 2) if p75 is not None else None,
        }
        for cc, ev, v, c, p, p25, md, p75 in con.execute(
            f"""
            SELECT category_code, count(*) ev,
                   count(*) FILTER (WHERE event_type = 'view') v,
                   count(*) FILTER (WHERE event_type = 'cart') c,
                   count(*) FILTER (WHERE event_type = 'purchase') p,
                   approx_quantile(price, 0.25) FILTER (WHERE price > 0),
                   approx_quantile(price, 0.5) FILTER (WHERE price > 0),
                   approx_quantile(price, 0.75) FILTER (WHERE price > 0)
            FROM jarvis_events
            WHERE category_code IS NOT NULL
            GROUP BY 1
            HAVING count(*) >= {CATEGORY_MIN_EVENTS}
            ORDER BY ev DESC
            LIMIT {CATEGORY_LIMIT}
            """
        ).fetchall()
    ]
    stats["categories_note"] = (
        f"이벤트 {CATEGORY_MIN_EVENTS}건 이상 카테고리 상위 {CATEGORY_LIMIT}개. "
        "전환율은 이벤트 수 비율 기준(세션 기준 아님)."
    )

    log("2차 가격-전환 상호작용")
    stats["price_conversion"] = [
        {
            "price_quintile": qt, "price_min": round(mn, 2), "price_max": round(mx, 2),
            "views": v, "purchases": b,
            "purchase_per_view": round(b / v, 6) if v else None,
        }
        for qt, mn, mx, v, b in con.execute(
            """
            WITH prod AS (
                SELECT product_id, median(price) mp,
                       count(*) FILTER (WHERE event_type = 'view') v,
                       count(*) FILTER (WHERE event_type = 'purchase') b
                FROM jarvis_events WHERE price > 0 GROUP BY 1
            ), ranked AS (
                SELECT *, ntile(5) OVER (ORDER BY mp) qt FROM prod WHERE v > 0
            )
            SELECT qt, min(mp), max(mp), sum(v), sum(b) FROM ranked GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
    ]
    return stats


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------

def build_report(full: dict, jv: dict, sources: list[str]) -> str:
    p = full["profile"]
    ev = full["event_types"]
    bots = full["bot_candidates"]
    sf = jv["session_funnel"]
    us = jv["user_structure"]

    def pct(x, nd=2):
        return f"{x * 100:.{nd}f}%" if x is not None else "-"

    lines = [
        "# REES46 통계 추출 리포트",
        "",
        f"- 입력: {', '.join(sources)}",
        f"- 기간: {p['event_time_min']} ~ {p['event_time_max']} (UTC, {p['days_covered']}일)",
        f"- 총 이벤트 {p['rows']:,}건 · 사용자 {p['unique_users']:,}명 · 세션 {p['unique_sessions']:,}개 · 상품 {p['unique_products']:,}종",
        "",
        "## 1차 — 전수 통계 (stats_full.json)",
        "",
        "| 이벤트 | 건수 | 비율 |",
        "|---|---:|---:|",
    ]
    for et, d in ev.items():
        lines.append(f"| {et} | {d['count']:,} | {pct(d['ratio'])} |")
    lines += [
        "",
        f"- 결측률: category_code {pct(p['null_ratio']['category_code'])}, "
        f"brand {pct(p['null_ratio']['brand'])} (나머지 컬럼은 stats_full.json 참고)",
        f"- 중복 행(근사): {p['duplicate_rows_approx']:,} ({pct(p['duplicate_ratio_approx'], 3)})",
        f"- 바운스(이벤트 1건) 세션: {pct(full['sessions']['bounce_ratio_single_event'])}",
        f"- 봇 후보: {bots['rule']} → 세션 {bots['sessions_flagged']:,}개, "
        f"이벤트 {bots['events_flagged']:,}건({pct(bots['events_flagged_ratio'], 3)}) 격리",
        "",
        "## 2차 — jarvis-ai 사용 가능분 (stats_jarvis.json)",
        "",
        f"- 필터 후 잔존: {jv['filters']['rows_after_filter']:,}건 "
        f"({pct(jv['filters']['rows_retained_ratio_of_full'])} of 전체) — "
        "remove_from_cart·봇 세션 제외",
        "",
        "| 항목 | 값 |",
        "|---|---:|",
        f"| view : cart : purchase | "
        f"{' : '.join(pct(jv['event_ratio'][k]['ratio']) for k in ('view', 'cart', 'purchase') if k in jv['event_ratio'])} |",
        f"| 세션 중 cart 발생 | {pct(sf['share_with_cart'])} |",
        f"| 세션 중 purchase 발생 | {pct(sf['share_with_purchase'])} |",
        f"| p(cart \\| view 세션) | {pct(sf['p_cart_given_view'])} |",
        f"| p(purchase \\| cart 세션) | {pct(sf['p_purchase_given_cart'])} |",
        f"| cart 없이 바로 구매한 구매세션 | {pct(sf['direct_purchase_share_of_buy_sessions'])} |",
        f"| 구매 전환 사용자 비율(월) | {pct(us['buyer_ratio'])} |",
        f"| 구매자 중 월내 재구매 | {pct(us['repeat_buyer_ratio_of_buyers'])} |",
        f"| 주문당 아이템 수(평균) | {jv['items_per_order']['mean']} |",
        f"| 상위 10% 상품의 조회 점유율 | {pct(jv['product_popularity']['view_share_by_top_fraction'].get('top_0.1'))} |",
        f"| 지프 alpha 적합 | {jv['product_popularity']['zipf_alpha_fit']} |",
        "",
        "### 다음 단계",
        "",
        "1. stats_jarvis.json 의 카테고리별 통계를 11번가 카테고리 트리에 매핑 (mapping.json)",
        "2. 더미 생성기: 세션 퍼널 전이확률 + 시간 가중치 + 지프 분포로 이벤트/주문 생성",
        "3. 생성 더미 역검증: 위 표의 값과 비교",
        "",
        "제한: REES46 에 구매 수량 없음(주문당 아이템 수는 purchase 이벤트 수로 근사), "
        "1개월 데이터라 재방문·재구매율은 월간 기준.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    script_dir = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", nargs="+", required=True,
                    help="REES46 CSV 또는 zip 경로 (여러 개 가능)")
    ap.add_argument("--output-dir", type=Path, default=script_dir,
                    help="결과물 저장 폴더 (기본: 스크립트 폴더)")
    ap.add_argument("--memory-limit", default="4GB", help="DuckDB 메모리 상한 (기본 4GB)")
    ap.add_argument("--max-temp-size", default="30GiB",
                    help="DuckDB 스필 디스크 상한 (기본 30GiB — 실제로는 드라이브 여유 공간까지만 사용)")
    ap.add_argument("--temp-dir", type=Path, default=None,
                    help="스필 임시 폴더 (기본: output-dir 밑. 여유 있는 다른 드라이브 지정 가능)")
    ap.add_argument("--threads", type=int, default=0, help="DuckDB 스레드 수 (0=자동)")
    ap.add_argument("--keep-db", action="store_true", help="작업용 DuckDB 파일 보존")
    args = ap.parse_args()

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    csvs = resolve_inputs(args.input)
    log(f"입력 CSV {len(csvs)}개: " + ", ".join(p.name for p in csvs))

    tmp_dir = (args.temp_dir or (out / "_duckdb_tmp"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / "_extract_stats_work.duckdb"  # 무거운 파일은 전부 temp-dir 쪽에
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    con.execute(f"SET memory_limit='{args.memory_limit}'")
    if args.threads > 0:
        con.execute(f"SET threads={args.threads}")
    # 정렬 순서 보존 해제 — 집계 전용 워크로드라 안전하며 메모리/스필 사용량을 크게 줄인다.
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{tmp_dir.as_posix()}'")
    con.execute(f"SET max_temp_directory_size='{args.max_temp_size}'")

    try:
        log("events 테이블 적재 (CSV 1회 스캔, 수 분 소요)")
        con.execute(
            f"""
            CREATE TABLE events AS
            SELECT strptime(substr(event_time, 1, 19), '%Y-%m-%d %H:%M:%S') AS event_time,
                   event_type, product_id, category_id,
                   nullif(trim(category_code), '') AS category_code,
                   nullif(trim(brand), '') AS brand,
                   price, user_id,
                   hash(user_session) AS session_key  -- UUID 문자열 대신 8B 해시(디스크 절약)
            FROM read_csv({sql_str_list(csvs)}, header=true, columns={{
                'event_time': 'VARCHAR', 'event_type': 'VARCHAR',
                'product_id': 'BIGINT', 'category_id': 'BIGINT',
                'category_code': 'VARCHAR', 'brand': 'VARCHAR',
                'price': 'DOUBLE', 'user_id': 'BIGINT', 'user_session': 'VARCHAR'
            }})
            """
        )
        n = con.execute("SELECT count(*) FROM events").fetchone()[0]
        log(f"적재 완료: {n:,}행")

        log("session_stats 집계")
        con.execute(
            """
            CREATE TABLE session_stats AS
            SELECT session_key, any_value(user_id) AS user_id, count(*) AS n_events,
                   date_diff('second', min(event_time), max(event_time)) AS dur_s,
                   count(DISTINCT product_id) AS n_products,
                   count(DISTINCT category_code) AS n_categories
            FROM events GROUP BY session_key
            """
        )

        full = compute_full_stats(con)
        threshold = full.pop("_bot_threshold")

        log("2차용 필터 뷰 구성")
        con.execute(
            f"""
            CREATE TABLE bot_sessions AS
            SELECT session_key FROM session_stats WHERE n_events > {threshold}
            """
        )
        con.execute(
            """
            CREATE VIEW jarvis_events AS
            SELECT e.* FROM events e
            WHERE e.event_type IN ('view', 'cart', 'purchase')
              AND NOT EXISTS (SELECT 1 FROM bot_sessions b
                              WHERE b.session_key = e.session_key)
            """
        )
        con.execute(
            """
            CREATE TABLE jarvis_session_stats AS
            SELECT session_key, any_value(user_id) AS user_id, count(*) AS n_events,
                   count(*) FILTER (WHERE event_type = 'view') AS n_view,
                   count(*) FILTER (WHERE event_type = 'cart') AS n_cart,
                   count(*) FILTER (WHERE event_type = 'purchase') AS n_purchase,
                   date_diff('second', min(event_time), max(event_time)) AS dur_s,
                   count(DISTINCT product_id) AS n_products,
                   count(DISTINCT category_code) AS n_categories
            FROM jarvis_events GROUP BY session_key
            """
        )
        con.execute(
            """
            CREATE TABLE user_stats AS
            SELECT user_id, count(*) AS n_sessions,
                   count(*) FILTER (WHERE n_purchase > 0) AS purchase_sessions,
                   sum(n_events) AS n_events
            FROM jarvis_session_stats GROUP BY user_id
            """
        )

        jv = compute_jarvis_stats(con, full)

        meta = {
            "source_files": [p.name for p in csvs],
            "dataset": "Kaggle mkechinov/ecommerce-behavior-data-from-multi-category-store (REES46)",
            "script": Path(__file__).name,
        }
        full_out = {"meta": meta} | full
        jv_out = {"meta": meta} | jv

        (out / "stats_full.json").write_text(
            json.dumps(full_out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (out / "stats_jarvis.json").write_text(
            json.dumps(jv_out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (out / "REPORT.md").write_text(
            build_report(full, jv, [p.name for p in csvs]), encoding="utf-8")
        log(f"완료: {out / 'stats_full.json'}")
        log(f"완료: {out / 'stats_jarvis.json'}")
        log(f"완료: {out / 'REPORT.md'}")
        print("\n다음 단계: 이 세 파일이 jarvis-ai\\data-analysis\\ 에 저장되었는지 확인하고 "
              "Cowork 세션에 '실행 완료'라고 알려주세요.")
    finally:
        con.close()
        if not args.keep_db and db_path.exists():
            db_path.unlink()
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
