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
    """기본값(3s×2=6s)이 first-token 10s 예산 안에 들어온다 (#133)."""
    settings = Settings(_env_file=None)

    assert settings.spring_max_retries == 1
    assert settings.spring_timeout_s * (settings.spring_max_retries + 1) < (
        settings.stream_first_token_timeout_s
    )


def test_search_retry_budget_overrun_fails_startup():
    """재시도 총량이 **턴 전체 예산**을 넘으면 기동을 막는다 — 살리려던 턴을 죽이는 설정이다."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="exhaust the buyer turn budget"):
        Settings(_env_file=None, spring_max_retries=1, spring_timeout_s=20.0)

    # 판매자와 공용인 90s 가 아니라 구매자 전용 30s 와 비교한다 — I-1 검색은 구매자 경로 전용이라
    # 느슨한 쪽과 비교하면 검증이 이름만 남는다(#138 로 두 상한이 갈렸다).
    assert Settings(_env_file=None).stream_total_timeout_buyer_s == 30.0


def test_search_retry_budget_must_also_fit_the_first_token_window():
    """[#113 PR #248 3차 리뷰] 재시도 총량은 **first-token 상한** 안에도 들어와야 한다.

    이 검증기는 원래 "추천 경로의 첫 이벤트는 `conditions` 이고 검색은 그 뒤라, 검색 재시도는
    first-token 예산을 한 톨도 쓰지 않는다"를 전제로 전체 상한(30s)과만 비교했다. **#113 이 그
    순서를 바꿨다** — 자동 완화가 검색 후에 조건을 바꿀 수 있는 턴은 표시-실제 불일치를 막으려고
    `conditions` 를 검색 뒤로 미룬다. 그 턴에서는 검색 재시도가 first-token 예산을 실제로 쓰고,
    넘기면 `stream.py` 의 `ft_deadline` 이 스트림을 끊어 504(UPSTREAM_TIMEOUT)가 된다.

    전체 상한(30s)만 보면 12s 짜리 설정이 조용히 통과해 그 턴들을 죽인다 — 두 상한을 **함께**
    본다. 재시도 횟수는 이미 `le=1` 로 묶여 있으므로 실제 튜닝 벡터는 타임아웃 값 쪽이다.
    """
    import pytest
    from pydantic import ValidationError

    # 전체 상한(30s)은 통과하지만 first-token(10s)은 못 넘는 구간 — 종전이면 조용히 통과했다.
    with pytest.raises(ValidationError, match="first-token budget"):
        Settings(_env_file=None, spring_timeout_s=6.0)  # 6 × 2 = 12s

    # 반대 방향 — first-token 상한을 낮추는 설정도 같은 쌍으로 잡힌다.
    with pytest.raises(ValidationError, match="first-token budget"):
        Settings(_env_file=None, stream_first_token_timeout_s=5.0)  # 기본 예산 6s > 5s

    # 예산을 함께 줄이면 정상 — 검증은 **쌍**을 보지 한쪽 값을 금지하지 않는다.
    assert Settings(_env_file=None, stream_first_token_timeout_s=5.0, spring_timeout_s=2.0)
    assert Settings(_env_file=None, spring_timeout_s=4.0)  # 8s < 10s — 여유가 있으면 통과


def test_deferred_retry_guard_rejects_default_serial_budget():
    """미룬 턴 재시도 가드는 기본 12s 직렬 합이 first-event 10s를 넘으면 기동을 막는다."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, search_retry_on_deferred_conditions=True)

    message = str(exc_info.value)
    assert "disable SEARCH_RETRY_ON_DEFERRED_CONDITIONS" in message
    assert "lower SPRING_TIMEOUT_S" in message
    assert "RELAXATION_MAX_ROUNDS=0" in message


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
    guarded = Settings(
        _env_file=None,
        search_retry_on_deferred_conditions=True,
        spring_timeout_s=2.0,
    )

    assert 2 * guarded.spring_timeout_s * (guarded.spring_max_retries + 1) < (
        guarded.stream_first_token_timeout_s
    )
    assert Settings(_env_file=None).search_retry_on_deferred_conditions is False


def test_deferred_retry_default_path_rejects_serial_budget_when_retries_zero():
    """기본 경로의 우연한 통과를 막는다(#277 리뷰 3차)."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, spring_max_retries=0, spring_timeout_s=6.0)

    message = str(exc_info.value)
    assert "SPRING_TIMEOUT_S" in message
    assert "RELAXATION_MAX_ROUNDS=0" in message


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
    """기본 조합(rounds=3, auto=["ratingMin"], chip 4종)의 직렬 호출 수는 2다(#288 일반형)."""
    from app.core.config import _deferred_first_event_i1_calls

    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=3,
            auto_fields=["ratingMin"],
            chip_fields=["priceMax", "ratingMin", "brand", "color"],
        )
        == 2
    )


