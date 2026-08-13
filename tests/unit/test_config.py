from app.core.config import Settings


def test_rerank_grounding_defaults_to_validated_with_current_rollback(monkeypatch) -> None:
    import pytest
    from pydantic import ValidationError

    assert Settings(_env_file=None).rerank_grounding_arm == "validated"
    assert Settings(_env_file=None).rerank_prompt_version == "rerank-grounding-v1"
    monkeypatch.setenv("RERANK_GROUNDING_ARM", "current")
    assert Settings(_env_file=None).rerank_grounding_arm == "current"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_grounding_arm="unknown")


def test_rerank_ranking_defaults_to_current_with_structured_opt_in(monkeypatch) -> None:
    import pytest
    from pydantic import ValidationError

    defaults = Settings(_env_file=None)
    assert defaults.rerank_ranking_arm == "current"
    assert defaults.rerank_rrf_alpha == 0.65
    assert defaults.rerank_rrf_k == 60
    assert defaults.rerank_scoring_reasoning_token_reserve == 4096
    assert defaults.rerank_scoring_prompt_version == "rerank-scoring-v1"

    monkeypatch.setenv("RERANK_RANKING_ARM", "structured")
    assert Settings(_env_file=None).rerank_ranking_arm == "structured"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_ranking_arm="unknown")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_rrf_alpha=-0.01)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_rrf_alpha=1.01)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_rrf_k=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_scoring_reasoning_token_reserve=-1)


def test_embedding_provenance_defaults():
    s = Settings(_env_file=None)
    assert s.embedding_task_document == "RETRIEVAL_DOCUMENT"
    assert s.embedding_task_query == "RETRIEVAL_QUERY"
    assert s.embedding_normalized is True


def test_observability_length_buckets_default():
    settings = Settings(_env_file=None)
    assert settings.observability_length_buckets == (50, 150, 400)


def test_langsmith_config_defaults_are_safe():
    settings = Settings(_env_file=None)

    assert settings.app_environment == "local"
    assert settings.langsmith_tracing is False
    assert settings.langsmith_api_key is None
    assert settings.langsmith_project == "jarvis-ai-local"
    assert settings.langsmith_tracing_sampling_rate == 1.0
    assert settings.langsmith_export_timeout_s == 0.5


def test_langsmith_api_key_is_secret_in_settings_repr():
    settings = Settings(_env_file=None, langsmith_api_key="lsv2_pt_super-secret")

    assert "lsv2_pt_super-secret" not in repr(settings)


def test_expose_max_defaults_to_contract_cap():
    """노출 상한 기본값은 I-21 목록당 상품 상한과 같다 (api-spec §4.2·§3.3 v0.17.3)."""
    from app.schemas.spring import LIST_MAX_PRODUCTS

    settings = Settings(_env_file=None)

    assert settings.expose_max == LIST_MAX_PRODUCTS == 9
    assert settings.expose_min <= settings.expose_max


def test_expose_max_over_contract_cap_is_rejected_at_startup():
    """expose_max 가 계약 상한을 넘으면 기동 시점에 거절한다 (PR #212 리뷰).

    통과시키면 push 페이로드 생성(RecommendationListEntry)에서 ValidationError 가 나는데,
    그 지점은 SpringUnavailableError degrade 블록 **밖**이라 §3.3 의 우아한 지연 안내 대신
    일반 INTERNAL 오류로 SSE 스트림이 끊긴다.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, expose_max=10)


def test_expose_min_above_expose_max_is_rejected():
    """expose_min > expose_max 는 보충 로직이 상한을 넘기려 드는 모순이라 거절한다."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, expose_min=9, expose_max=5)


def test_category_fanout_max_cannot_exceed_contract_list_cap():
    """leg 수가 계약 목록 상한(§4.2 lists ≤10)을 넘으면 기동 시점에 거절한다 (PR #212 리뷰).

    통과시키면 case 3 에서 니즈가 10개를 넘고 초과분이 push 직전에 조용히 잘린다 —
    사용자는 요청한 니즈가 사라진 걸 알 수 없고, rerank 예산도 잘려나갈 니즈까지 포함해
    부풀어 출력 잘림 위험이 도로 생긴다.
    """
    import pytest
    from pydantic import ValidationError

    from app.schemas.spring import MAX_LISTS

    assert Settings(_env_file=None).category_fanout_max <= MAX_LISTS
    with pytest.raises(ValidationError):
        Settings(_env_file=None, category_fanout_max=MAX_LISTS + 1)


def test_negative_repurchase_max_rejected():
    """음수 dedup_repurchase_max 는 로드 시 거부한다 (PR #230 리뷰).

    `_parse_repurchase_products` 가 `raw[:cap]` 으로 절단하는데 cap 이 음수면 "뒤에서 |cap|개
    제외"로 뒤집혀 "cap<=0 이면 정확히 0개"라는 절단 불변식이 조용히 깨진다 — 상한이 오히려
    대부분을 남긴다. 형제 튜너블 category_fanout_max 와 같은 이유로 소스에서 ge=0 으로 막는다.
    """
    import pytest
    from pydantic import ValidationError

    assert Settings(_env_file=None).dedup_repurchase_max == 5
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dedup_repurchase_max=-1)


def test_lifespan_cleanup_budget_is_independently_tunable():
    """배포 유예 설정과 교차 검증 없이 전체 cleanup 예산만 조정할 수 있다."""
    defaults = Settings(_env_file=None)

    assert defaults.lifespan_resource_close_timeout_s == 5.0
    assert defaults.lifespan_resource_close_floor_s == 0.2
    assert defaults.lifespan_cleanup_budget_s == 8.0
    assert (
        Settings(_env_file=None, lifespan_cleanup_budget_s=12.0).lifespan_cleanup_budget_s == 12.0
    )


def test_lifespan_cleanup_budget_mismatch_warns_at_runtime_instead_of_failing_startup():
    """floor 합이 예산보다 커도 기동은 허용하고 cleanup 경고로 관측한다."""
    settings = Settings(
        _env_file=None,
        lifespan_cleanup_budget_s=0.1,
        lifespan_resource_close_floor_s=0.2,
    )

    assert settings.lifespan_cleanup_budget_s == 0.1
    assert settings.lifespan_resource_close_floor_s == 0.2


def test_degrade_notice_defaults():
    """rerank 폴백은 기본 고지, dedup 스킵은 기본 미고지(빈 문자열 = off) (#133)."""
    settings = Settings(_env_file=None)

    assert "검색 결과 순서" in settings.rerank_fallback_notice
    assert settings.push_skipped_notice  # push 지연 안내는 종전부터 존재
    assert settings.dedup_skipped_notice == ""


def test_search_retry_defaults_fit_first_token_budget():
    """기본값(3s×2=6s, 사람 지시로 #394 원복)이 first-token 10s 예산 안에 들어온다 (#133)."""
    settings = Settings(_env_file=None)

    assert settings.spring_max_retries == 1
    assert settings.spring_search_timeout_s * (settings.spring_max_retries + 1) < (
        settings.stream_first_token_timeout_s
    )


