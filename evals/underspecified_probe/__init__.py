"""과소지정 판정 축 실 LLM 프로브 (#380).

`is_underspecified_turn`(`app/agents/buyer/recommendation/underspecified.py`)이 실 발화에서
얼마나 정확히 판정하는지를 실 LLM 반복 분포로 잰다 — decompose 산출을 판정 함수에 그대로 넘기고
(판정 로직은 복제하지 않는다), 미탐(`missRate`)·오탐(`falseAlarmRate`)을 사전 등록 confirmatory
지표로 잰다. 비용·비결정론 때문에 CI 에서 돌리지 않는 **수동 실행 도구**다. 자세한 측정 범위와
한계는 `evals/underspecified_probe/README.md` 참조.
"""
