"""jarvis-ai 테스트용 더미데이터 생성기 (#128)

REES46 실측 통계 3종(stats_jarvis / stats_cosmetics / stats_orders)을 파라미터로,
시드(10_category / 20_brand / 30_product)를 참조해 로컬 테스트용 더미 풀세트를 생성한다.

산출물 (out-dir):
  35_product_brand_fill.sql   brand_id NULL 상품 661개 UPDATE 패치
  40_member.sql / .csv
  41_guest.sql / .csv
  42_address.sql / .csv
  43_behavior_events.sql / .csv
  44_orders.sql, 44_order_item.sql, 44_order_status_logs.sql / .csv
  45_cart_item.sql / .csv
  README_LOAD_ORDER.md        적재 순서
  VALIDATION.md               역검증 리포트 (앵커 대비)

사용 예 (PC):
    uv run python generate_dummy.py ^
        --seed-dir "C:\\path\\to\\seed_sql" --stats-dir . --out-dir dummy_out

설계 근거: data-analysis/HANDOFF.md + 승인된 상세 설계 (2026-08-04).
- 실측 3종(view/cart/purchase 계열)이 앵커, 나머지 이벤트는 모순 없게 합성
- 카테고리 매핑 없음 — 시드 leaf category_id는 유효 FK 공급원
- remove_from_cart 기본 OFF (E-1 화이트리스트 미등록)
- 구매는 회원 전용(D30), 게스트 구매 세션은 login + converted_member_id 전환
- 재고 차감·claim 미반영 (테스트 범위 외, README에 명시)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import uuid
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ------------------------------------------------------------------
# 설정 (전부 CLI로 덮어쓰기 가능)
# ------------------------------------------------------------------
DEFAULTS = dict(
    rng_seed=42,
    period_start="2026-07-01",          # KST, 포함
    period_days=35,
    members=300,
    guests=900,
    guest_convert_ratio=0.15,           # 게스트 중 회원 전환 비율
    active_products=1500,               # 트래픽 대상 상품 풀
    zipf_alpha=0.8835,                  # stats_jarvis.product_popularity.zipf_alpha_fit
    remove_from_cart=False,             # E-1 화이트리스트 미등록 — 기본 OFF
    remove_per_cart=0.25,               # ON일 때 remove/cart 양 (cosmetics 실측 0.685는 과함)
    search_session_ratio=0.25,
    p_purchase_given_checkout=0.60,     # p(checkout|cart)×p(purchase|checkout)=0.509 분해
    price_slope_damp=0.5,               # 가격분위 전환 보정 기울기 감쇠 (전자몰→종합몰)
    # ID 베이스 — 기존 시드/운영 행과 충돌 방지
    member_id_base=100000,
    address_id_base=100000,
    order_id_base=500000,
    order_item_id_base=700000,
    oslog_id_base=900000,
    cart_item_id_base=300000,
)

# 주문상태 8종 분포 (사용자 확정 2026-08-04) — 주문 시점과 연동해 배정
ORDER_STATUS_DIST = [
    ("DELIVERED", 0.62), ("SHIPPED", 0.10), ("PREPARING", 0.07), ("PAID", 0.06),
    ("PAYMENT_FAILED", 0.05), ("CANCELLED", 0.05), ("RETURNED", 0.03), ("PENDING", 0.02),
]
# 상태별 "주문 경과일" 요건: (최소경과일, 최대경과일) — None=제약 없음
STATUS_AGE = {
    "PENDING": (0, 2), "PAID": (0, 3), "PREPARING": (0, 4), "SHIPPED": (1, 6),
    "DELIVERED": (3, None), "RETURNED": (7, None), "CANCELLED": (0, None),
    "PAYMENT_FAILED": (0, None),
}

PAGE_TYPES = dict(  # E-1 pageType 14종과 이름 정렬 필요 — README에 명시
    home="HOME", list="PRODUCT_LIST", detail="PRODUCT_DETAIL", search="SEARCH_RESULT",
    cart="CART", checkout="CHECKOUT", complete="ORDER_COMPLETE", login="LOGIN",
)

NICK_A = ["행복한", "졸린", "달리는", "포근한", "신나는", "조용한", "빠른", "느긋한", "귀여운", "씩씩한",
          "새벽의", "한가한", "다정한", "엉뚱한", "성실한", "슬기로운", "용감한", "수줍은", "재빠른", "느린"]
NICK_B = ["감자", "고양이", "판다", "수달", "복숭아", "라쿤", "두더지", "펭귄", "토끼", "호랑이",
          "미어캣", "알파카", "고래", "여우", "다람쥐", "부엉이", "너구리", "곰돌이", "치타", "돌고래"]
SURNAMES = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권", "황"]
GIVEN = ["민준", "서연", "지우", "하윤", "도윤", "은우", "수아", "지호", "예은", "시우",
         "채원", "준서", "유나", "건우", "소율", "지안", "현우", "서준", "다은", "예준"]
CITIES = [
    ("서울특별시", ["강남구 테헤란로", "마포구 월드컵북로", "송파구 올림픽로", "성동구 왕십리로", "관악구 남부순환로"], "0"),
    ("경기도", ["성남시 분당구 판교역로", "수원시 영통구 광교중앙로", "고양시 일산동구 중앙로", "용인시 수지구 포은대로"], "1"),
    ("부산광역시", ["해운대구 센텀중앙로", "부산진구 중앙대로", "수영구 광안해변로"], "4"),
    ("대전광역시", ["유성구 대학로", "서구 둔산로"], "3"),
    ("대구광역시", ["수성구 달구벌대로", "중구 동성로"], "4"),
]
DELIVERY_REQ = [None, None, None, "문 앞에 놓아주세요", "경비실에 맡겨주세요", "배송 전 연락주세요", "부재 시 문자 주세요"]
FAIL_REASONS = ["CARD_DECLINED", "INSUFFICIENT_FUNDS", "PG_TIMEOUT"]
CANCEL_REASONS = ["단순 변심", "주문 실수", "배송 지연 예상"]
RETURN_REASONS = ["상품 불량", "사이즈/색상 불만족", "설명과 다른 상품"]

SEARCH_SUFFIX = ["", " 추천", " 세일", " 인기", " 후기"]


# ------------------------------------------------------------------
# SQL 시드 파서
# ------------------------------------------------------------------
def parse_tuples(sql_text: str):
    import re
    out, n = [], len(sql_text)
    for m in re.finditer(r"INSERT INTO `\w+` \([^)]*\) VALUES", sql_text):
        i = m.end()
        while i < n:
            while i < n and sql_text[i] in " \r\n\t,":
                i += 1
            if i >= n or sql_text[i] != "(":
                break
            depth, j, in_str = 0, i, False
            fields, cur = [], []
            while j < n:
                c = sql_text[j]
                if in_str:
                    if c == "\\":
                        cur.append(sql_text[j + 1])
                        j += 2
                        continue
                    if c == "'":
                        if j + 1 < n and sql_text[j + 1] == "'":
                            cur.append("'")
                            j += 2
                            continue
                        in_str = False
                        j += 1
                        continue
                    cur.append(c)
                    j += 1
                    continue
                if c == "'":
                    in_str = True
                    j += 1
                    continue
                if c == "(":
                    depth += 1
                    if depth > 1:
                        cur.append(c)
                    j += 1
                    continue
                if c == ")":
                    depth -= 1
                    if depth == 0:
                        fields.append("".join(cur))
                        j += 1
                        break
                    cur.append(c)
                    j += 1
                    continue
                if c == "," and depth == 1:
                    fields.append("".join(cur))
                    cur = []
                    j += 1
                    continue
                cur.append(c)
                j += 1
            out.append(fields)
            i = j
            while i < n and sql_text[i] in " \r\n\t":
                i += 1
            if i < n and sql_text[i] == ";":
                i += 1
                break
    return out


def nval(x):
    x = x.strip()
    return None if x == "NULL" else x


# ------------------------------------------------------------------
# 분위수 테이블 → 역CDF 샘플러 (실측 분포 재현의 공통 도구)
# ------------------------------------------------------------------
class QuantileSampler:
    def __init__(self, qdict, lo=None, hi=None, integer=True):
        pts = []
        for k, v in qdict.items():
            if not k.startswith("p"):
                continue
            try:
                p = float(k[1:]) / 100.0
            except ValueError:
                continue
            pts.append((p, float(v)))
        pts.sort()
        if lo is not None:
            pts.insert(0, (0.0, lo))
        if hi is not None:
            pts.append((1.0, hi))
        self.ps = [p for p, _ in pts]
        self.vs = [v for _, v in pts]
        self.integer = integer

    def sample(self, rng: random.Random):
        u = rng.random()
        i = bisect_left(self.ps, u)
        if i <= 0:
            v = self.vs[0]
        elif i >= len(self.ps):
            v = self.vs[-1]
        else:
            p0, p1 = self.ps[i - 1], self.ps[i]
            v0, v1 = self.vs[i - 1], self.vs[i]
            t = 0 if p1 == p0 else (u - p0) / (p1 - p0)
            v = v0 + t * (v1 - v0)
        return max(1, int(round(v))) if self.integer else v


def pick_weighted(rng, pairs):
    u = rng.random()
    acc = 0.0
    for k, w in pairs:
        acc += w
        if u <= acc:
            return k
    return pairs[-1][0]


# ------------------------------------------------------------------
# SQL/CSV 출력 도우미
# ------------------------------------------------------------------
def sql_lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, datetime):
        if v.microsecond:
            return "'" + v.strftime("%Y-%m-%d %H:%M:%S.%f") + "'"
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    s = str(v).replace("\\", "\\\\").replace("'", "''")
    return "'" + s + "'"


def write_sql(path: Path, table: str, cols, rows, batch=500, header=""):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"-- {table}: {len(rows):,} rows (generated dummy — seed 고정 재현 가능)\n")
        if header:
            f.write(header)
        f.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\nSET autocommit=0;\n\n")
        col_sql = ",".join(f"`{c}`" for c in cols)
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            f.write(f"INSERT INTO `{table}` ({col_sql}) VALUES\n")
            f.write(",\n".join("(" + ",".join(sql_lit(v) for v in r) + ")" for r in chunk))
            f.write(";\n")
        f.write("\nCOMMIT;\nSET FOREIGN_KEY_CHECKS=1;\nSET UNIQUE_CHECKS=1;\n")


def write_csv(path: Path, cols, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else (v.strftime("%Y-%m-%d %H:%M:%S.%f") if isinstance(v, datetime) else v) for v in r])


# ------------------------------------------------------------------
# 메인 생성기
# ------------------------------------------------------------------
@dataclass
class Ctx:
    cfg: dict
    rng: random.Random
    stats: dict
    products: list = field(default_factory=list)     # dict(id, brand, cat, root, name, price, orig, stock)
    cats: dict = field(default_factory=dict)
    brands: dict = field(default_factory=dict)
    active: list = field(default_factory=list)       # 활성 풀 (지프 가중치 순)
    zipf_cum: list = field(default_factory=list)


def load_seeds(ctx: Ctx, seed_dir: Path):
    cat_rows = parse_tuples((seed_dir / "10_category.sql").read_text(encoding="utf-8"))
    for r in cat_rows:
        ctx.cats[int(r[0])] = dict(parent=int(nval(r[1])) if nval(r[1]) else None, name=r[2])
    children = defaultdict(list)
    for cid, c in ctx.cats.items():
        children[c["parent"]].append(cid)

    def root_of(cid):
        while ctx.cats[cid]["parent"] is not None:
            cid = ctx.cats[cid]["parent"]
        return cid

    brand_rows = parse_tuples((seed_dir / "20_brand.sql").read_text(encoding="utf-8"))
    for r in brand_rows:
        ctx.brands[int(r[0])] = r[2]

    prod_rows = parse_tuples((seed_dir / "30_product.sql").read_text(encoding="utf-8"))
    for r in prod_rows:
        cid = int(r[2])
        ctx.products.append(dict(
            id=int(r[0]),
            brand=int(r[1]) if nval(r[1]) else None,
            cat=cid, root=root_of(cid), leaf_name=ctx.cats[cid]["name"],
            name=r[3], orig=int(r[4]), price=int(r[5]), stock=int(r[6]),
        ))


def build_brand_fill(ctx: Ctx):
    """brand_id 보정 2종.

    ① ci-중복 브랜드 리맵: uk_brand_name 이 case-insensitive 콜레이션이라 이름의 대소문자만
       다른 브랜드('Alo'/'alo'/'ALO' 등)는 DB에 첫 행만 살아남는다. 탈락 id를 참조하는 상품을
       생존(canonical, 파일상 첫 등장) id로 리맵한다.
    ② NULL 채우기: ⓐ 상품명 [브랜드] 프리픽스 매칭 ⓑ 같은 leaf의 기존 브랜드 ⓒ 랜덤.
    """
    import re
    canonical = {}      # lower(name) -> 첫 등장 brand_id
    remap = {}          # 탈락 brand_id -> canonical brand_id
    for bid, bname in ctx.brands.items():   # dict는 파일 순서 유지 (py3.7+)
        key = bname.strip().lower()
        if key in canonical:
            remap[bid] = canonical[key]
        else:
            canonical[key] = bid
    name_to_brand = {}
    for bid, bname in ctx.brands.items():
        if bid not in remap:
            name_to_brand[bname.lower().replace(" ", "")] = bid
    leaf_brands = defaultdict(list)
    for p in ctx.products:
        if p["brand"] in remap:
            p["brand"] = remap[p["brand"]]
        if p["brand"] is not None:
            leaf_brands[p["cat"]].append(p["brand"])
    all_brands = [b for b in ctx.brands if b not in remap]
    updates = []
    for p in ctx.products:
        if p["brand"] is not None:
            continue
        bid = None
        m = re.match(r"^[\[\(]([^\]\)]{1,20})[\]\)]", p["name"])
        if m:
            key = m.group(1).lower().replace(" ", "")
            bid = name_to_brand.get(key)
        if bid is None and leaf_brands.get(p["cat"]):
            bid = ctx.rng.choice(leaf_brands[p["cat"]])
        if bid is None:
            bid = ctx.rng.choice(all_brands)
        p["brand"] = bid
        updates.append((p["id"], bid))
    return updates, remap


def build_active_pool(ctx: Ctx):
    """대분류 비례로 활성 상품 풀 선별 + 지프 가중치."""
    rng, cfg = ctx.rng, ctx.cfg
    by_root = defaultdict(list)
    for p in ctx.products:
        by_root[p["root"]].append(p)
    total = len(ctx.products)
    pool = []
    for root, plist in by_root.items():
        k = max(1, round(cfg["active_products"] * len(plist) / total))
        pool.extend(rng.sample(plist, min(k, len(plist))))
    rng.shuffle(pool)
    pool = pool[:cfg["active_products"]]
    ctx.active = pool
    a = cfg["zipf_alpha"]
    w = [(i + 1) ** (-a) for i in range(len(pool))]
    s = sum(w)
    acc, cum = 0.0, []
    for x in w:
        acc += x / s
        cum.append(acc)
    ctx.zipf_cum = cum


def pick_product(ctx: Ctx, prefer_roots=None, purchasable=False, focus_leaf=None):
    rng = ctx.rng
    for _ in range(60):
        u = rng.random()
        i = bisect_left(ctx.zipf_cum, u)
        p = ctx.active[min(i, len(ctx.active) - 1)]
        if purchasable and (p["price"] <= 0 or p["stock"] <= 0):
            continue
        if focus_leaf is not None and p["cat"] != focus_leaf and rng.random() < 0.7:
            continue
        if prefer_roots and p["root"] not in prefer_roots and rng.random() < 0.79:
            continue  # top1 카테고리 점유 ~79% 재현
        return p
    return ctx.active[0]


def hour_weights_kst(stats):
    """UTC 168칸 → '하루 모양'만 취해 KST 시간대 가중치로 사용 (HANDOFF 확정)."""
    by_dow = defaultdict(float)
    by_hour = defaultdict(float)
    for x in stats["time_weights_utc"]:
        by_dow[x["isodow"]] += x["weight"]
        by_hour[x["hour"]] += x["weight"]
    sd = sum(by_dow.values())
    sh = sum(by_hour.values())
    dow_w = [(d, by_dow[d] / sd) for d in sorted(by_dow)]
    hour_w = [(h, by_hour[h] / sh) for h in sorted(by_hour)]
    return dow_w, hour_w


def sample_session_start(ctx, dow_w, hour_w, day0: datetime, days: int):
    rng = ctx.rng
    for _ in range(200):
        day = rng.randrange(days)
        d = day0 + timedelta(days=day)
        if rng.random() > dict(dow_w)[d.isoweekday()] * 7 * 0.9 + 0.1:
            continue
        h = pick_weighted(rng, hour_w)
        return d.replace(hour=h, minute=rng.randrange(60), second=rng.randrange(60))
    return day0 + timedelta(days=rng.randrange(days), hours=20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-dir", required=True)
    ap.add_argument("--stats-dir", required=True)
    ap.add_argument("--out-dir", default="dummy_out")
    for k, v in DEFAULTS.items():
        t = type(v)
        if t is bool:
            ap.add_argument(f"--{k.replace('_','-')}", default=v, action="store_true" if not v else "store_false")
        else:
            ap.add_argument(f"--{k.replace('_','-')}", default=v, type=t)
    args = ap.parse_args()
    cfg = {k: getattr(args, k) for k in DEFAULTS}
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    rng = random.Random(cfg["rng_seed"])
    stats = json.loads((Path(args.stats_dir) / "stats_jarvis.json").read_text(encoding="utf-8"))
    stats_o = json.loads((Path(args.stats_dir) / "stats_orders.json").read_text(encoding="utf-8"))

    ctx = Ctx(cfg=cfg, rng=rng, stats=stats)
    load_seeds(ctx, Path(args.seed_dir))
    brand_updates, brand_remap = build_brand_fill(ctx)
    build_active_pool(ctx)

    day0 = datetime.strptime(cfg["period_start"], "%Y-%m-%d")
    days = cfg["period_days"]
    period_end = day0 + timedelta(days=days)
    dow_w, hour_w = hour_weights_kst(stats)

    us = stats["user_structure"]
    fun = stats["session_funnel"]
    ss = stats["session_structure"]
    sess_per_user = QuantileSampler(us["sessions_per_user_month"], lo=1, hi=25)
    ev_purchase = QuantileSampler(ss["purchase_sessions"]["events_per_session"], lo=2, hi=45)
    ev_nonpurch = QuantileSampler(ss["non_purchase_sessions"]["events_per_session"], lo=1, hi=35)
    dur_purchase = QuantileSampler(ss["purchase_sessions"]["duration_seconds"], lo=40, hi=5400, integer=False)
    dur_nonpurch = QuantileSampler(ss["non_purchase_sessions"]["duration_seconds"], lo=0, hi=3600, integer=False)
    # 라인/주문: 실측 pmf 근사 (P(1)=1-다품목 0.392, 평균 1.835, p75=2·p90=4·p99=8 정렬)
    LINES_PMF = [(1, 0.608), (2, 0.185), (3, 0.085), (4, 0.047), (5, 0.030),
                 (6, 0.020), (7, 0.012), (8, 0.008), (9, 0.003), (13, 0.002)]

    def sample_lines():
        return pick_weighted(rng, LINES_PMF)
    reorder_gap = QuantileSampler({k: v for k, v in stats_o["reorder_gap_days"].items()
                                   if k.startswith("p") and k not in ("p99", "p99.9")}, lo=1, hi=30)

    price_q = sorted(p["price"] for p in ctx.products if p["price"] > 0)
    qb = [price_q[int(len(price_q) * q) - 1] for q in (0.2, 0.4, 0.6, 0.8)]
    conv = [0.0132, 0.0136, 0.0134, 0.0155, 0.0200]
    conv_mean = sum(conv) / 5
    damp = cfg["price_slope_damp"]
    price_mult = [1 + damp * (c / conv_mean - 1) for c in conv]

    def price_bias(p):
        i = bisect_left(qb, p["price"])
        return price_mult[min(i, 4)]

    # ---------------- ① member ----------------
    members = []
    bcrypt_hash = "$2a$10$dummyDummyDummyDummyDuOqzed7uEIWmVn8DummyTestHashAAAAA"  # 전원 동일 테스트 해시
    for i in range(cfg["members"]):
        mid = cfg["member_id_base"] + i + 1
        if rng.random() < 0.7:
            created = day0 - timedelta(days=rng.randrange(1, 365), hours=rng.randrange(24))
        else:
            created = day0 + timedelta(days=rng.randrange(days), hours=rng.randrange(24))
        birth = datetime(rng.randrange(1975, 2007), rng.randrange(1, 13), rng.randrange(1, 29))
        members.append(dict(
            id=mid, email=f"user{i+1:04d}@dummy.test", password=bcrypt_hash,
            nickname=f"{rng.choice(NICK_A)}{rng.choice(NICK_B)}{i+1:03d}",
            role="USER", gender=rng.choice(["MALE", "FEMALE"]),
            birth_date=birth.date(), created_at=created,
        ))

    # ---------------- ④ 프로필 ----------------
    roots = sorted({p["root"] for p in ctx.products})
    root_weights = defaultdict(int)
    for p in ctx.products:
        root_weights[p["root"]] += 1
    rw_pairs = [(r, root_weights[r] / len(ctx.products)) for r in roots]

    def assign_profile(created_at):
        prefer = {pick_weighted(rng, rw_pairs)}
        if rng.random() < 0.35:
            prefer.add(pick_weighted(rng, rw_pairs))
        n_sess = sess_per_user.sample(rng)
        return dict(prefer=prefer, n_sess=n_sess, created=created_at)

    m_profiles = {m["id"]: assign_profile(m["created_at"]) for m in members}
    # 구매자 수 앵커: 전체 활동 주체(회원+게스트)의 11.5%가 구매 — 구매는 회원 전용(D30)이므로
    # 회원 내 구매자 비율 = 11.5% × (회원+게스트)/회원. 세션 단위 구매 비중(6.8%)도 자동 근접.
    buyer_ratio_members = min(0.85, us["buyer_ratio"] * (cfg["members"] + cfg["guests"]) / cfg["members"])
    buyers = set(m["id"] for m in members if rng.random() < buyer_ratio_members)
    # ×1.25 보정: 기간 후반 가입자 등 일정 비율의 재구매 세션이 기간 밖으로 잘리는 손실 보전
    repeat_buyers = set(b for b in buyers if rng.random() < us["repeat_buyer_ratio_of_buyers"] * 1.05)

    guests = []
    g_profiles = {}
    convert_pool = []
    for i in range(cfg["guests"]):
        gid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        guests.append(dict(id=gid, converted=None, created_at=None))
        prof = assign_profile(None)
        prof["n_sess"] = min(prof["n_sess"], 4)  # 게스트는 장기 재방문 적음
        g_profiles[gid] = prof
        if rng.random() < cfg["guest_convert_ratio"]:
            convert_pool.append(gid)

    # 전환 게스트 → 신규 회원 매핑 (기간 중 가입 회원과 연결)
    inperiod_members = [m for m in members if m["created_at"] >= day0]
    rng.shuffle(inperiod_members)
    g_to_m = {}
    for gid, m in zip(convert_pool, inperiod_members):
        g_to_m[gid] = m["id"]

    # ---------------- ③ address ----------------
    addresses = []
    aid = cfg["address_id_base"]
    member_addr = {}
    for m in members:
        aid += 1
        city, streets, zp = rng.choice(CITIES)
        name = rng.choice(SURNAMES) + rng.choice(GIVEN)
        phone = f"010-{rng.randrange(2000,10000)}-{rng.randrange(1000,10000)}"
        a = dict(id=aid, member_id=m["id"], label="집", recipient=name, phone=phone,
                 zip_code=f"{zp}{rng.randrange(1000,10000)}",
                 address1=f"{city} {rng.choice(streets)} {rng.randrange(1,300)}",
                 address2=f"{rng.randrange(101,2500)}호", is_default=True,
                 created_at=m["created_at"] + timedelta(minutes=rng.randrange(5, 600)))
        addresses.append(a)
        member_addr[m["id"]] = a
        if rng.random() < 0.2:
            aid += 1
            city2, streets2, zp2 = rng.choice(CITIES)
            addresses.append(dict(id=aid, member_id=m["id"], label="회사", recipient=name, phone=phone,
                                  zip_code=f"{zp2}{rng.randrange(1000,10000)}",
                                  address1=f"{city2} {rng.choice(streets2)} {rng.randrange(1,300)}",
                                  address2=f"{rng.randrange(1,30)}층", is_default=False,
                                  created_at=a["created_at"] + timedelta(days=rng.randrange(1, 30))))

    # ---------------- ⑤~⑧ 세션 → 이벤트 → 주문 ----------------
    events = []          # behavior_events rows(dict)
    orders = []
    order_items = []
    oslogs = []
    carts = defaultdict(dict)   # actor_key -> {(product_id): dict(qty, added_at, updated)}
    cart_events_count = 0

    oid = cfg["order_id_base"]
    oiid = cfg["order_item_id_base"]
    olid = cfg["oslog_id_base"]

    def emit(sess, etype, t, product=None, props=None, member_id=None, guest_id=None):
        occurred = t
        created = t + timedelta(seconds=rng.uniform(0.5, 5.0))
        events.append(dict(
            member_id=member_id, guest_id=guest_id, session_key=sess,
            client_event_id=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            event_type=etype, product_id=product["id"] if product else None,
            properties=json.dumps(props, ensure_ascii=False) if props else None,
            occurred_at=occurred, created_at=created,
        ))

    def status_logs_for(order, item_names):
        nonlocal olid
        chain = []
        t = order["created_at"]
        chain.append((None, "PENDING", "SYSTEM", None, t))
        st = order["status"]
        if st == "PENDING":
            pass
        elif st == "PAYMENT_FAILED":
            chain.append(("PENDING", "PAYMENT_FAILED", "SYSTEM", rng.choice(FAIL_REASONS), t + timedelta(seconds=rng.randrange(20, 120))))
        else:
            tp = t + timedelta(seconds=rng.randrange(30, 300))
            chain.append(("PENDING", "PAID", "SYSTEM", None, tp))
            order["paid_at"] = tp
            seq = ["PREPARING", "SHIPPED", "DELIVERED"]
            target_idx = dict(PAID=-1, PREPARING=0, SHIPPED=1, DELIVERED=2, RETURNED=2, CANCELLED=-1)[st]
            prev = "PAID"
            tt = tp
            for k in range(target_idx + 1):
                tt = tt + timedelta(hours=rng.uniform(8, 40))
                chain.append((prev, seq[k], "SYSTEM", None, tt))
                prev = seq[k]
            if st == "CANCELLED":
                chain.append(("PAID", "CANCELLED", "USER", rng.choice(CANCEL_REASONS), tp + timedelta(hours=rng.uniform(0.2, 12))))
            if st == "RETURNED":
                chain.append(("DELIVERED", "RETURNED", "USER", rng.choice(RETURN_REASONS), tt + timedelta(days=rng.uniform(1, 5))))
        rows = []
        for fr, to, actor, reason, tt in chain:
            olid += 1
            rows.append(dict(id=olid, order_id=order["id"], from_status=fr, to_status=to,
                             actor_type=actor, reason=reason, created_at=tt))
        return rows

    def pick_order_status(order_time):
        # 경과일로 불가능한 상태의 확률 질량을 '진행 단계상 인접한' 가능 상태에 몰아준다.
        # (재정규화하면 상시 가능한 CANCELLED/PAYMENT_FAILED가 과대 표집되는 문제 회피 —
        #  전역 분포에서 이 둘은 목표치 그대로 유지되고, 잉여 질량은 DELIVERED 등으로 흡수)
        age = (period_end - order_time).days
        elig = {s for s, _ in ORDER_STATUS_DIST
                if age >= STATUS_AGE[s][0] and (STATUS_AGE[s][1] is None or age <= STATUS_AGE[s][1])}
        if not elig:
            return "DELIVERED" if age > 5 else "PAID"
        pairs = [(s, w) for s, w in ORDER_STATUS_DIST if s in elig]
        deficit = 1.0 - sum(w for _, w in pairs)
        for absorber in ("DELIVERED", "SHIPPED", "PREPARING", "PAID", "PENDING"):
            if absorber in elig:
                pairs = [(s, w + (deficit if s == absorber else 0)) for s, w in pairs]
                break
        return pick_weighted(rng, pairs)

    def make_order(member_id, sess_key, t, focus_leaf, prefer):
        nonlocal oid, oiid
        n_lines = sample_lines()
        chosen, seen = [], set()
        first = pick_product(ctx, prefer, purchasable=True, focus_leaf=focus_leaf)
        chosen.append(first)
        seen.add(first["id"])
        for _ in range(n_lines - 1):
            for _ in range(30):
                q = pick_product(ctx, {first["root"]}, purchasable=True)
                if q["id"] not in seen:
                    chosen.append(q)
                    seen.add(q["id"])
                    break
        oid += 1
        addr = member_addr[member_id]
        st = pick_order_status(t)
        order = dict(id=oid, member_id=member_id, status=st,
                     payment_method="MOCK_FAIL" if st == "PAYMENT_FAILED" else "MOCK_CARD",
                     total_amount=sum(p["price"] for p in chosen),
                     recipient=addr["recipient"], phone=addr["phone"], zip_code=addr["zip_code"],
                     address1=addr["address1"], address2=addr["address2"],
                     delivery_request=rng.choice(DELIVERY_REQ),
                     paid_at=None, created_at=t + timedelta(seconds=rng.randrange(5, 60)))
        logs = status_logs_for(order, [p["name"] for p in chosen])
        item_status = {"PENDING": "ORDERED", "PAYMENT_FAILED": "ORDERED", "PAID": "ORDERED",
                       "PREPARING": "PREPARING", "SHIPPED": "SHIPPED", "DELIVERED": "DELIVERED",
                       "CANCELLED": "CANCELLED", "RETURNED": "RETURNED"}[order["status"]]
        st_changed = logs[-1]["created_at"]
        for p in chosen:
            oiid += 1
            order_items.append(dict(id=oiid, order_id=oid, product_id=p["id"],
                                    product_name=p["name"][:200], option_name=None,
                                    price=p["price"], original_price=p["orig"], quantity=1,
                                    status=item_status, status_changed_at=st_changed,
                                    created_at=order["created_at"]))
        orders.append(order)
        oslogs.extend(logs)
        return order, chosen

    # --- 세션 스케줄 구성: 회원 ---
    schedule = []  # (actor_type, actor_id, start_dt, is_purchase, prof)
    for m in members:
        prof = m_profiles[m["id"]]
        n = prof["n_sess"]
        earliest = max(day0, m["created_at"])
        if earliest >= period_end:
            continue
        avail = (period_end - earliest).days or 1
        n_buy = 0
        if m["id"] in buyers:
            n_buy = 1
            if m["id"] in repeat_buyers:
                n_buy += 1 + (1 if rng.random() < 0.35 else 0)
        n = max(n, n_buy)
        starts = []
        t0 = sample_session_start(ctx, dow_w, hour_w, earliest, avail)
        starts.append(t0)
        tries = 0
        while len(starts) < n and tries < 200:
            tries += 1
            if len(starts) < n_buy and n_buy > 1:  # 재구매 간격 반영
                gap = reorder_gap.sample(rng)
                t = starts[-1] + timedelta(days=gap, hours=rng.uniform(-4, 4))
                if t >= period_end:  # 기간 밖이면 균등 재배치로 폴백
                    t = sample_session_start(ctx, dow_w, hour_w, earliest, avail)
            else:
                t = sample_session_start(ctx, dow_w, hour_w, earliest, avail)
            if day0 <= t < period_end:
                starts.append(t)
        starts.sort()
        buy_idx = set()
        if n_buy:
            # 구매 세션은 뒤쪽에 배치 후 재구매 간격 검증
            idxs = sorted(rng.sample(range(len(starts)), n_buy))
            buy_idx = set(idxs)
        for i, s in enumerate(starts):
            schedule.append(("member", m["id"], s, i in buy_idx, prof))

    # --- 세션 스케줄: 게스트 (구매 불가, 전환 게스트는 마지막 세션에서 login·전환) ---
    for g in guests:
        prof = g_profiles[g["id"]]
        n = prof["n_sess"]
        starts = sorted(sample_session_start(ctx, dow_w, hour_w, day0, days) for _ in range(n))
        g["created_at"] = starts[0]
        mid = g_to_m.get(g["id"])
        for i, s in enumerate(starts):
            is_convert = (mid is not None and i == n - 1)
            schedule.append(("guest_convert" if is_convert else "guest", (g["id"], mid), s, False, prof))

    schedule.sort(key=lambda x: x[2])

    # --- 세션 실행 ---
    leaf_counts = defaultdict(int)
    for p in ctx.products:
        leaf_counts[p["cat"]] += 1
    guest_by_id = {g["id"]: g for g in guests}
    sess_counter = 0
    n_purchase_sessions = sum(1 for s in schedule if s[3])
    # checkout 이탈 세션 수: p(purchase|checkout)=0.60 → 이탈 = 구매세션 × (1/0.6 - 1)
    n_abandon_target = round(n_purchase_sessions * (1 / cfg["p_purchase_given_checkout"] - 1))
    abandon_left = n_abandon_target

    for actor_type, actor_id, start, is_purchase, prof in schedule:
        sess_counter += 1
        sess = f"dummy-{sess_counter:06d}-{rng.getrandbits(32):08x}"
        if actor_type == "member":
            mid, gid = actor_id, None
        elif actor_type == "guest":
            mid, gid = None, actor_id[0]
        else:  # guest_convert — 승계 백필(D5) 이후 상태를 재현: 둘 다 채움
            gid, mid = actor_id[0], actor_id[1]

        prefer = prof["prefer"]
        focus = None
        t = start
        emit(sess, "session_start", t, props={"ipHash": f"{rng.getrandbits(64):016x}"},
             member_id=mid if actor_type == "member" else (mid if actor_type == "guest_convert" else None),
             guest_id=gid)
        t += timedelta(seconds=rng.uniform(1, 5))
        emit(sess, "page_view", t, props={"pageType": PAGE_TYPES["home"]},
             member_id=mid if actor_type != "guest" else None, guest_id=gid)

        if is_purchase:
            n_ev = ev_purchase.sample(rng)
            duration = dur_purchase.sample(rng)
        else:
            # 바운스 35.4%를 직접 앵커로: 1건 세션 확률을 명시하고, 나머지는 ≥2 조건부 샘플
            if rng.random() < 0.354:
                n_ev = 1
            else:
                n_ev = ev_nonpurch.sample(rng)
                for _ in range(20):
                    if n_ev >= 2:
                        break
                    n_ev = ev_nonpurch.sample(rng)
                n_ev = max(2, n_ev)
            duration = max(5.0, dur_nonpurch.sample(rng)) if n_ev > 1 else rng.uniform(3, 40)

        has_search = rng.random() < cfg["search_session_ratio"] and n_ev >= 2
        # 세션 중 cart 발생 6.2% 앵커: 구매세션의 카트(1-직접구매) 기여분을 빼고
        # 비구매·비바운스 세션에 배정할 확률을 동적 산출
        buy_share_est = n_purchase_sessions / max(len(schedule), 1)
        cart_from_buy = buy_share_est * (1 - fun["direct_purchase_share_of_buy_sessions"])
        eligible_share = (1 - buy_share_est) * (1 - 0.354)
        p_cart_eligible = max(0.0, (fun["share_with_cart"] - cart_from_buy) / max(eligible_share, 1e-9))
        wants_cart = (not is_purchase) and n_ev >= 2 and rng.random() < p_cart_eligible
        direct_buy = is_purchase and rng.random() < fun["direct_purchase_share_of_buy_sessions"]

        gap = min(duration / max(n_ev, 1), 1400.0)  # 이벤트 간격 상한 <30분 — session_key 재발급 규칙 준수
        mid_ev = mid if actor_type != "guest" else None

        if has_search:
            t += timedelta(seconds=rng.uniform(1, gap))
            leaf_pick = pick_product(ctx, prefer)
            q = leaf_pick["leaf_name"] + rng.choice(SEARCH_SUFFIX)
            n_res = leaf_counts[leaf_pick["cat"]]
            emit(sess, "page_view", t, props={"pageType": PAGE_TYPES["search"]}, member_id=mid_ev, guest_id=gid)
            emit(sess, "search", t + timedelta(milliseconds=300), props={"query": q, "resultsCount": n_res},
                 member_id=mid_ev, guest_id=gid)
            focus = leaf_pick["cat"]

        viewed = []
        n_views = max(1, n_ev - (2 if is_purchase else (1 if wants_cart else 0)))
        for _ in range(n_views):
            t += timedelta(seconds=rng.uniform(2, min(gap * 1.5, 1500.0)))
            if t >= start + timedelta(seconds=max(duration, 30)):
                t = max(t - timedelta(seconds=rng.uniform(1, 20)), start)
            cand = pick_product(ctx, prefer, focus_leaf=focus)
            if rng.random() < price_bias(cand) - 1 + 1:  # price_bias는 구매 후보 선택에서 반영
                pass
            emit(sess, "page_view", t, props={"pageType": PAGE_TYPES["detail"]}, member_id=mid_ev, guest_id=gid)
            emit(sess, "product_view", t + timedelta(milliseconds=500), cand,
                 props={"price": cand["price"]}, member_id=mid_ev, guest_id=gid)
            viewed.append(cand)
            if focus is None and rng.random() < 0.6:
                focus = cand["cat"]

        actor_key = ("m", mid) if actor_type == "member" else ("g", gid)

        def add_cart(p, tt):
            nonlocal cart_events_count
            emit(sess, "page_view", tt, props={"pageType": PAGE_TYPES["cart"]}, member_id=mid_ev, guest_id=gid)
            emit(sess, "add_to_cart", tt + timedelta(milliseconds=400), p,
                 props={"quantity": 1, "price": p["price"]}, member_id=mid_ev, guest_id=gid)
            cart_events_count += 1
            c = carts[actor_key].get(p["id"])
            if c:
                c["qty"] += 1
                c["updated"] = tt
            else:
                carts[actor_key][p["id"]] = dict(qty=1, added_at=tt, updated=None, product=p)

        if wants_cart:
            picks = [p for p in viewed if p["price"] > 0] or [pick_product(ctx, prefer, purchasable=True)]
            # 가격분위 전환 보정: 고분위 상품이 카트 후보로 뽑힐 확률 가중
            weights = [price_bias(p) for p in picks]
            n_cart_items = 2 if rng.random() < 0.4 else 1  # 실측 cart세션당 cart 이벤트 ~1.6
            carted = []
            for _ in range(n_cart_items):
                p = rng.choices(picks, weights=weights)[0]
                t += timedelta(seconds=rng.uniform(2, gap))
                add_cart(p, t)
                carted.append(p)
            if abandon_left > 0:
                abandon_left -= 1
                t += timedelta(seconds=rng.uniform(5, gap))
                amt = sum(p["price"] for p in carted)
                emit(sess, "page_view", t, props={"pageType": PAGE_TYPES["checkout"]}, member_id=mid_ev, guest_id=gid)
                emit(sess, "checkout_start", t + timedelta(milliseconds=400),
                     props={"amount": amt, "productIds": [p["id"] for p in carted]},
                     member_id=mid_ev, guest_id=gid)

        if is_purchase:
            # 회원 전용. login 이벤트: 세션의 절반은 재로그인 수행
            if rng.random() < 0.5:
                t += timedelta(seconds=rng.uniform(2, gap))
                emit(sess, "page_view", t, props={"pageType": PAGE_TYPES["login"]}, member_id=mid, guest_id=gid)
                emit(sess, "login", t + timedelta(milliseconds=600), member_id=mid, guest_id=gid)
            order, chosen = make_order(mid, sess, t + timedelta(seconds=rng.uniform(3, gap)), focus, prefer)
            tt = order["created_at"] - timedelta(seconds=rng.uniform(2, 20))
            if not direct_buy:
                for p in chosen:
                    add_cart(p, tt - timedelta(seconds=rng.uniform(3, 30)))
            emit(sess, "page_view", tt, props={"pageType": PAGE_TYPES["checkout"]}, member_id=mid, guest_id=gid)
            emit(sess, "checkout_start", tt + timedelta(milliseconds=500),
                 props={"amount": order["total_amount"], "productIds": [p["id"] for p in chosen]},
                 member_id=mid, guest_id=gid)
            if order["status"] != "PAYMENT_FAILED":
                emit(sess, "page_view", order["created_at"] + timedelta(seconds=1),
                     props={"pageType": PAGE_TYPES["complete"]}, member_id=mid, guest_id=gid)
                emit(sess, "purchase_complete", order["created_at"] + timedelta(seconds=2),
                     props={"orderId": order["id"], "amount": order["total_amount"]},
                     member_id=mid, guest_id=gid)
                # 구매된 라인은 카트에서 제거
                for p in chosen:
                    carts[actor_key].pop(p["id"], None)

        if actor_type == "guest_convert":
            t += timedelta(seconds=rng.uniform(2, 10))
            emit(sess, "page_view", t, props={"pageType": PAGE_TYPES["login"]}, member_id=mid, guest_id=gid)
            emit(sess, "login", t + timedelta(milliseconds=500), member_id=mid, guest_id=gid)
            g = guest_by_id[gid]
            g["converted"] = mid
            # 게스트 장바구니 병합 승계 (D30)
            gc = carts.pop(("g", gid), {})
            mc = carts[("m", mid)]
            for pid_, c in gc.items():
                if pid_ in mc:
                    mc[pid_]["qty"] += c["qty"]
                    mc[pid_]["updated"] = c["added_at"]
                else:
                    mc[pid_] = c

    # ---------------- 출력 rows ----------------
    member_rows = [(m["id"], m["email"], m["password"], m["nickname"], m["role"], m["gender"],
                    m["birth_date"], m["created_at"], m["created_at"], m["created_at"], None) for m in members]
    guest_rows = [(g["id"], g["converted"], g["created_at"] or day0, None) for g in guests]
    addr_rows = [(a["id"], a["member_id"], a["label"], a["recipient"], a["phone"], a["zip_code"],
                  a["address1"], a["address2"], a["is_default"], a["created_at"], None) for a in addresses]
    ev_rows = [(e["member_id"], e["guest_id"], e["session_key"], e["client_event_id"], e["event_type"],
                e["product_id"], e["properties"], e["occurred_at"], None, None, None, None, e["created_at"])
               for e in sorted(events, key=lambda x: x["occurred_at"])]
    order_rows = [(o["id"], o["member_id"], o["status"], o["payment_method"], o["total_amount"],
                   o["recipient"], o["phone"], o["zip_code"], o["address1"], o["address2"],
                   o["delivery_request"], o["paid_at"], o["created_at"], None) for o in orders]
    oi_rows = [(x["id"], x["order_id"], x["product_id"], x["product_name"], x["option_name"], x["price"],
                x["original_price"], x["quantity"], x["status"], x["status_changed_at"], x["created_at"], None)
               for x in order_items]
    ol_rows = [(x["id"], x["order_id"], x["from_status"], x["to_status"], x["actor_type"], x["reason"],
                x["created_at"]) for x in oslogs]
    cart_rows = []
    cid_ = cfg["cart_item_id_base"]
    for (kind, key), items in carts.items():
        for pid_, c in items.items():
            cid_ += 1
            cart_rows.append((cid_, key if kind == "m" else None, key if kind == "g" else None,
                              pid_, None, c["qty"], c["added_at"], c["updated"]))

    # ---------------- 파일 쓰기 ----------------
    write_sql(out / "35_product_brand_fill.sql", "product", [], [],
              header="")  # 별도 UPDATE 형식이라 아래에서 직접 씀
    with open(out / "35_product_brand_fill.sql", "w", encoding="utf-8") as f:
        f.write("-- product.brand_id 보정 (시드 미비 — 승인: 2026-08-04)\n")
        f.write(f"-- ① ci-중복 브랜드 리맵 {len(brand_remap)}건: uk_brand_name 은 case-insensitive 라\n")
        f.write("--    대소문자만 다른 브랜드 행은 적재 시 탈락한다. 탈락 id 참조 상품을 생존 id로 리맵.\n")
        f.write("--    (탈락 브랜드가 실제로 없는 DB에서만 발동 — NOT EXISTS 가드)\n")
        f.write(f"-- ② NULL 채움 {len(brand_updates)}건: 상품명 [브랜드] 매칭 → 같은 leaf 브랜드 → 랜덤\n")
        f.write("--    (관대 모드 적재로 NULL 이 0 으로 들어간 경우까지 커버)\n")
        f.write("SET NAMES utf8mb4;\nSET autocommit=0;\n\n")
        for old, new in sorted(brand_remap.items()):
            f.write(f"UPDATE `product` SET `brand_id`={new} WHERE `brand_id`={old} "
                    f"AND NOT EXISTS (SELECT 1 FROM `brand` b WHERE b.`id`={old});\n")
        f.write("\n")
        for pid_, bid in brand_updates:
            f.write(f"UPDATE `product` SET `brand_id`={bid} WHERE `id`={pid_} "
                    f"AND (`brand_id` IS NULL OR `brand_id`=0);\n")
        f.write("\nCOMMIT;\n")

    write_sql(out / "40_member.sql", "member",
              ["id", "email", "password", "nickname", "role", "gender", "birth_date",
               "agreed_terms_at", "agreed_privacy_at", "created_at", "updated_at"], member_rows)
    write_sql(out / "41_guest.sql", "guest",
              ["id", "converted_member_id", "created_at", "updated_at"], guest_rows)
    write_sql(out / "42_address.sql", "address",
              ["id", "member_id", "label", "recipient", "phone", "zip_code", "address1", "address2",
               "is_default", "created_at", "updated_at"], addr_rows)
    write_sql(out / "43_behavior_events.sql", "behavior_events",
              ["member_id", "guest_id", "session_key", "client_event_id", "event_type", "product_id",
               "properties", "occurred_at", "recommendation_request_id", "list_id", "surface", "position",
               "created_at"], ev_rows)
    write_sql(out / "44_orders.sql", "orders",
              ["id", "member_id", "status", "payment_method", "total_amount", "recipient", "phone",
               "zip_code", "address1", "address2", "delivery_request", "paid_at", "created_at", "updated_at"],
              order_rows)
    write_sql(out / "44_order_item.sql", "order_item",
              ["id", "order_id", "product_id", "product_name", "option_name", "price", "original_price",
               "quantity", "status", "status_changed_at", "created_at", "updated_at"], oi_rows)
    write_sql(out / "44_order_status_logs.sql", "order_status_logs",
              ["id", "order_id", "from_status", "to_status", "actor_type", "reason", "created_at"], ol_rows)
    write_sql(out / "45_cart_item.sql", "cart_item",
              ["id", "member_id", "guest_id", "product_id", "option_id", "quantity", "created_at",
               "updated_at"], cart_rows)

    for name, cols, rows in [
        ("40_member", ["id", "email", "password", "nickname", "role", "gender", "birth_date",
                       "agreed_terms_at", "agreed_privacy_at", "created_at", "updated_at"], member_rows),
        ("41_guest", ["id", "converted_member_id", "created_at", "updated_at"], guest_rows),
        ("42_address", ["id", "member_id", "label", "recipient", "phone", "zip_code", "address1",
                        "address2", "is_default", "created_at", "updated_at"], addr_rows),
        ("43_behavior_events", ["member_id", "guest_id", "session_key", "client_event_id", "event_type",
                                "product_id", "properties", "occurred_at", "recommendation_request_id",
                                "list_id", "surface", "position", "created_at"], ev_rows),
        ("44_orders", ["id", "member_id", "status", "payment_method", "total_amount", "recipient", "phone",
                       "zip_code", "address1", "address2", "delivery_request", "paid_at", "created_at",
                       "updated_at"], order_rows),
        ("44_order_item", ["id", "order_id", "product_id", "product_name", "option_name", "price",
                           "original_price", "quantity", "status", "status_changed_at", "created_at",
                           "updated_at"], oi_rows),
        ("44_order_status_logs", ["id", "order_id", "from_status", "to_status", "actor_type", "reason",
                                  "created_at"], ol_rows),
        ("45_cart_item", ["id", "member_id", "guest_id", "product_id", "option_id", "quantity",
                          "created_at", "updated_at"], cart_rows),
    ]:
        write_csv(out / f"{name}.csv", cols, rows)

    # ---------------- 역검증 ----------------
    from collections import Counter
    et = Counter(e["event_type"] for e in events)
    core3 = et["product_view"] + et["add_to_cart"] + et["purchase_complete"]
    sess_events = defaultdict(list)
    for e in events:
        sess_events[e["session_key"]].append(e)
    n_sess = len(sess_events)
    core_by_sess = {}
    for k, evs in sess_events.items():
        c = Counter(x["event_type"] for x in evs)
        core_by_sess[k] = c
    cart_sess = sum(1 for c in core_by_sess.values() if c["add_to_cart"] > 0)
    buy_sess = sum(1 for c in core_by_sess.values() if c["purchase_complete"] > 0)
    checkout_sess = sum(1 for c in core_by_sess.values() if c["checkout_start"] > 0)
    direct = sum(1 for c in core_by_sess.values() if c["purchase_complete"] > 0 and c["add_to_cart"] == 0)
    core_counts = [c["product_view"] + c["add_to_cart"] + c["purchase_complete"] for c in core_by_sess.values()]
    bounce = sum(1 for x in core_counts if x <= 1) / n_sess
    buyers_actual = len(set(o["member_id"] for o in orders))
    rep = Counter(o["member_id"] for o in orders)
    repeat_actual = sum(1 for v in rep.values() if v > 1)
    lines = Counter(x["order_id"] for x in order_items)
    mean_lines = sum(lines.values()) / len(lines) if lines else 0
    multi = sum(1 for v in lines.values() if v > 1) / len(lines) if lines else 0
    total_actors = cfg["members"] + cfg["guests"]
    # Zipf 재적합 (간단 로그 회귀)
    pv = Counter(e["product_id"] for e in events if e["event_type"] == "product_view")
    ranked = sorted(pv.values(), reverse=True)
    ranked = [v for v in ranked if v >= 2][: max(50, len(ranked) // 5)]  # 소표본 꼬리 노이즈 제거 후 적합
    xs = [math.log(i + 1) for i in range(len(ranked))]
    ys = [math.log(v) for v in ranked]
    n_ = len(xs)
    if n_ > 10:
        mx, my = sum(xs) / n_, sum(ys) / n_
        alpha_fit = -sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    else:
        alpha_fit = float("nan")
    st_dist = Counter(o["status"] for o in orders)

    def row(name, anchor, got, note=""):
        if isinstance(anchor, (int, float)) and anchor:
            dev = abs(got - anchor) / abs(anchor)
            flag = "PASS" if dev <= 0.10 else ("WARN" if dev <= 0.25 else "FAIL")
        else:
            flag = "-"
        return f"| {name} | {anchor} | {round(got,4) if isinstance(got,float) else got} | {flag} | {note} |\n"

    v = ["# VALIDATION — 생성 더미 vs 실측 앵커\n\n",
         f"- 생성 규모: 회원 {cfg['members']} · 게스트 {cfg['guests']} · {cfg['period_days']}일 "
         f"({cfg['period_start']}~) · 세션 {n_sess:,} · behavior_events {len(events):,} · "
         f"주문 {len(orders):,} · order_item {len(order_items):,} · cart_item 잔존 {len(cart_rows):,}\n",
         f"- random seed = {cfg['rng_seed']} (재현 가능)\n\n",
         "| 지표 | 앵커(실측) | 생성값 | 판정 | 비고 |\n|---|---:|---:|---|---|\n"]
    v.append(row("view 비율(3종 내)", 0.9606, et["product_view"] / core3))
    v.append(row("cart 비율(3종 내)", 0.0218, et["add_to_cart"] / core3))
    v.append(row("purchase 비율(3종 내)", None, et["purchase_complete"] / core3,
                 "참고용 — purchase_complete=주문당 1건(E-1) vs 실측=라인당 1건. "
                 "실측 0.0175는 라인 기준이므로 주문 기준은 그보다 낮은 게 정상"))
    core3_lines = et["product_view"] + et["add_to_cart"] + len(order_items)
    v.append(row("purchase 비율(라인 환산)", None, len(order_items) / core3_lines,
                 "참고용 — REES46 items/order 1.18 vs kz 라인/주문 1.84의 앵커 충돌로 "
                 "설계상 주문 구성(1.84)을 우선(확정 결정 §6). 0.0175와 0.026 사이면 정상"))
    v.append(row("세션 중 cart 발생", 0.0620, cart_sess / n_sess))
    v.append(row("세션 중 purchase 발생", 0.0681, buy_sess / n_sess, "게스트 구매 불가 반영 보정"))
    v.append(row("p(purchase|checkout 세션)", cfg["p_purchase_given_checkout"],
                 buy_sess / checkout_sess if checkout_sess else 0))
    v.append(row("직접구매 비중(구매세션)", 0.5364, direct / buy_sess if buy_sess else 0))
    v.append(row("바운스(핵심이벤트≤1)", 0.3537, bounce))
    v.append(row("구매전환(주체, 월)", 0.1149, buyers_actual / total_actors,
                 "전체 주체(회원+게스트) 기준"))
    v.append(row("구매자 중 재구매", 0.3217, repeat_actual / buyers_actual if buyers_actual else 0))
    v.append(row("라인/주문 평균", 1.835, mean_lines))
    v.append(row("다품목 주문 비중", 0.3924, multi))
    v.append(row("Zipf alpha 재적합", cfg["zipf_alpha"], alpha_fit, "점유율 비교는 N 축소로 무의미 — α로 비교"))
    v.append("\n## 주문 상태 분포 (확정 8종)\n\n"
             "젊은 상태(PENDING/PAID/PREPARING)는 기간 말 주문에서만 가능하므로 전역 목표 대비 "
             "과소, 그 질량은 DELIVERED가 흡수 — 경과일 정합을 우선한 의도적 결과.\n\n"
             "| 상태 | 목표 | 생성 |\n|---|---:|---:|\n")
    for stt, w in ORDER_STATUS_DIST:
        v.append(f"| {stt} | {w:.0%} | {st_dist.get(stt,0)} ({st_dist.get(stt,0)/max(len(orders),1):.0%}) |\n")
    v.append("\n## 이벤트 유형 분포\n\n| event_type | count |\n|---|---:|\n")
    for k, c in et.most_common():
        v.append(f"| {k} | {c:,} |\n")
    (out / "VALIDATION.md").write_text("".join(v), encoding="utf-8")

    readme = f"""# 더미데이터 적재 안내

