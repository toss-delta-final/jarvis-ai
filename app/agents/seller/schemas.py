"""판매자 그래프 구조화 출력 스키마 (SPEC-SELLER-001 §3, 일관성 장치 ⑤ — Literal·ge/le).

create_agent 의 response_format 으로 강제되는 **내부 계약**이다 — 와이어(SSE/HTTP)에
직접 나가지 않으므로 CamelModel 이 아닌 일반 BaseModel 을 쓴다(snake_case 유지).
2-2a: RouteDecision·AnalysisFinding (2026-07-18 사용자 확정).
2-2b: ReportScore·ProposedChange·ActionRecommendation·RecommendationSet (2026-07-18 사용자 확정).
2-7: DraftChange·DraftProposal (2026-07-18 잠정 확정 — 4단계 HITL 배선 시 조정 가능).
3-1: AnalysisPlan (2026-07-18 사용자 확정 — "이번 달" 등 미지원 표현은 되묻기).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 분석 워커 6종 식별자 — supervisor 라우팅·planner 분류·finding 이 공유하는 단일 출처.
# [#297] "review"(리뷰 분석, I-31) 추가 — WORKER_BUILDERS·WORKER_PROGRESS_TOKENS·
# PLANNER_PROMPT 와 동시 갱신이 계약이다(pipeline 모듈 로드 자기검증이 누락을 잡는다).
AnalysisType = Literal["sales_anomaly", "conversion", "behavior", "churn", "abuse", "review"]


class RouteDecision(BaseModel):
    """supervisor 의 3분기 라우팅 결과 (SPEC §2 — smart tier 구조화 출력 라우팅).

    category 는 Literal 로 세 값만 허용 — LLM 이 신규 카테고리를 지어낼 수 없다(장치 ⑤).
    confidence 는 "애매하면 general 재지정"(#180 저신뢰 폴백 역전) 규칙의 코드 분기
    재료다 — orchestrator.route_question 이 seller_route_confidence_min 과 비교한다.
    """

    category: Literal["analysis", "product", "general"] = Field(description="질문 카테고리")
    reason: str = Field(description="분류 근거 한 문장 — 오라우팅 디버깅·회귀 테스트용")
    confidence: float = Field(ge=0.0, le=1.0, description="분류 확신도(0~1)")


class AnalysisFinding(BaseModel):
    """분석 워커 5종의 공통 반환 형식 (SPEC §2 — response_format=ToolStrategy(AnalysisFinding)).

    팬인 시 report_agent 가 이질적 텍스트가 아닌 동일 스키마 목록을 받게 한다(설계서 §4.2.2).
    조회 실패 시에는 severity="info" + summary="데이터 확보 실패..." 의 degrade finding 을
    반환하고 파이프라인은 계속된다(SPEC §4) — 그래서 evidence 는 빈 목록을 허용한다.
    """

    analysis_type: AnalysisType = Field(description="실행한 분석 유형")
    summary: str = Field(description="핵심 발견 요약(2~3문장)")
    evidence: list[str] = Field(
        default_factory=list,
        description="근거 수치/사실 목록 — degrade finding 은 비어 있을 수 있다",
    )
    severity: Literal["info", "warning", "critical"] = Field(description="심각도")
    recommendation: str = Field(
        default="",
        description="간단 조치 힌트(선택) — 정식 행동 추천은 recommend_agent 소관(2-2b)",
    )
    chart_data_hint: str = Field(
        default="",
        description="차트로 그릴 데이터 설명 — 차트는 MVP 보류(SPEC §12), 복원 대비 보존",
    )


class SegmentNaming(BaseModel):
    """세그먼트 1건의 LLM 서술 — `behavior` 워커 상주 `interpret` 출력 (이슈 #598).

    `rule_label` 은 `sop.context.Segment.rule_label` 과의 조인 키다(원형, 표시 라벨
    아님) — `interpret` 스텝이 이 값으로 `ctx.segments[i]` 를 찾아
    `llm_label`/`llm_desc` 를 되쓴다. 크기·통계는 LLM 이 손대지 않는다(`context.py`
    불변 규약 — LLM 이 채우는 필드는 `llm_label`·`llm_desc` 둘뿐).
    """

    rule_label: str = Field(description="세그먼트 원형 라벨 — ctx.segments 조인 키")
    llm_label: str = Field(description="판매자가 이해할 짧은 별칭(예: '단골 큰손')")
    llm_desc: str = Field(description="이 세그먼트의 특징 서술 1~2문장 — 표의 수치만 근거로")


class BehaviorFinding(AnalysisFinding):
    """`behavior` 워커 상주 `interpret` 출력 — `AnalysisFinding` 에 세그먼트별 서술을 얹는다.

    다른 3워커(`churn`·`conversion`·`sales_anomaly`)는 `AnalysisFinding` 을 그대로 쓴다.
    `behavior` 만 별도 스키마가 필요한 이유: 세그먼트마다 이름·설명을 받아야 하는데
    그 결과를 실을 자리가 기존 스키마에 없다(`analysis_type` 은 항상 `"behavior"`).
    """

    segment_namings: list[SegmentNaming] = Field(
        default_factory=list,
        description="세그먼트별 별칭·설명 — ctx.segments 전건에 대응할 필요는 없다"
        "(서술할 것이 없는 세그먼트는 생략 가능)",
    )


# ── 3-1: AnalysisPlan (analysis_planner 구조화 출력 — 파이프라인 입력 계약) ────


class AnalysisPlan(BaseModel):
    """analysis_planner 의 구조화 출력 (SPEC §2 — 워커 선택 + 기간 표현, smart tier).

    기간의 날짜 환산은 LLM 소관이 아니다(장치 ④) — planner 는 질문의 기간 표현을
    **그대로 옮겨적기만** 하고, 실제 (from, to) 환산과 지원 여부 판정은 파이프라인
    코드가 `period.resolve_period` 로 수행한다(pipeline.resolve_plan).

    [#345] **이 description 은 PLANNER_PROMPT `[기간]` 절과 한 쌍이다.** 구조화 출력이라
    LLM 이 프롬프트와 이 설명을 함께 읽으므로, 한쪽만 고치면 서로 반대를 지시해 planner
    산출이 비결정적이 된다 — 어휘 규약을 바꿀 때는 반드시 둘을 같은 커밋에서 고친다.
    또한 planner 는 **기간을 이유로 clarification 을 쓰지 않는다**: 기간 관련 사용자
    대면 문구(되묻기·확인)의 소유자는 period.py 하나다
    (docs/specs/DESIGN-SELLER-PERIOD.md §4.2).

    clarification 이 비어있지 않으면 계획 불성립 — 호출부는 팬아웃 대신 되묻기
    token 으로 전환한다(DraftProposal.clarification 과 동일 패턴).
    """

    analyses: list[AnalysisType] = Field(
        default_factory=list,
        max_length=6,
        description="실행할 분석 워커 선택(1~6종) — clarification 시에는 빈 목록",
    )
    period_expr: str = Field(
        default="최근",
        description=(
            "판매자가 말한 기간 표현을 그대로 옮겨적은 값 — 단위를 바꾸거나 날짜를 "
            "계산하지 않는다('최근 3개월'·'이번 달'·'상반기'도 그대로). 표기만 정규화: "
            "연·월·일이 다 있으면 'YYYY-MM-DD~YYYY-MM-DD', 연도가 없으면 'M월 D일~M월 D일'. "
            "기간 언급이 없으면 '최근'(기본 일수는 코드 소관). "
            "해석 불가 여부와 되묻기 문구는 코드가 정한다 — 기간을 이유로 clarification 금지"
        ),
    )
    comparison_expr: str = Field(
        default="",
        description=(
            "판매자가 대조군을 함께 말했을 때만 그 표현을 그대로 옮겨적는다 "
            "('지난달 대비'·'직전 동일 기간'·'작년 대비'). 비교 요청이 없으면 빈 문자열. "
            "여기서도 날짜를 계산하지 않는다 — 환산·되묻기 문구는 코드 소관(이슈 #346)"
        ),
    )
    reason: str = Field(description="분석 선택 근거 한 문장 — 오분류 디버깅·회귀 테스트용")
    clarification: str = Field(
        default="",
        description=(
            "어떤 분석을 원하는지 파악하지 못했을 때 되물을 질문 — 비어있지 않으면 계획 "
            "불성립. 기간을 이유로는 쓰지 않는다(#345 — 기간 문구는 period.py 소유)"
        ),
    )
    wants_chart: bool = Field(
        default=False,
        description=(
            "판매자가 차트·시각화를 원하는가(이슈 #242) — 명시 키워드가 없어도 "
            "'한눈에 보여줘' 류 암시 요청이면 True. 최종 판정은 이 값과 코드 "
            "키워드 검사(_CHART_RE)의 OR 다(pipeline.resolve_plan) — LLM 이 놓친 "
            "명시 키워드를 코드가 보강한다."
        ),
    )
    chart_period_expr: str = Field(
        default="",
        description=(
            "[#504] 판매자가 **그래프 기간을 분석 기간과 별도로** 말했을 때만 그 표현을 "
            "그대로 옮겨적는다('지난달 이탈 보고서 쓰고 최근 7일 매출은 그래프로' → "
            "period_expr='지난달', chart_period_expr='최근 7일'). 별도 언급이 없으면 빈 "
            "문자열(차트는 분석 기간을 따른다). 여기서도 날짜를 계산하지 않는다 — "
            "환산·해석 불가 안내는 코드 소관(period.py 문구 소유권 규약 동일)"
        ),
    )
    chart_only: bool = Field(
        default=False,
        description=(
            "[#504] 보고서 없이 **차트만** 원하는 턴인가('최근 7일 매출 그래프만 보여줘'). "
            "True 면 워커 분석·보고서·추천을 생략하고 차트만 조립한다 — 오판이 보고서를 "
            "지우므로 확실할 때만 True. 애매하면 False + wants_chart=True 로 둔다. "
            "True 일 때는 analyses 를 비워도 된다(차트 소스는 워커와 무관)"
        ),
    )

    @field_validator("analyses")
    @classmethod
    def _dedupe_preserving_order(cls, value: list[AnalysisType]) -> list[AnalysisType]:
        """중복 워커는 첫 등장만 남긴다 — 거부(ValidationError) 대신 관용 처리.

        하드 거부는 ToolStrategy 재시도 루프만 늘린다 — 중복은 의미가 같으므로
        조용히 정리한다(팬아웃 이중 실행 방지가 목적).
        """
        return list(dict.fromkeys(value))


# ── 2-2b: ReportScore · RecommendationSet ─────────────────────────────────────

# judge 채점 축의 단일 출처 — 축 확장 시 ReportScore 에 필드 1개 추가 + 여기 등록(total 자동 합산).
SCORE_AXES: tuple[str, ...] = ("accuracy", "completeness", "clarity")
SCORE_AXIS_MAX = (
    10  # 축당 만점 — 통과 임계(현재 21/30)·루프 횟수는 Settings·verifier 코드 소관(장치 ⑦)
)


class ReportScore(BaseModel):
    """report_verifier의 smart-tier judge 결과 (SPEC §10-⑦ — 21/30 판정 재료, ≤3회 루프).

    총점·통과 여부는 LLM 필드로 두지 않는다 — LLM 산수 오류를 배제하기 위해
    total 은 코드 property 로 합산하고, 임계·루프 횟수는 verifier 노드(3단계)가
    Settings 에서 읽어 판정한다. 축 확장: 필드 추가 + SCORE_AXES 등록이면 끝.
    """

    accuracy: int = Field(
        ge=0,
        le=SCORE_AXIS_MAX,
        description="수치 정합 — 보고서의 수치가 finding evidence 와 일치하는가",
    )
    completeness: int = Field(
        ge=0,
        le=SCORE_AXIS_MAX,
        description="완전성 — 전달된 finding 전부를 반영하고 요청 범위를 커버하는가",
    )
    clarity: int = Field(
        ge=0,
        le=SCORE_AXIS_MAX,
        description="명료성 — 판매자가 이해하고 실행할 수 있는 서술인가",
    )
    feedback: str = Field(
        description="미달 축 중심의 개선 지시 — 재작성 루프에서 report_agent 프롬프트에 주입",
    )

    @property
    def total(self) -> int:
        """축 합산 총점(현재 만점 30) — 판정 재료. 판정 자체는 verifier 코드 소관."""
        return sum(getattr(self, axis) for axis in SCORE_AXES)


# ── 4단계: AnalysisScore (analysis_judge 구조화 출력 — 분석 검증 층, 이슈 #242) ─

# ReportScore 와 축당 만점(SCORE_AXIS_MAX=10, 만점 30)을 공유하되 축 이름은 분리한다
# — 두 judge 가 보는 대상이 다르다(분석 행위 vs 서술 품질). 같은 이름이면 프롬프트·
# 로그·회귀 테스트에서 섞인다(DESIGN-ANALYSIS-V31-242 §4.1).
ANALYSIS_SCORE_AXES: tuple[str, ...] = ("grounding", "sufficiency", "relevance")


class AnalysisScore(BaseModel):
    """analysis_judge 의 브랜치 검증 채점 결과 (분석 검증 층 — F1~F3 결정론 검사와 병행).

    총점·통과 여부는 ReportScore 와 동일 원칙으로 LLM 필드에 두지 않는다 —
    total 은 코드 property, 임계·재실행 횟수는 orchestrator(4단계)가 Settings 에서
    읽어 판정한다.
    """

    grounding: int = Field(
        ge=0,
        le=SCORE_AXIS_MAX,
        description="근거 대조 — finding 의 evidence 가 도구 출력에서 나왔는가",
    )
    sufficiency: int = Field(
        ge=0,
        le=SCORE_AXIS_MAX,
        description="충분성 — 배정된 분석 유형을 실제로 수행했는가",
    )
    relevance: int = Field(
        ge=0,
        le=SCORE_AXIS_MAX,
        description="적합성 — 판매자 질문·기간에 답하는 분석인가",
    )
    feedback: str = Field(
        description="미달 축 중심의 개선 지시 — 워커 재실행 입력에 주입",
    )

    @property
    def total(self) -> int:
        """축 합산 총점(만점 30) — 판정 재료. 판정 자체는 orchestrator 코드 소관."""
        return sum(getattr(self, axis) for axis in ANALYSIS_SCORE_AXES)


# update_product 인자 8종과 1:1 — 도구 시그니처가 바뀌면 여기도 함께 갱신한다.
ProductField = Literal[
    "name",
    "price",
    "original_price",
    "description",
    "category",
    "image_url",
    "status",
    "stock_quantity",
]


class ProposedChange(BaseModel):
    """추천이 제안하는 개별 필드 변경 초안 — §6.3 draft 변환의 재료.

    after 는 str 통일(2026-07-18 확정) — 가격·재고도 문자열로 받고, draft 변환
    코드(4단계)가 필드별로 캐스팅한다(Union 타입은 구조화 출력 실패율을 높임).
    before 는 저장하지 않는다 — 실행 시점에 I-9 조회로 확보한다(SPEC §3 draft 규약).
    """

    field: ProductField = Field(description="변경 대상 필드 — update_product 인자와 동일 8종")
    after: str = Field(description="변경 후 값 초안 — 수치도 문자열(예: '12900')")


class ActionRecommendation(BaseModel):
    """recommend_agent 의 개별 행동 추천 — §6.3 'N번 적용해줘'가 조회하는 저장 원천.

    product_id 는 필수(2026-07-18 확정) — promotion 유형도 특정 상품 대상으로
    한정한다(브랜드 단위 추천은 MVP 범위 밖, draft 변환에 productId 가 반드시 필요).
    [변경 2026-07-19, REALIGN F2/D2] productId 는 숫자(BE·FE 확정, DB BIGINT) — str 폐기.
    """

    action_type: Literal[
        "price_adjust",
        "description_update",
        "stock_adjust",
        "product_visibility",
        "promotion",
    ] = Field(description="추천 유형")
    product_id: int = Field(description="대상 상품 식별자(숫자) — draft 변환의 키(필수)")
    title: str = Field(description="짧은 제목 — '1번: 감귤청 가격 10% 인하' 식 표시 단위")
    rationale: str = Field(description="근거 — 어떤 finding·수치에서 나온 추천인지")
    changes: list[ProposedChange] = Field(
        default_factory=list,
        description="구체 변경 초안 — promotion 등 필드 변경이 아닌 유형은 빈 목록 허용",
    )
    expected_effect: str = Field(default="", description="기대 효과 한 문장(선택)")


# ── 2-7: DraftProposal (product_agent draft 생성 — 잠정 확정, 4단계 HITL 배선 시 조정 가능) ──


class DraftChange(BaseModel):
    """draft 의 개별 필드 변경 (api-spec §3.2 changes[] 원소의 내부 계약).

    before 는 반드시 list_my_products 조회값에서 옮긴다(프롬프트 강제) —
    create 시에는 "" 로 둔다. 와이어 변환(SSE draft, CamelModel)은 4단계 소관.
    """

    field: ProductField = Field(description="변경 대상 필드 — update_product 인자와 동일 8종")
    before: str = Field(description="변경 전 값 — list_my_products 조회값 그대로, create 는 ''")
    after: str = Field(description="변경 후 값 — 수치도 문자열")
    # [#524] 옵션별 재고 — stock_quantity 변경에만 쓰는 선택 필드(그 외 필드는 null).
    # LLM 은 option_name 만 채운다(조회 목록의 옵션명 그대로) — optionId 는 코드가
    # 실행 시점에 I-9 stocks 로 해소한다(이름은 표시·의도, id 는 실행 — 계약값은 코드).
    option_name: str | None = Field(
        default=None,
        description=(
            "재고 변경 대상 옵션명 — 조회 목록의 옵션명 그대로(줄여 쓰지 않는다). "
            "옵션 없는 상품·재고 외 필드는 null"
        ),
    )
    option_id: int | None = Field(
        default=None,
        description="코드 전용 — LLM 은 채우지 않는다(실행 시점에 I-9 로 해소)",
    )


class DraftProposal(BaseModel):
    """product_agent 의 draft 구조화 출력 (SPEC §6 — 2-7 은 draft 생성까지).

    draftId 는 LLM 필드가 아니다 — 실행 계층(4단계)이 uuid 로 발급해 checkpoint 에
    바인딩한다(§6.2-①, ReportScore.total 과 같은 '계약값은 코드' 원칙).
    clarification 이 비어있지 않으면 draft 불성립 — 호출부는 draft 대신 되묻기
    token 으로 전환한다(§6.3-4 패턴). 2026-07-18 잠정 확정 — 4단계에서 조정 가능.
    """

    op: Literal["create", "update", "delete", "ship"] = Field(
        description="작업 종류(api-spec §3.2) — ship 은 주문 아이템 발송 처리(I-30, #297)"
    )
    # [변경 2026-07-19, REALIGN F2/D2] productId 숫자 확정 — create 는 null(구 "" 폐기).
    product_id: int | None = Field(
        default=None,
        description="대상 상품(숫자) — update/delete 필수, create/ship 은 null(코드가 검증)",
    )
    # [#297] ship 전용 — I-30 발송 대상. get_orders(I-29) 조회 결과의 orderItemId 만 쓴다.
    order_item_id: int | None = Field(
        default=None,
        description="발송 대상 주문 아이템(숫자) — ship 필수, 그 외 op 는 null(코드가 검증)",
    )
    changes: list[DraftChange] = Field(
        default_factory=list,
        description="변경 목록 — delete 는 status <조회값>→DELETED 1건으로 표현(soft delete 가시화). DELETED 는 HIDDEN(숨김)과 다른 상태이며 되돌릴 수 없다",
    )
    summary: str = Field(description="diff 카드 보조 한 줄 요약(예: '가격 12,900원으로 인하')")
    clarification: str = Field(
        default="",
        description="대상 모호·제약 위반·추천 적용 발화 시 되물을 질문 — 비어있지 않으면 draft 불성립",
    )
    # [이슈 #590] 추천 적용 경로 전용 연결 키 — LLM 은 채우지 않는다(코드 전용, draftId 와
    # 같은 '계약값은 코드' 원칙). history.apply_recommendation 이 DB 에서 읽은
    # RecommendationRecord.id 를 여기 채워 hitl._execute_draft 성공 시
    # analysis_store.mark_recommendation_applied 로 되돌려 쓴다(07 결정 49).
    rec_id: str | None = Field(
        default=None,
        description="추천 적용 경로 전용 연결 키 — LLM 은 채우지 않는다(코드 전용)",
    )


# ── [#506] 등록 초안 대기 게이트 (입구 ①.8 — draft_session 대기 중 발화 3분류) ──


class PendingDraftAction(BaseModel):
    """초안 대기 중 발화 분류 — 게이트 LLM 구조화 출력 (FE 계약 §5.7).

    approve 는 실행이 아니다 — 텍스트 승인은 받지 않는다는 안내(버튼 유도)로만
    이어진다(발화 ≠ 동의 [HARD]). cancel 만 코드 단축경로(정형 발화)와 병행한다 —
    위험이 비대칭이기 때문(승인 오독은 되돌릴 수 없고, 취소 오독은 재초안이면 된다).
    """

    action: Literal["modify", "approve", "cancel", "offtopic"] = Field(
        description=(
            "modify: 초안 내용 수정 요청 / approve: 등록·승인 의도 / "
            "cancel: 취소·폐기 의도 / offtopic: 초안과 무관한 다른 주제"
        )
    )
    reason: str = Field(default="", description="판정 근거 한 줄(로그용)")


MAX_RECOMMENDATIONS = 5  # 추천 개수 상한 — 스키마 계약(와이어 아님)이라 Settings 가 아닌 상수


class RecommendationSet(BaseModel):
    """recommend_agent 구조화 출력 (response_format=ToolStrategy(RecommendationSet)).

    recommendations 의 **목록 순서가 곧 'N번'** — save_history 가 이 순서 그대로
    저장하고 §6.3 이 recommendations[N-1] 로 조회한다(순서가 계약이다).
    조회 실패·추천 없음 degrade 를 위해 빈 목록을 허용한다.
    """

    recommendations: list[ActionRecommendation] = Field(
        default_factory=list,
        max_length=MAX_RECOMMENDATIONS,
        description="행동 추천 목록(≤5) — 순서 보존이 §6.3 조회 계약",
    )
    summary: str = Field(default="", description="추천 전체 한 줄 요약 — compose_response 용(선택)")


# ── 5단계: ChartSet (graph_agent 구조화 출력 — 이슈 #242, DESIGN-ANALYSIS-V31-242 §4.2) ──
#
# 필드명을 FE jarvis-front `SellerAnalysis` 타입(features/seller/types.ts)에 그대로
# 정렬한다(결정 D-2) — AnalysisChart.tsx 컴포넌트를 무수정 재사용하기 위해서다.
# 서버 내부는 snake_case 유지(schemas.py 전역 규약), SSE 로 나갈 때만 camelCase 변환
# (6단계 _chart_event, _draft_event 의 to_camel 선례와 동일).

# ChartSet 상한 — 스키마 계약(와이어 아님)이라 Settings 가 아닌 상수(MAX_RECOMMENDATIONS 와 동일 원칙).
CHART_MAX = 3
CHART_POINTS_MAX = (
    60  # 시계열 과다 방지(일별 약 2개월) — 도구 상한(seller_summary_max_points)과 별개
)


class ChartPoint(BaseModel):
    """차트 좌표 1점 — x 는 축 라벨(날짜·카테고리 등) 문자열로 통일한다."""

    x: str = Field(description="축 라벨 — '07-01'·'장바구니' 등")
    y: float = Field(description="값 — finding·보고서에 나온 수치만(G1 검증 대상)")


class ChartSeries(BaseModel):
    """차트 계열 1개."""

    label: str = Field(description="계열 이름 — 범례에 쓰인다")
    points: list[ChartPoint] = Field(
        default_factory=list,
        max_length=CHART_POINTS_MAX,
        description="좌표 목록 — 시계열이면 시간 순",
    )


class ChartSpec(BaseModel):
    """차트 1개 — FE AnalysisChart.tsx 의 SellerAnalysis 와 1:1 대응.

    series 상한을 1로 강제한다(결정 D-3, MVP) — FE 컴포넌트가 series[0] 만
    그리므로, 스키마로 막지 않으면 서버가 2계열을 보내고 FE 가 조용히 1개만
    그리는 은폐 버그가 된다. 다계열이 필요해지면 max_length 완화와 FE 컴포넌트
    확장을 같은 릴리스로 묶어야 한다.
    """

    title: str = Field(description="차트 제목")
    chart_type: Literal["line", "bar"] = Field(
        description="시계열이면 line, 단계·범주 비교면 bar — 와이어 chartType"
    )
    unit: Literal["KRW", "COUNT", "PERCENT", "RATING"] = Field(
        description="값 단위 — RATING 은 상품별 평균 평점(1~5 실수, #504 신설)"
    )
    aggregate: Literal["sum", "avg", "none"] = Field(
        default="sum",
        description=(
            "이 계열의 집계 방식(#504 신설) — charts.py 소스 레지스트리가 채운다. "
            "sum: 합계가 의미 있음 / avg: 평균(평점) / none: 스냅샷(가격·재고)이라 둘 다 무의미. "
            "FE 헤더 숫자 분기의 근거라 unit 으로 추측하게 두지 않는다"
        ),
    )
    series: list[ChartSeries] = Field(
        default_factory=list, max_length=1, description="계열 목록 — MVP 는 1개만(결정 D-3)"
    )
    summary: str = Field(default="", description="버킷·상위 절단·스냅샷 안내 한 줄(선택)")


class ChartSet(BaseModel):
    """조립 완료된 차트 목록 — `report` 이벤트 charts[] 직렬화 재료.

    [#504] 종전에는 graph_agent 의 구조화 출력이었으나, 좌표 생성 주체가 LLM → 코드로
    바뀌며 이제 **charts.py(build_charts)가 조립하는 내부 산출물**이다. LLM 은 축 선언
    (ChartPlanSet)까지만 하고 좌표·단위·집계는 소스 레지스트리가 채운다.
    """

    charts: list[ChartSpec] = Field(
        default_factory=list, max_length=CHART_MAX, description="차트 목록(≤3)"
    )


# ── 5단계-b: ChartPlanSet (graph_agent 구조화 출력 — 이슈 #504 축 선언 계약) ──
#
# graph_agent 는 "어떤 축을 그릴 것인가"만 선언한다 — 좌표를 만들지 않는다.
# 좌표는 charts.py 가 이 선언으로 Spring(I-6·I-13·I-9·I-31)을 직접 호출해 조립한다.
# 어휘 밖 요청(퍼널·상관관계 등)은 x_axis/y_axis="other" 로 선언하고 title 에 요청명을
# 남긴다 — charts.py 가 unsupported_axes 안내로 변환한다(지원 목록 포함 완성 문장).

ChartXAxis = Literal["date", "product", "rating", "behavior_type", "other"]
ChartYAxis = Literal[
    "sales",
    "sales_quantity",
    "order_count",
    "view",
    "cart",
    "review_count",
    "avg_rating",
    "price",
    "stock",
    "event_count",
    "other",
]


class ChartAxisPlan(BaseModel):
    """차트 1개의 축 선언 — 14조합 지원 여부 판정은 charts.py 레지스트리 소관."""

    x_axis: ChartXAxis = Field(
        description=(
            "x축 — date(일자별)/product(상품별)/rating(별점별)/behavior_type(행동 유형별). "
            "네 값으로 표현할 수 없는 요청(퍼널 등)은 other"
        )
    )
    y_axis: ChartYAxis = Field(
        description=(
            "y축 — sales(매출)/sales_quantity(판매 수량)/order_count(주문 수)/view(조회수)/"
            "cart(장바구니)/review_count(리뷰 수)/avg_rating(평균 평점)/price(가격)/"
            "stock(재고)/event_count(행동 건수). 표현할 수 없으면 other"
        )
    )
    title: str = Field(
        default="",
        description=(
            "차트 제목(선택) — 비우면 코드가 기본 제목을 쓴다. other 선언 시에는 "
            "판매자가 요청한 그래프 이름을 그대로 적는다(안내 문구 재료)"
        ),
    )


class ChartPlanSet(BaseModel):
    """graph_agent 구조화 출력 (response_format=ToolStrategy(ChartPlanSet), #504).

    그릴 게 없으면 빈 목록을 허용한다(억지 차트 금지 — 종전 ChartSet 규약 승계).
    """

    charts: list[ChartAxisPlan] = Field(
        default_factory=list, max_length=CHART_MAX, description="축 선언 목록(≤3)"
    )