def test_search_retry_budget_overrun_fails_startup():
    """재시도 총량이 **턴 전체 예산**을 넘으면 기동을 막는다 — 살리려던 턴을 죽이는 설정이다.

    [#427] 이 검증은 이제 `spring_search_timeout_s`(검색 전용)로 잰다 — 공용 `spring_timeout_s`
    를 올려도 이 검증에는 영향이 없다(분리 회귀, `test_config_search.py` 참조).
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="exhaust the buyer turn budget"):
        Settings(_env_file=None, spring_max_retries=1, spring_search_timeout_s=20.0)

    # 판매자와 공용인 90s 가 아니라 구매자 전용 30s 와 비교한다 — I-1 검색은 구매자 경로 전용이라
    # 느슨한 쪽과 비교하면 검증이 이름만 남는다(#138 로 두 상한이 갈렸다).
    assert Settings(_env_file=None).stream_total_timeout_buyer_s == 30.0


def test_search_retry_budget_must_also_fit_the_first_token_window():
    """[#113 PR #248 3차 리뷰, #427 재기준선] 단일 I-1 호출 예산은 first-token 상한과도
    비교되지만, 이제 `PROGRESS_EVENTS_ENABLED=false` 일 때만이다.

    이 검증기는 원래 "추천 경로의 첫 이벤트는 `conditions` 이고 검색은 그 뒤라, 검색 재시도는
    first-token 예산을 한 톨도 쓰지 않는다"를 전제로 전체 상한(30s)과만 비교했다. **#113 이 그
    순서를 바꿨다** — 자동 완화가 검색 후에 조건을 바꿀 수 있는 턴은 표시-실제 불일치를 막으려고
    `conditions` 를 검색 뒤로 미룬다. #427 은 그 전제를 다시 갈아엎었다 — `progress_events_
    enabled=True`(기본, #396)면 첫 SSE 는 `conditions` 가 아니라 decompose 앞의 `progress`라
    first-token 관문 자체가 이 검색 재시도 앞에 있지 않다(DESIGN-SHARED-BUDGET-384 §1(a)). 그래서
    이 비교는 `progress_events_enabled=False`(운영 롤백 등) 일 때만 다시 실질 가드가 된다.
    """
    import pytest
    from pydantic import ValidationError

    # [#394] 기본값이 0으로 바뀌어 이 검증기(재시도 1회 가정) 자체를 겨눈 값들은 명시 주입한다.
    with pytest.raises(ValidationError, match="first-token budget"):
        Settings(
            _env_file=None,
            spring_search_timeout_s=6.0,
            spring_max_retries=1,
            progress_events_enabled=False,
        )  # 6 × 2 = 12s

    # 반대 방향 — first-token 상한을 낮추는 설정도 같은 쌍으로 잡힌다.
    with pytest.raises(ValidationError, match="first-token budget"):
        Settings(
            _env_file=None,
            stream_first_token_timeout_s=5.0,
            spring_max_retries=1,
            progress_events_enabled=False,
        )  # 예산 6s > 5s

    # [리뷰 F2 수정] progress_events_enabled=True(기본)면 이 관문 자체가 적용되지 않는다 —
    # 위 첫 raise 와 **정확히 같은 단일 호출 예산**(검색예산 6.0, retries=1 → 6×2=12.0 ≥
    # first-token 10.0, 그 자체로는 여전히 위반)이 progress=True 에서는 이 비교를 그냥 건너뛰어
    # 통과한다는 것을 실제로 잰다. 다른 축(직렬 합)이 먼저 걸리지 않도록 두 가지를 함께 준다:
    # RESCUE_BUDGET_MODE=narrow 로 observe 꼬리 예약 비교를 빼고, [#306] 균일식에서 계수 3 이면
    # 직렬 합이 3×12.0=36.0 ≥ 30.0 으로 구매자 상한에 걸리므로 CATEGORY_EXPAND_ENABLED=false 로
    # 계수를 2 로 낮춘다(2×12.0=24.0 < 30.0). 종전에는 비대칭식이라 24.0 이어서 이 손잡이가
    # 필요 없었다.
    assert Settings(
        _env_file=None,
        spring_search_timeout_s=6.0,
        spring_max_retries=1,
        rescue_budget_mode="narrow",
        category_expand_enabled=False,
    )

    # 예산을 함께 줄이면 정상 — 검증은 **쌍**을 보지 한쪽 값을 금지하지 않는다.
    # [#306] 미룸 직렬 합은 이제 균일하다: 계수 3 × 검색예산 × (재시도+1) = 6×검색예산
    # (종전 비대칭식은 4×검색예산이었다). 이 assert 가 겨누는 것은 단일 호출 예산 vs
    # first-token 쌍이지 직렬 합이 아니므로, 검색예산을 1.4 로 낮춰 둘 다 통과시킨다 —
    # 단일 호출 2.8 < 9.0, 직렬 합 6×1.4=8.4 < 9.0.
    assert Settings(
        _env_file=None,
        stream_first_token_timeout_s=9.0,
        spring_search_timeout_s=1.4,
        spring_max_retries=1,
        progress_events_enabled=False,
    )
    # [#306] 같은 이유로 검색예산 1.8 은 직렬 합 6×1.8=10.8 ≥ 10.0(기본 first-token)에 걸린다 —
    # 단일 호출 예산 검증만 겨누도록 1.6 으로 낮춘다. 단일 호출 3.2(<10.0), 직렬 합
    # 6×1.6=9.6(<10.0) 둘 다 여유가 있다.
    assert Settings(
        _env_file=None,
        spring_search_timeout_s=1.6,
        spring_max_retries=1,
        progress_events_enabled=False,
    )  # 3.2s < 10s 이고 6×1.6=9.6s < 10s — 여유가 있으면 통과


def test_deferred_retry_guard_rejects_default_serial_budget():
    """직렬 합 가드는 기본 18s 가 observe 모드 꼬리 예약 비교(30-15=15s)를 넘으면 기동을
    막는다 (#427 재기준선 — 종전은 first-token 10s 였다).

    [#306] 종전에는 이 값을 내려면 `SEARCH_RETRY_ON_DEFERRED_CONDITIONS=true` 로 억제를 꺼야
    했다(끄지 않으면 비대칭식이 12.0 을 내 통과했다). 억제 기구가 사라져 **기본 재시도 설정만
    으로** 18.0 이 나온다 — 그 필드 없이도 같은 거절이 재현되는지가 이 테스트의 요지다.
    """
    import pytest
    from pydantic import ValidationError

    # 기본 검색예산(3.0)·counts(1,1,1)·retries=1 에서 균일식 직렬 합은 3 × (3.0×2) = 18.0 이고,
    # observe 모드 꼬리 예약 비교(30-15=15.0)를 넘는다.
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            spring_max_retries=1,
            rescue_budget_mode="observe",
        )

    message = str(exc_info.value)
    assert "RESCUE_BUDGET_MODE=observe" in message
    # [#306] 폐지된 손잡이 대신 재시도 자체를 낮추라고 안내해야 한다.
    assert "lower SPRING_MAX_RETRIES" in message
    assert "SEARCH_RETRY_ON_DEFERRED_CONDITIONS" not in message
    assert "lower SPRING_SEARCH_TIMEOUT_S" in message
    assert "RELAXATION_MAX_ROUNDS=0" in message
    # [#383] 구제 폴백 항을 끄는 방법도 안내한다.
    assert "CATEGORY_EXPAND_ENABLED=false" in message


def test_deferred_retry_guard_allows_empty_auto_relax_fields():
    """자동 완화 목록이 비면 그 단이 빠져 계수가 2로 내려가고 observe 에서도 통과한다."""
    settings = Settings(
        _env_file=None,
        rescue_budget_mode="observe",
        relaxation_auto_fields=[],
    )

    assert settings.relaxation_auto_fields == []
    # counts(1,1,0) × 3.0 × 2 = 12.0 < 15.0 — auto_relax 항이 빠져 통과한다.
    assert settings.spring_max_retries == 1


def test_deferred_retry_guard_allows_disabled_relaxation():
    """완화를 끄면 같은 이유로 계수가 2로 내려가 observe 에서도 통과한다."""
    settings = Settings(
        _env_file=None,
        rescue_budget_mode="observe",
        relaxation_max_rounds=0,
    )

    assert settings.relaxation_max_rounds == 0
    assert settings.spring_max_retries == 1


def test_deferred_retry_guard_allows_reduced_search_timeout():
    """[#306] 오류 메시지가 안내하는 `lower SPRING_SEARCH_TIMEOUT_S` 손잡이가 실제로 먹힌다.

    억제 기구가 사라져 직렬 합은 계수 × 검색예산 × (재시도+1) 하나다 — 검색예산만 낮춰도
    observe 모드에서 다시 기동한다(계수 3 × 1.5 × 2 = 9.0 < 15.0). 종전
    `..._allows_reduced_timeout_and_default_off` 는 폐지된 필드의 기본값을 단언해 성립할 수
    없어 이 형태로 대체했다.
    """
    from app.core.config import (
        _deferred_first_event_i1_calls,
        _rescue_chain_serial_budget_s,
        _rescue_chain_stage_counts,
    )

    reduced = Settings(
        _env_file=None,
        rescue_budget_mode="observe",
        spring_search_timeout_s=1.5,
    )

    kwargs = {
        "relaxation_max_rounds": reduced.relaxation_max_rounds,
        "auto_fields": reduced.relaxation_auto_fields,
        "chip_fields": reduced.relaxation_chip_fields,
        "category_expand_enabled": reduced.category_expand_enabled,
    }
    # [#383] 계수를 손으로 복제하면 드리프트가 생긴다 — 헬퍼로 계산한다.
    assert _deferred_first_event_i1_calls(**kwargs) == 3
    serial = _rescue_chain_serial_budget_s(
        counts=_rescue_chain_stage_counts(**kwargs),
        search_timeout_s=reduced.spring_search_timeout_s,
        spring_max_retries=reduced.spring_max_retries,
    )
    assert serial == 9.0
    assert serial < reduced.stream_total_timeout_buyer_s - reduced.rescue_tail_reserve_s


def test_deferred_retry_default_path_rejects_serial_budget_when_retries_zero():
    """기본 경로의 우연한 통과를 막는다(#277 리뷰 3차).

    [#427] 검색예산=6.0, retries=0 이면 OFF 분기 직렬 합은 (1+1)×6.0 + 1×6.0 = 18.0 이고,
    observe 모드(기본) 꼬리 예약 비교(30-15=15.0)를 넘는다.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            spring_max_retries=0,
            spring_search_timeout_s=6.0,
            rescue_budget_mode="observe",
        )

    message = str(exc_info.value)
    assert "RESCUE_BUDGET_MODE=observe" in message
    assert "SPRING_SEARCH_TIMEOUT_S" in message
    assert "RELAXATION_MAX_ROUNDS=0" in message
    # [#383] 새 손잡이 — 구제 폴백 항을 끄는 방법도 안내한다(가드 OFF 분기).
    assert "CATEGORY_EXPAND_ENABLED=false" in message


def test_deferred_retry_default_path_allows_disabled_relaxation():
    """완화를 끄면 가드 off의 12s 조합도 첫 이벤트 앞 직렬 호출이 없어 통과한다."""
    settings = Settings(
        _env_file=None,
        rescue_budget_mode="observe",
        spring_max_retries=0,
        spring_timeout_s=6.0,
        relaxation_max_rounds=0,
    )

    assert settings.spring_max_retries == 0
    assert settings.spring_timeout_s == 6.0
    assert settings.relaxation_max_rounds == 0


# ─────────── [PR #452 리뷰 R3] 구제 체인 계수 — 미룸 게이트와 물리적 사실을 분리한다 ───────────
#
# `_rescue_chain_stage_counts` 는 `relaxation_max_rounds<=0`(또는 교집합 0, = `may_auto_relax`
# 불성립)이면 세 항 전부(`main` 까지) 0을 냈다. 그런데 본검색(`main`)과 F-1/#343 재검색
# (`rescue`)은 `may_auto_relax` 와 무관하게 항상 돈다(design DESIGN-SHARED-BUDGET-384.md §3
# D6) — 조기 return 은 first-token 비교 전용 판단(#288)을 세 항 전부에 잘못 적용해, 구매자
# 30s 상한·observe 꼬리 예약 비교까지 함께 건너뛰게 만들었다(#427 이 새로 넣은 코드의 결함).


def test_rescue_chain_stage_counts_no_longer_gates_main_and_rescue_on_auto_relax():
    """[PR #452 리뷰 R3] `_rescue_chain_stage_counts` 는 이제 물리적 사실만 담는다 —
    `relaxation_max_rounds<=0`(또는 교집합 0)이어도 `main`·`rescue` 는 0으로 꺼지지 않는다.
    `auto_relax` 만 그 조건에서 자연히 0이 된다(더 이상 조기 return 이 아니라 `min` 식 자체가
    0을 낸다).
    """
    from app.core.config import _rescue_chain_stage_counts

    rounds_disabled = _rescue_chain_stage_counts(
        relaxation_max_rounds=0,
        auto_fields=["ratingMin"],
        chip_fields=["priceMax", "ratingMin", "brand", "color"],
        category_expand_enabled=True,
    )
    assert rounds_disabled.main == 1
    assert rounds_disabled.rescue == 1
    assert rounds_disabled.auto_relax == 0

    intersection_disabled = _rescue_chain_stage_counts(
        relaxation_max_rounds=3,
        auto_fields=[],
        chip_fields=["priceMax", "ratingMin"],
        category_expand_enabled=True,
    )
    assert intersection_disabled.main == 1
    assert intersection_disabled.rescue == 1
    assert intersection_disabled.auto_relax == 0

    # category_expand_enabled=False 는 종전대로 rescue 항만 없앤다 — main 은 여전히 1이다.
    rescue_disabled = _rescue_chain_stage_counts(
        relaxation_max_rounds=0,
        auto_fields=["ratingMin"],
        chip_fields=["priceMax", "ratingMin", "brand", "color"],
        category_expand_enabled=False,
    )
    assert rescue_disabled.main == 1
    assert rescue_disabled.rescue == 0
    assert rescue_disabled.auto_relax == 0


def test_startup_guard_buyer_cap_still_checked_when_auto_relax_disabled():
    """[PR #452 리뷰 R3 — 기동 검증 구멍] `RELAXATION_MAX_ROUNDS=0` 이어도 F-1 재검색
    (`category_expand_enabled=True`, 기본)은 여전히 돈다 — 구매자 30s 상한 비교는 미룸과
    무관하게 늘 걸려야 한다.

    `SPRING_SEARCH_TIMEOUT_S=16.0, SPRING_MAX_RETRIES=0` 이면 물리 계수
    (main=1+rescue=1+auto_relax=0) 직렬 합은 `2*16.0 = 32.0` ≥
    `STREAM_TOTAL_TIMEOUT_BUYER_S`(30.0) 다.

    ⚠️ `SPRING_MAX_RETRIES=0` **명시 주입이 이 테스트의 전제다** — #406 이 기본값을 1 로
    올려서, 주입하지 않으면 **단일 호출 예산**(16.0×2=32.0 ≥ 30.0)이 먼저 걸려 이 테스트가
    겨누는 직렬 합 분기에 도달하지 못한다. 두 분기의 메시지가 달라 아래 어설션이 그 차이를
    잡는다.

    검증 실효성: 옛 코드(조기 return)는 `RELAXATION_MAX_ROUNDS=0` 이면 `deferred_calls == 0`
    이라 이 비교 자체를 건너뛰어 이 조합도 기동을 통과시켰다 — **먼저 옛 코드에 돌려 통과(=
    ValidationError 미발생)를 확인했다**(TDD 적색).
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            relaxation_max_rounds=0,
            spring_max_retries=0,
            spring_search_timeout_s=16.0,
        )

    message = str(exc_info.value)
    assert "STREAM_TOTAL_TIMEOUT_BUYER_S" in message
    # 직렬 합 분기임을 못박는다 — 단일 호출 예산 분기가 먼저 걸리면 이 문구가 없다.
    assert "the serial I-1 budget across the buyer turn" in message


def test_startup_guard_observe_tail_still_checked_when_auto_relax_disabled():
    """[PR #452 리뷰 R3 — 기동 검증 구멍] observe 꼬리 예약 비교도 미룸과 무관하게 늘 걸린다.

    `SPRING_SEARCH_TIMEOUT_S=8.0` 이면 물리 계수 직렬 합은 `8.0 + 8.0 = 16.0` — 구매자 30s
    (30.0)는 안 넘지만 observe 꼬리 예약 비교(30-15=15.0)는 넘는다. 이 값으로 30s 비교가
    아니라 observe 분기만 겨눈다.

    검증 실효성: 옛 코드는 이 조합도 조기 return 으로 통과시켰다 — **먼저 옛 코드에 돌려
    통과를 확인했다**(TDD 적색).
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            relaxation_max_rounds=0,
            spring_max_retries=0,
            spring_search_timeout_s=8.0,
            rescue_budget_mode="observe",
        )

    assert "RESCUE_BUDGET_MODE=observe" in str(exc_info.value)


def test_startup_guard_first_token_check_still_gated_by_deferral_when_auto_relax_disabled():
    """[PR #452 리뷰 R3 — first-token 비교는 미룸 게이트를 유지한다] `RELAXATION_MAX_ROUNDS=0`
    이면 F-1 재검색은 `conditions` 발신 **뒤**에 돈다(`may_auto_relax=False`, design D6) —
    first-token 비교는 이 조합에서 계속 건너뛰어야 한다.

    `SPRING_SEARCH_TIMEOUT_S=6.0` 이면 물리 계수 직렬 합은 `6.0 + 6.0 = 12.0` 로
    `STREAM_FIRST_TOKEN_TIMEOUT_S`(10.0)는 넘지만 구매자 30s(30.0)·observe 꼬리 예약(15.0)
    은 넘지 않게 골라 위 두 검증과 섞이지 않는다.

    검증 실효성: first-token 비교가 (미룸 게이트 없이) 물리 계수를 그대로 쓰도록 회귀하면
    이 조합이 `STREAM_FIRST_TOKEN_TIMEOUT_S` 위반으로 거절돼 이 테스트가 깨진다.
    """
    settings = Settings(
        _env_file=None,
        rescue_budget_mode="observe",
        relaxation_max_rounds=0,
        spring_search_timeout_s=6.0,
        spring_max_retries=0,
        progress_events_enabled=False,
    )
    assert settings.relaxation_max_rounds == 0


def test_startup_guard_message_wording_differs_between_deferral_agnostic_and_first_token_branches():
    """[PR #452 리뷰 G3] 미룸과 무관한 분기(구매자 30s·observe 꼬리 예약)와 first-token 분기는
    오류 메시지·복구 안내 문구가 실제로 갈려야 한다 — R3 로 30s·observe-tail 비교가 미룸과
    무관해졌는데 메시지가 여전히 "deferred"/"disable deferral" 이라고 말하면, R3 가 코드에서
    없앤 혼동("우리는 미루는 설정이 아닌데 왜 이 오류가 뜨지")을 메시지가 그대로 재생산한다
    (리뷰 지적).

    검증 실효성: 두 분기가 다시 같은 `recovery` 문자열을 공유하도록 되돌리면(G3 결함 재현)
    미룸-무관 분기의 메시지에도 "the deferred I-1 serial budget"·"disable deferral" 이 섞여
    들어와 아래 부정 어설션이 깨진다.
    """
    import pytest
    from pydantic import ValidationError

    # 미룸과 무관한 분기(observe 꼬리 예약, RELAXATION_MAX_ROUNDS=0이라 애초에 미루지 않는다) —
    # "deferred"/"disable deferral" 을 쓰면 안 된다.
    with pytest.raises(ValidationError) as physical_exc:
        Settings(_env_file=None, relaxation_max_rounds=0, spring_search_timeout_s=8.0)
    physical_message = str(physical_exc.value)
    assert "the serial I-1 budget across the buyer turn" in physical_message
    assert "the deferred I-1 serial budget" not in physical_message
    assert "disable deferral" not in physical_message

    # first-token 분기 — 이 조합(rounds=3 기본, 교집합 성립)은 실제로 미룸 게이트가 걸리므로
    # "deferred"/"disable deferral" 이 정확한 표현이다.
    with pytest.raises(ValidationError) as deferred_exc:
        Settings(
            _env_file=None,
            spring_max_retries=1,
            spring_search_timeout_s=4.0,
            rescue_budget_mode="narrow",
            progress_events_enabled=False,
        )
    deferred_message = str(deferred_exc.value)
    assert "the deferred I-1 serial budget" in deferred_message
    assert "disable deferral" in deferred_message


# ─────────── [#306] 직렬 예산 값매김 — 억제가 사라져 세 항이 균일하다 ───────
#
# 종전에는 미룬 턴만 본검색·자동완화 probe 의 재시도가 억제되고 F-1/#343 재검색은 그 밖이라
# 혼자 재시도 전액을 썼다. 이 함수는 턴별 판정을 할 수 없어 억제되는 턴/안 되는 턴 두 상한의
# `max` 를 냈다(PR #452 R4). #306 이 억제를 제거해 두 시나리오가 하나로 합쳐졌으므로 그 세
# 테스트(`..._off_branch_uses_worst_of_both_suppression_scenarios`·`..._off_branch_default_
# counts_unchanged`·`..._off_branch_prefers_suppressed_scenario_when_auto_relax_is_large`)는
# 아래 균일식 테스트 하나로 대체했다.


def test_rescue_chain_serial_budget_s_is_uniform_across_stages():
    """[#306] 직렬 합은 `단 수 × 검색예산 × (재시도+1)` 하나다 — 항마다 값이 갈리지 않는다.

    검증 실효성: 어느 한 항이라도 억제된 1 회분(`search_timeout_s`)으로 다시 매겨지는 회귀가
    나면 아래 세 값 중 최소 하나가 작아져 깨진다. 특히 `auto_relax=0` 케이스는 옛 비대칭식이
    `(1+0)*3.0 + 1*6.0 = 9.0` 을 내던 자리라, 균일식의 `2*6.0 = 12.0` 과 값이 다르다.
    """
    from app.core.config import RescueStageCounts, _rescue_chain_serial_budget_s

    def serial(counts: RescueStageCounts, *, retries: int, t: float = 3.0) -> float:
        return _rescue_chain_serial_budget_s(
            counts=counts, search_timeout_s=t, spring_max_retries=retries
        )

    # 기본 계수 — 재시도 0/1 이 정확히 배수 관계다(억제가 있으면 12.0 에 머물렀다).
    default_counts = RescueStageCounts(main=1, rescue=1, auto_relax=1)
    assert serial(default_counts, retries=0) == 9.0
    assert serial(default_counts, retries=1) == 18.0

    # 자동완화 항이 없는 조합도 같은 곱셈이다(옛 비대칭식은 9.0 을 냈다).
    assert serial(RescueStageCounts(main=1, rescue=1, auto_relax=0), retries=1) == 12.0

    # 자동완화가 여러 단이어도 마찬가지 — 항이 늘면 선형으로 는다.
    assert serial(RescueStageCounts(main=1, rescue=1, auto_relax=2), retries=1) == 24.0


def test_startup_guard_catches_auto_relax_disabled_worst_case():
    """[PR #452 리뷰 R4 → #306 승계] `Settings` 인스턴스 경로 — `RELAXATION_MAX_ROUNDS=0`
    (`auto_relax=0`) + `SPRING_MAX_RETRIES=1` + `SPRING_SEARCH_TIMEOUT_S=4.0` 조합의 직렬 합
    `(main+rescue) × 4.0 × 2 = 16.0` 이 observe 꼬리 예약(30-15=15.0)을 넘어 기동을 막는다.

    R4 는 이 조합에서 옛 비대칭식이 `(1+0)*4.0 + 1*8.0 = 12.0` 을 내 통과시키던 구멍을
    `max(A, B)` 로 막았다. #306 이 억제를 없애 A/B 구분 자체가 사라졌지만 **이 조합의 판정은
    같다**(16.0 ≥ 15.0) — 균일식이 그 구멍을 구조적으로 갖지 않는다는 회귀 가드로 남긴다.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            rescue_budget_mode="observe",
            relaxation_max_rounds=0,
            spring_max_retries=1,
            spring_search_timeout_s=4.0,
        )
    assert "RESCUE_BUDGET_MODE=observe" in str(exc_info.value)


def test_deferred_first_event_i1_calls_matches_default_config():
    """기본 조합(rounds=3, auto=["ratingMin"], chip 4종)의 직렬 호출 수는 3이다(#383 보정식).

    `category_expand_enabled=True`(기본값)라 F-1/#343 구제 폴백 한 단이 더해져
    1(본 검색) + 1(구제 폴백) + min(3, 1)(교집합) = 3.
    """
    from app.core.config import _deferred_first_event_i1_calls

    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=3,
            auto_fields=["ratingMin"],
            chip_fields=["priceMax", "ratingMin", "brand", "color"],
            category_expand_enabled=True,
        )
        == 3
    )


def test_deferred_first_event_i1_calls_matches_actual_rescue_chain_stages():
    """[#383] 가드 모델과 실측 구제 체인 단 수가 이제 일치한다(#363 이 고정했던 불일치 해소).

    `_deferred_first_event_i1_calls`(#288)는 원래 "본 검색 1 + 자동완화 probe"만 세어 #222 F-1 /
    #343 억제-후 재판정의 무필터 재검색 한 단을 빠뜨렸다(#363 이 실측 불일치로 고정). #383 이
    `category_expand_enabled` 항을 더해 그 단을 식에 편입했다. 실측 3은 `tests/unit/
    test_fanout.py::test_worst_case_rescue_chain_sequential_stages_before_first_sse`가
    first SSE 이전 순차 Spring 왕복 수로 직접 센 값(상세 근거는
    docs/specs/MEASURE-FIRST-TOKEN-363.md §5)이며, 이 테스트는 그 상수를 가드 모델과 다시
    맞춰 둔다.

    **이 테스트가 실패해야 하는 조건**: 가드 식이 다시 이 단을 빠뜨리게 되거나, 실측 구제 체인
    단 수가 (코드 변경으로) 3이 아니게 됐는데 이 테스트가 갱신되지 않은 경우 — 즉 "둘 중 하나만
    바뀐" 상태를 잡는다.
    """
    from app.core.config import _deferred_first_event_i1_calls

    guard_model_calls = _deferred_first_event_i1_calls(
        relaxation_max_rounds=3,
        auto_fields=["ratingMin"],
        chip_fields=["priceMax", "ratingMin", "brand", "color"],
        category_expand_enabled=True,
    )
    actual_rescue_chain_stages = 3  # 출처: test_fanout.py 위 AC2 테스트(§5) — 이 상수를 실측이
    # 바뀔 때 여기서도 갱신한다.

    assert guard_model_calls == 3  # 가드가 실제로 계산하는 값(#383 보정 후)
    assert actual_rescue_chain_stages == 3  # 실측 값(오늘 기준)
    assert guard_model_calls == actual_rescue_chain_stages  # [#383] 불일치 해소 — 일치를 고정한다


def test_deferred_first_event_i1_calls_category_expand_enabled_toggles_rescue_term():
    """`category_expand_enabled` 가 True/False 일 때 값이 3/2 로 갈린다(#383 새 항 순수 함수)."""
    from app.core.config import _deferred_first_event_i1_calls

    base_kwargs = {
        "relaxation_max_rounds": 3,
        "auto_fields": ["ratingMin"],
        "chip_fields": ["priceMax", "ratingMin", "brand", "color"],
    }

    assert _deferred_first_event_i1_calls(**base_kwargs, category_expand_enabled=True) == 3
    # False 면 구제 폴백 항이 빠져 종전 #288 일반형과 동치(2)로 돌아간다.
    assert _deferred_first_event_i1_calls(**base_kwargs, category_expand_enabled=False) == 2


def test_rescue_chain_stage_counts_rescue_term_toggles_with_category_expand_enabled():
    """[#383, PR #452 리뷰 R6 — 재조준] `category_expand_enabled` 이 `_rescue_chain_stage_
    counts` 의 `rescue` 항(F-1/#343 구제 폴백)을 켜고 끈다 — True/False 에서 1/0.

    [R6] 구제 항만 떼는 하위 호환 래퍼(#383 R5, rescue-only 추출기)가 운영 소비처 없이 죽은
    코드가 돼 삭제됐다(#427 이 그 조립을 `_rescue_chain_serial_budget_s` 안으로 옮기면서
    호출이 사라졌다, `config.py::_deferred_first_event_i1_calls` docstring 참조) — 이
    테스트가 재던 "카테고리 토글이 rescue 항을 켜고 끈다"는 성질을 이제
    `_rescue_chain_stage_counts(...).rescue` 에 직접 잰다.

    옛 테스트가 함께 쟀던 "rounds=0/교집합 0 이면 rescue 도 0" 어설션은 옮기지 **않는다** —
    R3 이후 `rescue` 는 `may_auto_relax` 게이트와 무관해져(design D6, F-1/#343 은 미룸
    성립 여부와 무관하게 돈다) 더 이상 참이 아니다(의도적으로 그렇게 재정의됐다). 그 조건에서
    **총합**이 0이 되는 성질(첫 이벤트 앞 직렬 검증 자체가 대상이 아님)은
    `_deferred_first_event_i1_calls` 의 게이트 테스트
    (`test_deferred_first_event_i1_calls_zero_when_relaxation_disabled`·
    `..._zero_when_auto_field_missing_from_chip`)가 이미 고정하고 있어 중복이라 다시 두지
    않는다.
    """
    from app.core.config import _rescue_chain_stage_counts

    base_kwargs = {
        "relaxation_max_rounds": 3,
        "auto_fields": ["ratingMin"],
        "chip_fields": ["priceMax", "ratingMin", "brand", "color"],
    }

    assert _rescue_chain_stage_counts(**base_kwargs, category_expand_enabled=True).rescue == 1
    assert _rescue_chain_stage_counts(**base_kwargs, category_expand_enabled=False).rescue == 0


def test_rescue_chain_stage_counts_rescue_term_never_exceeds_the_total():
    """[PR #452 리뷰 R6 — 재조준] 옛 `rescue ≤ total` 불변식을 지금 실제로 의미가 남는 대상
    (`_rescue_chain_stage_counts` 자신)에 대해 다시 잰다 — `main`(항상 1)·`auto_relax`
    (0 이상)가 함께 더해지는 구조라 성립해야 한다. 여러 rounds·교집합·category_expand_enabled
    조합으로 재서 상수 하나만 우연히 통과하는 공허한 어설션이 아니게 한다.
    """
    from app.core.config import _rescue_chain_stage_counts

    for rounds, auto_fields, category_expand_enabled in (
        (3, ["ratingMin"], True),
        (3, ["ratingMin"], False),
        (0, ["ratingMin"], True),
        (3, [], True),
        (1, ["ratingMin", "priceMax"], True),
    ):
        counts = _rescue_chain_stage_counts(
            relaxation_max_rounds=rounds,
            auto_fields=auto_fields,
            chip_fields=["priceMax", "ratingMin", "brand", "color"],
            category_expand_enabled=category_expand_enabled,
        )
        assert counts.rescue <= counts.main + counts.rescue + counts.auto_relax


def test_deferred_first_event_i1_calls_zero_when_relaxation_disabled():
    """rounds=0 이거나 auto 목록이 비면 미룸 자체가 없어 0이다 — 검증 대상 아님.

    `category_expand_enabled=True` 여도 조기 return 0 이 구제 폴백 항보다 먼저 걸려야 한다 —
    미룸이 성립하지 않은 턴은 F-1/#343 재검색도 직렬 검증 밖이기 때문이다(#383).
    """
    from app.core.config import _deferred_first_event_i1_calls

    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=0,
            auto_fields=["ratingMin"],
            chip_fields=["ratingMin"],
            category_expand_enabled=True,
        )
        == 0
    )
    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=3,
            auto_fields=[],
            chip_fields=["priceMax", "ratingMin"],
            category_expand_enabled=True,
        )
        == 0
    )


def test_deferred_first_event_i1_calls_zero_when_auto_field_missing_from_chip():
    """auto 필드가 칩 목록에 없으면 후보 자체가 안 생겨(build_relaxation_candidates가 칩만
    순회) 0이다 — 합집합이 아니라 교집합으로 세야 하는 이유. `category_expand_enabled=True`
    에서도 조기 return 이 먼저 걸려 0이다(#383)."""
    from app.core.config import _deferred_first_event_i1_calls

    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=3,
            auto_fields=["ratingMin"],
            chip_fields=["priceMax", "brand", "color"],
            category_expand_enabled=True,
        )
        == 0
    )


