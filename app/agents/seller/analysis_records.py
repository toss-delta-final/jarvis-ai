"""판매자 분석 저장 계층 — 저장 전용 레코드 모델 (이슈 #585).

`db/profile/init/05_seller_analysis.sql`(5테이블)의 애플리케이션 측 표현이다.
**와이어 스키마가 아니다** — `app/schemas/`의 `CamelModel`(by_alias) 계약과 섞지 않고
DDL 컬럼명 그대로 snake_case 를 쓴다(analysis_store.py 가 그대로 바인딩).

DDL 은 CHECK 제약을 두지 않는다(수동 SQL 두 번 적용 관행상 ALTER 부담을 피하려는 선택,
OPS-RUNTIME.md §1.4) — 그 대신 이 모듈의 `Literal` 타입이 어휘를 강제한다(`schemas.py` 관행).

`id`(uuid4)는 **호출부가 생성해 레코드에 채운 뒤 넘긴다** — DB 쪽에서 생성하면 쓰기
재시도(analysis_store._write 1회) 시 두 번째 행이 생길 수 있다(멱등 보장은 UNIQUE 제약이
최종 방어선이지만, PK 자체가 재시도마다 같아야 그 방어선이 의미가 있다).

JSONB 컬럼(`clusters`·`feature_rows`·`holds`·`changes`·`confounders`)은 여기서는 평범한
Python list/dict 로 두고, `analysis_store.py` 가 쓰기 시점에 `psycopg.types.json.Jsonb` 로
감싼다(`graph_journal.py` 관행) — 모양은 04-CLUSTERING/09-CHART 등 생성 측 소관이라 이
모델에서 세부 스키마를 강제하지 않는다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.agents.seller.schemas import ActionRecommendation

# 추천 액션 어휘 재사용 — `ActionRecommendation.action_type`과 동형이어야 한다(중복 정의 금지,
# DESIGN-SELLER-ANALYSIS-STORE-585.md §3.4). `schemas.py`에 별도 export 가 없어 필드
# annotation 을 그대로 가져와 이 모듈의 Literal 로 재사용한다(Pydantic v2 FieldInfo.annotation).
_ActionType = ActionRecommendation.model_fields["action_type"].annotation

TriggerType = Literal["scheduled_daily", "scheduled_weekly", "event", "manual"]
RecommendationStatus = Literal["proposed", "applied", "expired", "superseded"]


class SnapshotRecord(BaseModel):
    """`seller_analysis_snapshots` 1행 — 고객 피처 스냅샷(개인 단위, 14일 후 삭제 대상)."""

    id: UUID
    brand_id: int
    period_from: date
    period_to: date
    computed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str  # 예: "i38_v1"
    feature_spec_version: str  # 예: "fe_v1" — UNIQUE(brand_id, period_to, feature_spec_version)
    total_customers: int
    row_limit: int
    truncated: bool
    insufficient_cohort: bool
    scaler_params: dict[str, Any]
    pca_used: bool
    pca_params: dict[str, Any] | None = None
    silhouette: float | None = None
    random_state: int
    clusters: Any  # rule_label · display_label 포함 (04-CLUSTERING 소관 모양)
    feature_rows: Any  # 최대 1000행(1~2MB) — save_snapshot 이 직전에 크기를 로깅한다
    holds: Any = Field(default_factory=list)


class ReportRecord(BaseModel):
    """`seller_analysis_reports` 1행 — 집계 보고서(무기한 보존)."""

    id: UUID
    brand_id: int
    trigger_type: TriggerType
    period_from: date
    period_to: date
    compared_from: date | None = None
    compared_to: date | None = None
    title: str
    summary: str
    report_md: str
    segments: Any = Field(default_factory=list)
    # 이슈 #599 — `schemas.AnalysisFinding` 목록(snake_case). S-4 report 이벤트와 같은
    # 형식이라 채팅과 R-2 가 조립기(`api/seller._report_payload`)를 공유한다.
    # 조회 시 report_md 를 파싱해 만들지 않고 생성 시점에 코드가 합성해 저장한다 —
    # 완료 조건이 "페이지가 본문 파싱 없이" 그릴 수 있는 구조를 요구한다.
    findings: Any = Field(default_factory=list)
    holds: Any = Field(default_factory=list)
    verified: bool
    score_total: int | None = None
    attempts: int
    snapshot_id: UUID | None = None  # ON DELETE SET NULL — 스냅샷 삭제 후에도 보고서는 남는다
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    read_at: datetime | None = None


class RecommendationRecord(BaseModel):
    """`seller_analysis_recommendations` 1행 — 보고서에 딸린 추천(report 와 단일 트랜잭션 저장)."""

    id: UUID
    report_id: UUID
    brand_id: int
    rank: int  # UNIQUE(report_id, rank) — §6.3 "N번"의 저장 측 근거
    action_type: _ActionType
    target_kind: str = "product"
    segment_label: str = ""
    product_ids: list[int] = Field(default_factory=list)  # R-2 productIds 와이어와 동형
    title: str
    rationale: str
    expected_effect: str = ""
    changes: Any = Field(default_factory=list)
    effectiveness_score: float = 0.5
    status: RecommendationStatus = "proposed"
    applied_at: datetime | None = None
    draft_id: str | None = None  # 07 결정 49 — ix_sarec_draft 조회 키(find_by_draft_id)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OutcomeRecord(BaseModel):
    """`seller_analysis_outcomes` 1행 — 추천 적용 성과 측정(무기한 보존)."""

    id: UUID
    rec_id: UUID
    brand_id: int
    product_id: int | None = None
    action_type: _ActionType
    applied_at: datetime
    measured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metric_key: str  # UNIQUE(rec_id, metric_key)
    window_days: int
    outcome_spec_version: str = "oc_v1"
    treated_pre_succ: int | None = None
    treated_pre_trials: int | None = None
    treated_post_succ: int | None = None
    treated_post_trials: int | None = None
    control_pre_succ: int | None = None
    control_pre_trials: int | None = None
    control_post_succ: int | None = None
    control_post_trials: int | None = None
    control_products: int | None = None
    delta_pp: float | None = None
    p_value: float | None = None
    verdict: str
    confounders: Any = Field(default_factory=list)
