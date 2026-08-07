from app.core.config import Settings


def test_embedding_provenance_defaults():
    s = Settings(_env_file=None)
    assert s.embedding_task_document == "RETRIEVAL_DOCUMENT"
    assert s.embedding_task_query == "RETRIEVAL_QUERY"
    assert s.embedding_normalized is True


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
    """기본값(3s×1=3s, #394 로 재시도 한시적 비활성)이 first-token 10s 예산 안에 들어온다 (#133)."""
    settings = Settings(_env_file=None)

    assert settings.spring_max_retries == 0
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
    # 위 첫 raise 와 **정확히 같은 조합**(검색예산 6.0, retries=1 → 단일 호출 예산
    # 6×2=12.0 ≥ first-token 10.0, 그 자체로는 여전히 위반)이 progress=True 에서는 이 비교를
    # 그냥 건너뛰어 통과한다는 것을 실제로 잰다. RESCUE_BUDGET_MODE=narrow 로 두어 observe
    # 꼬리 예약 비교(직렬 합 24.0 ≥ 15.0, 이 테스트가 겨누는 것과 다른 축)가 끼어들지 않게
    # 한다 — 안 그러면 그 검증기가 먼저 걸려 이 assert 가 "first-token 관문이 꺼졌다"를
    # 증명하지 못한다.
    assert Settings(
        _env_file=None,
        spring_search_timeout_s=6.0,
        spring_max_retries=1,
        rescue_budget_mode="narrow",
    )

    # 예산을 함께 줄이면 정상 — 검증은 **쌍**을 보지 한쪽 값을 금지하지 않는다.
    # [#383 R5] 미룸 직렬 합은 이제 항이 균질하지 않다 — 기본 설정(rescue_calls=1,
    # suppressed_calls=2)에서 spring_max_retries=1 이면 budget=검색예산×2, 직렬 합은
    # suppressed×검색예산 + rescue×budget = 2×검색예산 + 1×(검색예산×2) = 4×검색예산이다
    # (구제 폴백은 억제 밖이라 재시도를 그대로 받으므로 3×가 아니라 4×). 종전 값
    # (first-token=5.0, 검색예산=2.0)은 4×2.0=8.0 ≥ 5.0 으로 그 자체가 걸린다 — 이 assert 가
    # 겨누는 것은 단일 호출 예산 vs first-token 쌍이지 미룸 직렬 합이 아니므로, first-token
    # 상한을 9.0 으로 올려 단일 호출 예산(4.0)도, 미룸 직렬 합(8.0)도 함께 통과하는 조합으로
    # 조정한다.
    assert Settings(
        _env_file=None,
        stream_first_token_timeout_s=9.0,
        spring_search_timeout_s=2.0,
        spring_max_retries=1,
        progress_events_enabled=False,
    )
    # [#383 R5] 검색예산=2.5 는 미룸 직렬 합 4×2.5=10.0 ≥ 10.0(기본 first-token)으로 동률
    # 거절된다 — 단일 호출 예산 검증(5.0s < 10s)만 겨누도록 검색예산=1.8 로 낮춘다. 단일 호출
    # 예산 3.6s(<10s) 는 여전히 여유가 있고, 미룸 직렬 합 4×1.8=7.2 도 10.0 아래다.
    assert Settings(
        _env_file=None,
        spring_search_timeout_s=1.8,
        spring_max_retries=1,
        progress_events_enabled=False,
    )  # 3.6s < 10s 이고 4×1.8=7.2s < 10s — 여유가 있으면 통과