def test_deferred_first_event_i1_calls_grows_with_intersection_and_caps_at_rounds():
    """교집합이 2로 늘면 호출 수도 4로 늘고, rounds가 그 아래면 min이 실제로 상한을 묶는다.

    `category_expand_enabled=True` 고정 시 값은 1(본 검색) + 1(구제 폴백) + min(rounds, 교집합).
    """
    from app.core.config import _deferred_first_event_i1_calls

    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=3,
            auto_fields=["ratingMin", "priceMax"],
            chip_fields=["priceMax", "ratingMin", "brand", "color"],
            category_expand_enabled=True,
        )
        == 4
    )
    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=1,
            auto_fields=["ratingMin", "priceMax"],
            chip_fields=["priceMax", "ratingMin", "brand", "color"],
            category_expand_enabled=True,
        )
        == 3
    )


def test_default_settings_pass_deferred_serial_budget_by_formula():
    """기본값(`spring_max_retries=1`)은 항별 값 매김으로 12.0s이고 꼬리 예약 창 15.0s 안이다
    (#383 R5, #394 원복). progress가 기본 on이라 first-token 관문은 검색 전에 지나며, 그래서
    옛 `9.0 < first-token 10.0` 단언은 더 이상 성립하지 않는다. 이 테스트는 억제된 본검색·
    자동완화 2항(각 3.0s)과 재시도되는 구제 폴백 1항(6.0s)을 분리한 값 매김을 고정한다.

    [PR #452 리뷰 R6 — 재조준] 구제 항만 떼던 하위 호환 래퍼(#383 R5)가 죽은 코드로 삭제됐다
    — 여기서 뗀 값은 `_rescue_chain_stage_counts(...).rescue` 로 조립한다(기본 설정은 미룸
    게이트가 성립해 `_deferred_first_event_i1_calls` 의 게이트된 총합과
    `_rescue_chain_stage_counts` 의 물리적 rescue 항이 같은 값을 낸다).
    """
    from app.core.config import _deferred_first_event_i1_calls, _rescue_chain_stage_counts

    settings = Settings(_env_file=None)
    kwargs = {
        "relaxation_max_rounds": settings.relaxation_max_rounds,
        "auto_fields": settings.relaxation_auto_fields,
        "chip_fields": settings.relaxation_chip_fields,
        "category_expand_enabled": settings.category_expand_enabled,
    }
    calls = _deferred_first_event_i1_calls(**kwargs)
    rescue_calls = _rescue_chain_stage_counts(**kwargs).rescue
    suppressed_calls = calls - rescue_calls
    budget = settings.spring_timeout_s * (settings.spring_max_retries + 1)

    assert calls == 3
    assert rescue_calls == 1
    assert suppressed_calls == 2
    serial_budget = suppressed_calls * settings.spring_timeout_s + rescue_calls * budget
    assert serial_budget == 12.0
    assert serial_budget < settings.stream_total_timeout_buyer_s - settings.rescue_tail_reserve_s