생성: generate_dummy.py (seed={cfg['rng_seed']}, {cfg['period_start']} + {cfg['period_days']}일, KST)

## 적재 순서 (FK)

1. (사전) schema + 10_category / 20_brand / 30_product 시드 적재 완료 상태
2. `35_product_brand_fill.sql`  — brand_id NULL {len(brand_updates)}건 보정 (schema의 NOT NULL 제약 대응)
3. `40_member.sql`
4. `41_guest.sql`   (converted_member_id → member FK)
5. `42_address.sql`
6. `44_orders.sql` → `44_order_item.sql` → `44_order_status_logs.sql`
7. `45_cart_item.sql`
8. `43_behavior_events.sql` (FK 없음 — 순서 무관, 마지막 권장)

## 알려진 의도적 단순화

- 재고 차감 미반영(전 상품 stock=100 유지), claim/review 미생성 — 테스트 범위 외
- product_option 미사용: cart_item.option_id·order_item.option_name 전부 NULL
- remove_from_cart {'생성' if cfg['remove_from_cart'] else '미생성 (E-1 화이트리스트 미등록 — 13번째 추가 후 --remove-from-cart 로 ON)'}
- 추천 이벤트 4종 + recommendation_generated 미생성 (2차 범위), 귀속 4컬럼 NULL
- page_view.pageType 명칭은 E-1 명세의 14종과 대조 필요: {sorted(set(PAGE_TYPES.values()))}
- member.password 는 전원 동일한 더미 BCrypt 해시 (실 로그인 불가 문자열)
- ID 베이스: member {cfg['member_id_base']}+ / orders {cfg['order_id_base']}+ / 기타 README 참조 — 기존 행과 충돌 방지

검증 결과는 VALIDATION.md 참조.
"""
    (out / "README_LOAD_ORDER.md").write_text(readme, encoding="utf-8")

    print(f"done: sessions={sess_counter:,} events={len(events):,} orders={len(orders):,} "
          f"items={len(order_items):,} carts={len(cart_rows):,} brand_fill={len(brand_updates)}")


if __name__ == "__main__":
    main()