def test_deferred_retry_guard_rejects_default_serial_budget():
    """미룬 턴 재시도 가드는 기본 18s 직렬 합이 observe 모드 꼬리 예약 비교(30-15=15s)를
    넘으면 기동을 막는다 (#427 재기준선 — 종전은 first-token 10s 였다).
    """
    import pytest
    from pydantic import ValidationError

    # [#394] 재시도 자체는 기본 0으로 꺼졌으니, 가드가 재시도 1회를 가정한 직렬 합을 여전히
    # 올바르게 계산하는지는 명시 주입으로 겨눈다. 기본 검색예산(3.0)·counts(1,1,1) 에서 ON
    # 분기 직렬 합은 3 × (3.0×2) = 18.0 이고, observe 모드(기본) 꼬리 예약 비교(30-15=15.0)
    # 를 넘는다.
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, search_retry_on_deferred_conditions=True, spring_max_retries=1)

    message = str(exc_info.value)
    assert "RESCUE_BUDGET_MODE=observe" in message
    assert "disable SEARCH_RETRY_ON_DEFERRED_CONDITIONS" in message
    assert "lower SPRING_SEARCH_TIMEOUT_S" in message
    assert "RELAXATION_MAX_ROUNDS=0" in message
    # [#383] 새 손잡이 — 구제 폴백 항을 끄는 방법도 안내한다(가드 ON 분기).
    assert "CATEGORY_EXPAND_ENABLED=false" in message


def test_deferred_retry_guard_allows_empty_auto_relax_fields():
    """자동 완화 목록이 비면 미룸이 없어 직렬 합 검증 대상이 아니다."""
    settings = Settings(
        _env_file=None,
        relaxation_auto_fields=[],
        search_retry_on_deferred_conditions=True,
    )

    assert settings.relaxation_auto_fields == []
    assert settings.search_retry_on_deferred_conditions is True


def test_deferred_retry_guard_allows_disabled_relaxation():
    """완화를 끄면 첫 이벤트 앞 직렬 probe가 없으므로 종전 재시도 가드를 켤 수 있다."""
    settings = Settings(
        _env_file=None,
        search_retry_on_deferred_conditions=True,
        relaxation_max_rounds=0,
    )

    assert settings.search_retry_on_deferred_conditions is True
    assert settings.relaxation_max_rounds == 0


def test_deferred_retry_guard_allows_reduced_timeout_and_default_off():
    """Spring 상한을 함께 낮추면 가드가 열리고, 기본값인 가드 off도 종전대로 통과한다."""
    from app.core.config import _deferred_first_event_i1_calls

    guarded = Settings(
        _env_file=None,
        search_retry_on_deferred_conditions=True,
        spring_timeout_s=2.0,
    )

    # [#383] 계수를 손으로 복제(`2 * ...`)하면 드리프트가 생긴다 — 헬퍼로 계산한다.
    # 설정 자체는 여전히 통과한다: 계수 3(기본 조합) × 2.0 = 6.0 < 10.0.
    guarded_calls = _deferred_first_event_i1_calls(
        relaxation_max_rounds=guarded.relaxation_max_rounds,
        auto_fields=guarded.relaxation_auto_fields,
        chip_fields=guarded.relaxation_chip_fields,
        category_expand_enabled=guarded.category_expand_enabled,
    )
    assert guarded_calls * guarded.spring_timeout_s * (guarded.spring_max_retries + 1) < (
        guarded.stream_first_token_timeout_s
    )
    assert Settings(_env_file=None).search_retry_on_deferred_conditions is False