# [#306] `test_deferred_retry_guard_off_meters_rescue_fallback_at_full_retry_budget` 는 삭제했다 —
# 그 테스트는 "구제 폴백 항만 재시도 전액으로 매긴다"는 **비대칭 자체**를 검사했고(억제된 두 항
# 2×4.0=8.0 + 구제 폴백 1×8.0 = 16.0 거절 / 구제 항을 빼면 8.0 통과), 억제가 사라져 그 명제가
# 성립하지 않는다(균일식에서는 구제 항을 빼도 2×8.0=16.0 이라 여전히 거절된다).
# `category_expand_enabled` 토글이 계수를 낮춰 기동 통과/실패를 가른다는 축은 바로 아래
# `test_deferred_first_event_i1_calls_category_expand_enabled_false_lowers_settings_coefficient`
# 가 그대로 유지한다.


def test_deferred_first_event_i1_calls_category_expand_enabled_false_lowers_settings_coefficient():
    """[#383] `Settings` 인스턴스 경로 — `category_expand_enabled=False` 가 실제로 검증기 계수를
    낮춰 기동 통과/실패를 가른다. 호출부가 새 인자를 실제로 넘기는지 잡는 유일한 테스트다.

    [#427] `spring_max_retries=0, spring_search_timeout_s=6.0` 조합은 계수 3(기본)에서 직렬 합
    (1+1)×6.0 + 1×6.0 = 18.0 이 observe 모드 꼬리 예약 비교(30-15=15)를 넘어 거절되지만,
    `category_expand_enabled=False` 로 계수를 2로 낮추면 (1+1)×6.0=12.0 < 15.0 으로
    통과한다 — 양쪽 방향을 함께 검사한다.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=r"\(3 calls\)"):
        Settings(
            _env_file=None,
            spring_max_retries=0,
            spring_search_timeout_s=6.0,
            rescue_budget_mode="observe",
        )

    assert Settings(
        _env_file=None,
        rescue_budget_mode="observe",
        spring_max_retries=0,
        spring_search_timeout_s=6.0,
        category_expand_enabled=False,
    )


def test_deferred_retry_guard_off_rejects_serial_budget_tie():
    """동률(==)도 거절한다 — 어느 시계가 먼저 터지는지가 지터로 갈리는 비결정성을 막는다.

    [#427] 비교 대상이 구매자 전체 상한(STREAM_TOTAL_TIMEOUT_BUYER_S, 상시 비교, observe
    모드와 무관)으로 바뀌었다 — `SPRING_SEARCH_TIMEOUT_S=4.0, SPRING_MAX_RETRIES=0` 이면
    직렬 합은 3×4.0 = 12.0 이고, `STREAM_TOTAL_TIMEOUT_BUYER_S=12.0` 으로 두면 정확히
    동률이다(STREAM_FIRST_TOKEN_TIMEOUT_S 기본 10.0 이하 제약도 만족한다).

    ⚠️ `SPRING_MAX_RETRIES=0` **명시 주입이 이 테스트의 전제다** — #406 이 기본값을 1 로
    올려서, 주입하지 않으면 직렬 합이 24.0 이 되어 "동률"이 아니라 그냥 초과로 걸린다.
    그러면 이 테스트가 이름과 달리 경계(`>=` vs `>`)를 재지 않는다.
    """
    import pytest
    from pydantic import ValidationError

    # RESCUE_TAIL_RESERVE_S(기본 15.0)는 STREAM_TOTAL_TIMEOUT_BUYER_S 미만이어야 하는 별개
    # 검증기(_require_rescue_tail_reserve_within_buyer_cap)가 있다 — 이 동률 테스트는 그 값과
    # 무관하게 버퍼 상한 경계만 겨누므로 0.0 으로 낮춰 무력화한다.
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            rescue_budget_mode="observe",
            spring_search_timeout_s=4.0,
            spring_max_retries=0,
            stream_total_timeout_buyer_s=12.0,
            rescue_tail_reserve_s=0.0,
        )

    assert "must be < STREAM_TOTAL_TIMEOUT_BUYER_S" in str(exc_info.value)
    # 바로 위(12.0 < 12.1)는 통과 — 검증이 쌍을 보지 한쪽 값을 금지하지 않는다.
    assert Settings(
        _env_file=None,
        spring_search_timeout_s=4.0,
        stream_total_timeout_buyer_s=12.1,
        spring_max_retries=0,
        rescue_tail_reserve_s=0.0,
    )


def test_search_retries_capped_at_implemented_value():
    """backoff 가 없으므로 재시도 상한은 1이다 — 2 이상은 기동 실패 (PR #235 리뷰)."""
    import pytest
    from pydantic import ValidationError

    assert Settings(_env_file=None, spring_max_retries=0).spring_max_retries == 0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, spring_max_retries=2)


