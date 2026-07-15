-- 주문 시드 테이블 (확정 2026-07-15, api-spec §3.6 파생).
--
-- [변경] order_mirror(이벤트 통지 미러) → order_seed. MVP(데모)에서는 주문 데이터를
-- 이벤트 채널이 아니라 이 테이블에 직접 시드한다. 따라서 event_id 멱등 키는 의미가 없어
-- surrogate id(PK)로 대체했다.
--
-- 소비처:
--   * 판매자 통계 Q&A — seller_id·amount 집계 (seller graph, MVP 최소판)
--   * 추천 dedup — 최근 구매 product_id 제외 (결정 14-F)
--   * 프로필 purchase 신호 — 구매 이력 델타 (결정 16)

CREATE TABLE IF NOT EXISTS order_seed (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- surrogate PK
    user_id      text NOT NULL,
    seller_id    text,
    product_id   text NOT NULL,
    category     text,
    amount       integer,                     -- 판매 금액 (판매자 통계용)
    purchased_at timestamptz NOT NULL,
    created_at   timestamptz DEFAULT now()
);

-- 사용자별 최근 구매 조회 (추천 dedup·프로필 신호).
CREATE INDEX IF NOT EXISTS idx_order_seed_user ON order_seed (user_id, purchased_at DESC);

-- 판매자별 매출/판매 집계.
CREATE INDEX IF NOT EXISTS idx_order_seed_seller ON order_seed (seller_id, purchased_at DESC);
