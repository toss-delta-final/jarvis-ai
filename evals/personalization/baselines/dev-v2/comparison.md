# 개인화 5-arm paired 비교

- 주 비교: `clean_vs_member_no_profile` (identity·최근구매 조건 동일, profile만 변경)
- `member_no_profile_vs_guest`는 identity·최근구매가 섞인 cold-start 보조 비교
- hard-filter verdict: **pass**

## 기본 weight arm별 nDCG@10

| arm | nDCG@10 | diversity |
|---|---:|---:|
| guest | 0.448425 | 0.853211 |
| member_no_profile | 0.428237 | 0.854230 |
| clean | 0.686380 | 0.755352 |
| noisy | 0.567170 | 0.780836 |
| repeated | 0.526982 | 0.826707 |