def test_deferred_retry_default_path_rejects_serial_budget_when_retries_zero():
    """기본 경로의 우연한 통과를 막는다(#277 리뷰 3차).

    [#427] 검색예산=6.0, retries=0 이면 OFF 분기 직렬 합은 (1+1)×6.0 + 1×6.0 = 18.0 이고,
    observe 모드(기본) 꼬리 예약 비교(30-15=15.0)를 넘는다.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, spring_max_retries=0, spring_search_timeout_s=6.0)

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
        spring_max_retries=0,
        spring_timeout_s=6.0,
        relaxation_max_rounds=0,
    )

    assert settings.spring_max_retries == 0
    assert settings.spring_timeout_s == 6.0
    assert settings.relaxation_max_rounds == 0


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


def test_deferred_first_event_rescue_i1_calls_isolates_the_rescue_term():
    """[#383 R5] 구제 폴백 항만 떼는 헬퍼 — True/False 에서 1/0, 미룸 불성립(rounds=0 /
    교집합 0)에서는 둘 다 0이다. `rescue ≤ total`·`total == 0 → rescue == 0` 불변식도 고정한다
    (총합 함수와 조기 return 조건이 어긋나면 이 두 불변식이 깨진다).
    """
    from app.core.config import (
        _deferred_first_event_i1_calls,
        _deferred_first_event_rescue_i1_calls,
    )

    base_kwargs = {
        "relaxation_max_rounds": 3,
        "auto_fields": ["ratingMin"],
        "chip_fields": ["priceMax", "ratingMin", "brand", "color"],
    }
    rounds_disabled = {**base_kwargs, "relaxation_max_rounds": 0}
    intersection_disabled = {**base_kwargs, "auto_fields": []}

    assert _deferred_first_event_rescue_i1_calls(**base_kwargs, category_expand_enabled=True) == 1
    assert _deferred_first_event_rescue_i1_calls(**base_kwargs, category_expand_enabled=False) == 0
    assert (
        _deferred_first_event_rescue_i1_calls(**rounds_disabled, category_expand_enabled=True) == 0
    )
    assert (
        _deferred_first_event_rescue_i1_calls(**intersection_disabled, category_expand_enabled=True)
        == 0
    )

    for enabled in (True, False):
        for kwargs in (base_kwargs, rounds_disabled, intersection_disabled):
            total = _deferred_first_event_i1_calls(**kwargs, category_expand_enabled=enabled)
            rescue = _deferred_first_event_rescue_i1_calls(
                **kwargs, category_expand_enabled=enabled
            )
            assert rescue <= total
            if total == 0:
                assert rescue == 0


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
    """기본값(`spring_max_retries=0`)은 항별 값 매김으로도 9.0 < 10.0 이라 기동이 통과한다
    (#383 R5). `retries=0` 이면 `budget == spring_timeout_s` 라 억제된 항(본 검색·자동완화
    probe)과 구제 폴백 항의 값이 갈리지 않는 경계 조건이다 — 이 테스트는 총 호출 수만이
    아니라 **항별로 나눈 값 매김**을 명시적으로 계산해도 여전히 9.0 임을 고정한다.
    """
    from app.core.config import (
        _deferred_first_event_i1_calls,
        _deferred_first_event_rescue_i1_calls,
    )

    settings = Settings(_env_file=None)
    kwargs = {
        "relaxation_max_rounds": settings.relaxation_max_rounds,
        "auto_fields": settings.relaxation_auto_fields,
        "chip_fields": settings.relaxation_chip_fields,
        "category_expand_enabled": settings.category_expand_enabled,
    }
    calls = _deferred_first_event_i1_calls(**kwargs)
    rescue_calls = _deferred_first_event_rescue_i1_calls(**kwargs)
    suppressed_calls = calls - rescue_calls
    budget = settings.spring_timeout_s * (settings.spring_max_retries + 1)

    assert calls == 3
    assert rescue_calls == 1
    assert suppressed_calls == 2
    serial_budget = suppressed_calls * settings.spring_timeout_s + rescue_calls * budget
    assert serial_budget == 9.0 < settings.stream_first_token_timeout_s


def test_deferred_retry_guard_off_meters_rescue_fallback_at_full_retry_budget():
    """[#383 R5, PR #414 Claude 리뷰] 가드 OFF 분기에서 구제 폴백 항만은 재시도 억제 밖이라
    `budget`(=검색예산×(retries+1))로 매겨야 한다 — 균질하게 검색예산 1 회분으로만 매기면 이
    항을 과소평가해 이 이슈가 원래 고치려던 실패 모드를 되풀이한다.

    [#427] 기본 검색예산(3.0)에서는 이 비대칭이 observe 모드 꼬리 예약 비교(30-15=15) 아래라
    드러나지 않는다(억제된 두 항 2×3.0=6.0 + 구제 폴백 1×(3.0×2)=6.0 = 12.0 < 15.0) —
    검색예산을 4.0 으로 올려 임계값을 넘긴다: 억제된 두 항은 2×4.0=8.0, 구제 폴백 한 항은
    재시도를 그대로 받아 1×(4.0×2)=8.0 이 되어 합이 16.0 ≥ 15.0 으로 거절된다.
    `category_expand_enabled=False` 로 구제 폴백 항 자체를 빼면 억제된 두 항만 남아
    2×4.0=8.0 < 15.0 으로 통과한다 — 이 대비가 "구제 항만 budget 으로 매긴다"를 실제로
    검사하는 유일한 테스트다.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=r"\(3 calls\)"):
        Settings(_env_file=None, spring_max_retries=1, spring_search_timeout_s=4.0)

    assert Settings(
        _env_file=None,
        spring_max_retries=1,
        spring_search_timeout_s=4.0,
        category_expand_enabled=False,
    )


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
        Settings(_env_file=None, spring_max_retries=0, spring_search_timeout_s=6.0)

    assert Settings(
        _env_file=None,
        spring_max_retries=0,
        spring_search_timeout_s=6.0,
        category_expand_enabled=False,
    )


def test_deferred_retry_guard_off_rejects_serial_budget_tie():
    """동률(==)도 거절한다 — 어느 시계가 먼저 터지는지가 지터로 갈리는 비결정성을 막는다.

    [#427] 비교 대상이 구매자 전체 상한(STREAM_TOTAL_TIMEOUT_BUYER_S, 상시 비교, observe
    모드와 무관)으로 바뀌었다 — `SPRING_SEARCH_TIMEOUT_S=4.0, SPRING_MAX_RETRIES=0`(기본)
    이면 직렬 합은 (1+1)×4.0 + 1×4.0 = 12.0 이고, `STREAM_TOTAL_TIMEOUT_BUYER_S=12.0` 으로
    두면 정확히 동률이다(STREAM_FIRST_TOKEN_TIMEOUT_S 기본 10.0 이하 제약도 만족한다).
    """
    import pytest
    from pydantic import ValidationError

    # RESCUE_TAIL_RESERVE_S(기본 15.0)는 STREAM_TOTAL_TIMEOUT_BUYER_S 미만이어야 하는 별개
    # 검증기(_require_rescue_tail_reserve_within_buyer_cap)가 있다 — 이 동률 테스트는 그 값과
    # 무관하게 버퍼 상한 경계만 겨누므로 0.0 으로 낮춰 무력화한다.
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            spring_search_timeout_s=4.0,
            stream_total_timeout_buyer_s=12.0,
            rescue_tail_reserve_s=0.0,
        )

    assert "must be < STREAM_TOTAL_TIMEOUT_BUYER_S" in str(exc_info.value)
    # 바로 위(12.0 < 12.1)는 통과 — 검증이 쌍을 보지 한쪽 값을 금지하지 않는다.
    assert Settings(
        _env_file=None,
        spring_search_timeout_s=4.0,
        stream_total_timeout_buyer_s=12.1,
        rescue_tail_reserve_s=0.0,
    )


def test_search_retries_capped_at_implemented_value():
    """backoff 가 없으므로 재시도 상한은 1이다 — 2 이상은 기동 실패 (PR #235 리뷰)."""
    import pytest
    from pydantic import ValidationError

    assert Settings(_env_file=None, spring_max_retries=0).spring_max_retries == 0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, spring_max_retries=2)


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
    settings = Settings(_env_file=None)

    assert settings.color_synonym_expansion_enabled is False
    assert settings.color_synonym_array_contract_ready is False


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