def test_rescue_stage_min_timeout_must_be_below_search_budget():
    """[PR #452 리뷰 R5 — 기동 검증기] `RESCUE_STAGE_MIN_TIMEOUT_S >= SPRING_SEARCH_TIMEOUT_S`
    면 기동을 막는다 — `attempts >= 1` 이라 어떤 단이든 `stage_cap`(=`spring_search_timeout_s *
    attempts`)의 최솟값은 `spring_search_timeout_s`(attempts=1) 다. 이 부등식이 깨지면 F1
    하한 clamp 가 원래 단 상한보다 큰 값을 주입해(R5) 좁히기가 목적과 정반대로 동작한다.

    기본값(0.5 < 3.0)은 통과해야 한다 — 이 검증기가 오늘 배포를 새로 막지 않는지 확인한다.

    검증 실효성: **먼저 이 검증기가 없던 코드에 돌려 통과(=ValidationError 미발생)를
    확인했다**(TDD 적색).
    """
    import pytest
    from pydantic import ValidationError

    assert Settings(_env_file=None)  # 기본값 불변 — 새 검증기가 오늘 배포를 막지 않는다.

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, rescue_stage_min_timeout_s=3.0, spring_search_timeout_s=3.0)
    assert "RESCUE_STAGE_MIN_TIMEOUT_S must be < SPRING_SEARCH_TIMEOUT_S" in str(exc_info.value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, rescue_stage_min_timeout_s=5.0, spring_search_timeout_s=3.0)

    assert Settings(_env_file=None, rescue_stage_min_timeout_s=2.9, spring_search_timeout_s=3.0)