def test_deferred_first_event_i1_calls_zero_when_relaxation_disabled():
    """rounds=0 이거나 auto 목록이 비면 미룸 자체가 없어 0이다 — 검증 대상 아님."""
    from app.core.config import _deferred_first_event_i1_calls

    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=0,
            auto_fields=["ratingMin"],
            chip_fields=["ratingMin"],
        )
        == 0
    )
    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=3,
            auto_fields=[],
            chip_fields=["priceMax", "ratingMin"],
        )
        == 0
    )


def test_deferred_first_event_i1_calls_zero_when_auto_field_missing_from_chip():
    """auto 필드가 칩 목록에 없으면 후보 자체가 안 생겨(build_relaxation_candidates가 칩만
    순회) 0이다 — 합집합이 아니라 교집합으로 세야 하는 이유."""
    from app.core.config import _deferred_first_event_i1_calls

    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=3,
            auto_fields=["ratingMin"],
            chip_fields=["priceMax", "brand", "color"],
        )
        == 0
    )


def test_deferred_first_event_i1_calls_grows_with_intersection_and_caps_at_rounds():
    """교집합이 2로 늘면 호출 수도 3으로 늘고, rounds가 그 아래면 min이 실제로 상한을 묶는다."""
    from app.core.config import _deferred_first_event_i1_calls

    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=3,
            auto_fields=["ratingMin", "priceMax"],
            chip_fields=["priceMax", "ratingMin", "brand", "color"],
        )
        == 3
    )
    assert (
        _deferred_first_event_i1_calls(
            relaxation_max_rounds=1,
            auto_fields=["ratingMin", "priceMax"],
            chip_fields=["priceMax", "ratingMin", "brand", "color"],
        )
        == 2
    )


def test_default_settings_pass_deferred_serial_budget_by_formula():
    """기본값은 일반형으로도 2 * 3.0 = 6.0 < 10.0 이라 기동이 통과한다."""
    from app.core.config import _deferred_first_event_i1_calls

    settings = Settings(_env_file=None)
    calls = _deferred_first_event_i1_calls(
        relaxation_max_rounds=settings.relaxation_max_rounds,
        auto_fields=settings.relaxation_auto_fields,
        chip_fields=settings.relaxation_chip_fields,
    )

    assert calls == 2
    assert calls * settings.spring_timeout_s == 6.0 < settings.stream_first_token_timeout_s


def test_deferred_retry_guard_off_rejects_serial_budget_tie():
    """동률(==)도 거절한다 — 어느 시계가 먼저 터지는지가 지터로 갈리는 비결정성을 막는다."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, spring_max_retries=0, spring_timeout_s=5.0)

    assert "must be < STREAM_FIRST_TOKEN_TIMEOUT_S" in str(exc_info.value)
    # 바로 아래(9.8 < 10)는 통과 — 검증이 쌍을 보지 한쪽 값을 금지하지 않는다.
    assert Settings(_env_file=None, spring_max_retries=0, spring_timeout_s=4.9)


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
        match="COLOR_SYNONYM_HARVEST_MAX_CONCURRENCY must be less than "
        "COLOR_SYNONYM_POOL_MAX_SIZE",
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
