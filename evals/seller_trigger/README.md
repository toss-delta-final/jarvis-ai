# evals/seller_trigger — 판매자 트리거 판정 검증 하네스

이슈 #595 · `12-EVAL.md` §6 · `10-TRIGGER.md` §3 · 결정 94·119~121

현재 검증층(F1~F3 · D1~D3 · C1~C4 · judge)에는 **정답 대조가 0건**이다. 전부 형식과
어휘를 재고, judge 는 LLM 이 LLM 을 채점한다. "이 판정이 맞았나"를 재는 검사가 없다.
이 패키지가 그 자리를 셋으로 채운다.

| 모듈 | 무엇 | 게이트 |
|---|---|---|
| `synth.py` | 이상 0건·요일 효과만 있는 합성 브랜드(집계 응답 수준) 생성 | — |
| `null_sim.py` | 1,000일 발동률 측정 | ✅ `tier1.openRate < seller_eval_trigger_rate_max` |
| `goldenset.py` | 알려진 이상을 심고 잡는지 보는 회귀 케이스 10종 | ✅ 전 케이스 통과 |
| `ari.py` | 군집 안정성 Adjusted Rand Index | ✅ 고객 축 `>= seller_cluster_stability_min` (상품 축은 exploratory) |

## 돌리는 법

```bash
# 근거 리포트 재산출 (reports/ 에 덮어쓴다 — 커밋 대상)
uv run python -m evals.seller_trigger

# CI 가 도는 것과 같은 검사
uv run pytest tests/eval/test_seller_trigger_*.py
```

CI 는 `tests/eval/test_seller_trigger_*.py`(`@pytest.mark.eval`)로 돈다 —
`addopts` 가 제외하는 것은 smoke/integration/slow 뿐이라 별도 워크플로 설정이 필요 없다.
전 시나리오(4종 × 1,000일)는 CLI 가 돌려 `reports/` 에 커밋하고, CI 는 그중 둘을 다시
돌려 **커밋된 값과 정확히 일치하는지** 대조한다 — 리포트가 코드와 따로 노는 것을 막는
유일한 방법이다.

## 왜 원시 이벤트가 아니라 집계 응답 수준을 합성하나

`data-analysis/generate_dummy.py` 는 원시 행(behavior_events·orders)을 만든다. 그것을
트리거에 먹이려면 Spring 집계 SQL(I-6/I-7/I-13/I-8/I-14)을 파이썬으로 재구현해야 하는데,
이슈 #595 가 판정 함수를 스케줄러보다 먼저 만들게 한 이유(**이중 구현 금지**)가 한 층
아래에서 그대로 재발한다.

게다가 실측상 그 생성기는 1,000일에 못 쓴다 — 볼륨이 `period_days` 에 비례하지 않고
(1,000일이면 40건/일), 주문 상태가 `period_end` 까지의 거리 함수라 추세가 주입되며,
checkout 이탈 예산이 시간순으로 소진돼 계단 변화가 생긴다. 요일 효과의 앵커
(`stats_jarvis.json` 의 `time_weights_utc`)만 같은 원천에서 가져온다.

> ⚠️ 그 생성기의 `--remove-from-cart` 스위치는 **no-op** 이다 — `DEFAULTS` 에 키만 있고
> `emit()` 호출이 없어서, 켜도 `README_LOAD_ORDER.md` 문구 한 줄만 바뀐다
> (`remove_per_cart` 도 어디서도 읽히지 않는다). 그래서 트리거 4 골든셋(gs-08)은
> 이탈률 카운트를 직접 주는 픽스처로 작성했다.

## 규약 (`evals/README.md` 계승)

- 지표는 **분자·분모 정의를 동봉**한다 → `null_sim.METRIC_DEFINITIONS`
- 데이터셋 버전이 바뀌면 baseline 을 재실행한다 → `scenarios.DATASET_VERSION`
- **결정론은 CI, 확률은 수동** — 이 하네스는 시드 고정 결정론이라 CI 에 있다
- **채널이 헛돌지 않는지** 반대 테스트를 함께 둔다 → 각 테스트 파일의
  `test_*_is_not_vacuous`. 게이트가 항상 통과하는 코드여도 모르는 상태를 막는다

## 이 수치가 보증하지 않는 것

합성 데이터의 정상 변동은 검정이 가정하는 분포와 **정확히 일치**한다(퍼널이 중첩 이항,
카운트가 포아송). 실제 브랜드의 과분산·자기상관·프로모션은 없으므로 운영 발동률은 이
값보다 높게 나올 수 있다. 게이트는 하한 검증이지 상한 보증이 아니다.