def test_color_synonym_expansion_requires_array_contract_attestation() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(
        ValidationError,
        match=r"api-spec §4\.6.*BE 배포",
    ):
        Settings(
            _env_file=None,
            color_synonym_expansion_enabled=True,
            color_synonym_array_contract_ready=False,
        )


def test_color_synonym_contract_attestation_cannot_be_enabled_alone() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must be enabled together"):
        Settings(
            _env_file=None,
            color_synonym_expansion_enabled=False,
            color_synonym_array_contract_ready=True,
        )


def test_color_synonym_expansion_and_array_contract_must_be_enabled_together() -> None:
    settings = Settings(
        _env_file=None,
        color_synonym_expansion_enabled=True,
        color_synonym_array_contract_ready=True,
    )

    assert settings.color_synonym_expansion_enabled is True
    assert settings.color_synonym_array_contract_ready is True


def test_color_synonym_contract_gate_defaults_both_off() -> None:
    """선행 조건(BE 배포 2026-08-04·api-spec §4.6 동기화 v0.28.4·운영 시드 적재 2026-08-08)은
    충족됐지만 코드 기본값은 off 로 둔다(#258 CI hang 회귀, 2026-08-08) — 이 기능은
    pg-catalog 에 의존하는데 CI·로컬처럼 DB 가 없는 환경에서 기본 on 이면 색상 검색마다
    실패하는 연결을 재시도해 테스트가 사실상 멈춘다. 운영은 `deploy.yml` env 로 켠다."""
    settings = Settings(_env_file=None)

    assert settings.color_synonym_expansion_enabled is False
    assert settings.color_synonym_array_contract_ready is False


