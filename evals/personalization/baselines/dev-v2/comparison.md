# 개인화 5-arm paired 비교

- 주 비교: `clean_vs_member_no_profile` (identity·최근구매 조건 동일, profile만 변경)
- `member_no_profile_vs_guest`는 identity·최근구매가 섞인 cold-start 보조 비교
- hard-filter verdict: **pass**

## 기본 weight arm별 nDCG@10

| arm | nDCG@10 | diversity |
|---|---:|---:|
| guest | 0.461219 | 0.840278 |
| member_no_profile | 0.429822 | 0.842593 |
| clean | 0.734220 | 0.746528 |
| noisy | 0.628061 | 0.769676 |
| repeated | 0.589522 | 0.822917 |
