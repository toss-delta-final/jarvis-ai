# Actual-model buyer evaluation

`python -m evals.model_eval`은 sealed 골든셋, 기존 `evals.metrics.runner.evaluate`, 고정
Spring fixture, 실제 provider LLM을 연결한다. 개발·테스트에서는 `--dry-run` 또는 fake
LLM만 사용하며 실제 호출은 별도 승인된 실행자가 수행한다.

## 실행

```bash
uv run python -m evals.model_eval --out artifacts/model-eval --split dev
uv run python -m evals.model_eval --out /tmp/me-dry --dry-run --case-limit 5 --repeats 2
```

기본 반복 5회는 dev 31케이스 × 5회 × 케이스당 3호출 보수 추정 = 약 465호출이다.
현재 recommend 경로 실측은 decompose·rerank 2호출이지만 향후 stream 호출과 실패 경로
변화를 흡수하도록 사전 gate만 3으로 유지한다. 기본 상한은 800호출, 3천만 token, USD
20이며 `app/core/config.py`에서 주입된다. 예산은 측정 결과 튜너블이 아니라 운영 안전장치라
runtime 환경변수 override를 허용하고 실제 사용값을 manifest에 기록한다. 사전 호출 예측이
상한을 넘으면 provider 객체를 만들기 전에 거절한다. 실행 중에는
호출별 usage와 versioned 가격표 비용을 누적하고 상한 초과 즉시 부분 산출물을 남긴다. usage나
단가 누락은 0이 아니라 `null`과 coverage 부족으로 남으며 release에서는 100% coverage가
아니면 실패한다.

호출 상한은 provider에 보내기 전에 슬롯을 선점한다. token·cost 상한은 완료 usage에서
초과 상태를 기록하고 다음 호출 전에 중단하므로 provider 원래 오류를 예산 예외가 덮지 않는다.
LangChain SDK 내부 재시도는 논리 호출 1건으로 계수한다. 실제 배포 후보의 재시도 거동까지
평가하기 위해 `max_retries`는 runtime 설정을 그대로 유지하고 manifest에 기록한다.

## 평가 검색 경계

LLM provider·exact model·reasoning effort·timeout·retry는 runtime 후보 설정을 명시적으로
복사하지만 검색은 항상 `spring` + 고정 fixture transport다. 프로덕션 기본
`embedding_rerank`와 다른 이유는 카탈로그 후보 snapshot을 고정하고 DB·임베딩 API 외부
의존을 차단하기 위해서다. 따라서 rerank 입력 후보와 초기 순서는 fixture 순서이며,
검색 백엔드 축 자체의 비교는 #145 범위다. 이 차이는 `modelConfig.searchBackend`와
run manifest에 기록된다.

## Holdout

holdout은 release 전용이다. `--split holdout --release --unseal-reason <사유>
--commit-sha <40자>`가 모두 있어야 `unseal_holdout_labels()`를 호출한다. 개발 검증과
일반 nightly는 dev만 사용한다.

## 산출물과 종료 코드

산출물은 `results.json`, `report.md`, `cases.csv`, `calls.csv`, `regression.csv`,
`run_manifest.json`이며 기존 디렉터리는 덮어쓰지 않는다. 종료 코드는 정상 `0`, 사전 거부
`2`, 실행 중 예산 중단 `3`, regression 또는 release 기준 실패 `4`다.

향후 nightly는 dev split 결과로 같은 회귀 이슈를 생성·갱신하고, release workflow는 사람
승인 뒤 holdout을 한 번만 연다. `.github/workflows/` 배선은 이 변경 범위 밖이며 별도 승인
작업으로 남긴다. primary metric만 confirmatory이고 slice·secondary metric은 exploratory다.
release는 hard failure, hard-constraint 위반, token/cost coverage, 예산, paired baseline을
강제한다. 절대 quality lower bound `overall.ndcgAtK.10 = 0.60`은 2026-08-03 전량 dev
실행의 순위 유효분 N=18에서 얻은 95% CI 하한 0.679를 기준으로, holdout 분포 차이와
소표본(holdout 라벨 12건 중 순위 유효분 미상)을 고려한 버퍼를 둔 보수적 기준이다.
versioned config에 고정된 값이므로 이후 release 실측이 쌓이면 근거와 함께 개정한다.

`filterAccuracy`는 골든 라벨 어휘(`keyword` 등)와 모델 산출 어휘(`semanticQuery` 등)의
합집합을 분모로 삼으므로, 모델이 다른 어휘를 선택한 차이도 의도적으로 벌점이 된다.
`baselines/20260803-dev-full-n5`는 기계적 기본 필드인 `limit`와
`excludeProductIds`를 제거하기 전 산출물이어서 그 실행의 `filterAccuracy`는 이후 실행과
직접 비교할 수 없다. 이 정규화는 평가 기록만 바꾸며 순위 출력과 primary nDCG에는 영향이 없다.

OpenAI `gpt-5-nano` 단가는 공식 모델 문서의 USD 0.05/1M input, USD 0.40/1M output을
USD/1K로 변환해 기록했다. `gpt-5.6-luna`도 2026-07-30
[OpenAI 발표](https://openai.com/index/advancing-the-price-performance-frontier-with-gpt-5-6/)의
USD 0.20/1M input, USD 1.20/1M output을 각각 USD 0.0002/1K,
USD 0.0012/1K로 변환해 manifest에 기록했으므로 이후 실행은 usage가 모두 있으면 cost
coverage 100%가 가능하다. 커밋된 `20260803-dev-full-n5` baseline은 이 단가 항목 추가 전
실행이라 cost coverage 0.534로 남으며, 불변 산출물의 run manifest에 당시 pricing manifest
해시가 기록돼 있다. LLM seed는 provider에서 강제할 수 없으며 config seed는 순서와 bootstrap
재표본에만 사용한다.