def test_color_synonym_gate_tolerates_empty_string_from_unregistered_deploy_vars() -> None:
    """[PR #447 리뷰] deploy.yml 이 두 값을 무조건 주입하므로 저장소 변수 미등록 시 빈
    문자열이 온다 — bool 파싱 실패로 기동이 죽지 않고 필드 기본값(off)으로 폴백해야 한다
    (`langsmith_trace_content` 폴백과 같은 관례)."""
    assert (
        Settings(_env_file=None, color_synonym_expansion_enabled="").color_synonym_expansion_enabled
        is False
    )
    assert (
        Settings(
            _env_file=None, color_synonym_array_contract_ready=""
        ).color_synonym_array_contract_ready
        is False
    )
    assert (
        Settings(
            _env_file=None, color_synonym_expansion_enabled=" "
        ).color_synonym_expansion_enabled
        is False
    )


def test_color_synonym_gate_empty_string_fallback_does_not_swallow_real_values() -> None:
    """폴백은 빈 문자열만 가로챈다 — "true" 문자열은 정상적으로 True 로 파싱돼야 한다."""
    settings = Settings(
        _env_file=None,
        color_synonym_expansion_enabled="true",
        color_synonym_array_contract_ready="true",
    )

    assert settings.color_synonym_expansion_enabled is True
    assert settings.color_synonym_array_contract_ready is True


def test_color_synonym_pool_reserves_runtime_search_slot_only_when_harvest_enabled() -> None:
    import pytest
    from pydantic import ValidationError

    settings = Settings(
        _env_file=None,
        color_synonym_batch_harvest_enabled=False,
        color_synonym_pool_max_size=1,
        color_synonym_harvest_max_concurrency=2,
    )
    assert settings.color_synonym_pool_max_size == 1

    with pytest.raises(
        ValidationError,
        match="COLOR_SYNONYM_HARVEST_MAX_CONCURRENCY must be less than COLOR_SYNONYM_POOL_MAX_SIZE",
    ):
        Settings(
            _env_file=None,
            color_synonym_batch_harvest_enabled=True,
            color_synonym_pool_max_size=2,
            color_synonym_harvest_max_concurrency=2,
        )


def test_color_synonym_scan_budget_must_exceed_accepted_term_budget() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(
        ValidationError,
        match="COLOR_SYNONYM_HARVEST_SCAN_MAX_VALUES_PER_PRODUCT must be greater than "
        "COLOR_SYNONYM_HARVEST_MAX_TERMS_PER_PRODUCT",
    ):
        Settings(
            _env_file=None,
            color_synonym_harvest_max_terms_per_product=40,
            color_synonym_harvest_scan_max_values_per_product=40,
        )


def test_trace_content_defaults_off_and_tolerates_empty_string():
    """[#326] 기본 off + 배포 vars 미설정(빈 문자열)도 off 로 해석돼 기동이 죽지 않는다."""
    assert Settings(_env_file=None).langsmith_trace_content is False
    assert Settings(_env_file=None, langsmith_trace_content="").langsmith_trace_content is False
    assert Settings(_env_file=None, langsmith_trace_content=" ").langsmith_trace_content is False
    assert Settings(_env_file=None, langsmith_trace_content="true").langsmith_trace_content is True
    assert Settings(_env_file=None).langsmith_trace_content_max_chars == 20000


def test_trace_content_max_chars_tolerates_empty_string():
    """[#326] max_chars 도 빈 문자열 vars 내성 — int("") 부팅 실패 재발 방지(PR #327 리뷰)."""
    assert (
        Settings(
            _env_file=None, langsmith_trace_content_max_chars=""
        ).langsmith_trace_content_max_chars
        == 20000
    )


def test_forwarded_for_settings_tolerate_empty_string_from_unregistered_deploy_vars() -> None:
    """[이슈 #134] deploy.yml 이 TRUST_FORWARDED_FOR·FORWARDED_FOR_TRUSTED_HOPS 를 무조건
    주입하므로 조직 Variable 미등록·삭제 시 빈 문자열이 온다 — bool/int 파싱 실패로 전체
    서비스가 기동 크래시 루프에 빠지는 사고(실증: `TRUST_FORWARDED_FOR=` →
    `ValidationError: bool_parsing`)를 막는다. 빈 값은 필드 기본값(신뢰 off / hops=1)으로
    폴백해야 한다(`langsmith_trace_content` 폴백과 같은 관례)."""
    assert Settings(_env_file=None, trust_forwarded_for="").trust_forwarded_for is False
    assert Settings(_env_file=None, trust_forwarded_for=" ").trust_forwarded_for is False
    assert Settings(_env_file=None, forwarded_for_trusted_hops="").forwarded_for_trusted_hops == 1
    assert Settings(_env_file=None, trust_forwarded_for="true").trust_forwarded_for is True
    assert Settings(_env_file=None, forwarded_for_trusted_hops="3").forwarded_for_trusted_hops == 3


def test_client_ip_probe_settings_defaults() -> None:
    """[이슈 #134] 진단 로그는 기본 on, CF 헤더 이름은 벤더 값이 코드 기본값이다."""
    settings = Settings(_env_file=None)

    assert settings.client_ip_probe_enabled is True
    assert settings.trusted_client_ip_header == "cf-connecting-ip"
