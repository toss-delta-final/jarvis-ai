# 워커별 근거 논문 목록 (이슈 #290)

> **재구성본(2026-08-04).** 원본 `docs/worker-papers.md` 는 이전 세션에서 커밋되지 않아 소실됐다.
> 이 문서는 인수인계 문서(§2)의 선정 결과를 기준으로 링크를 재확보·재검증해 복원한 것이다.
> Phase 표기: **A** = 이번 이슈(#290)에서 알고리즘 구현, **B** = 확장사항(별도 이슈), 참고 = 피처/유형 카탈로그로만 사용.

## 1. sales_anomaly — 계절성 인지 이상탐지

| 역할 | 논문 | Phase | PDF |
|---|---|---|---|
| 핵심 | Hochenbaum, Vallis & Kejariwal (2017), *Automatic Anomaly Detection in the Cloud Via Statistical Learning* (S-H-ESD, Twitter) | A | https://arxiv.org/abs/1704.07706 (✅ 검증) |
| 보조 | Cleveland et al. (1990), *STL: A Seasonal-Trend Decomposition Procedure Based on Loess*, J. Official Statistics 6(1) | A | https://www.math.unm.edu/~lil/Stat581/STL.pdf (✅ 검색 확인) |
| 보조 | Taylor & Letham (2018), *Forecasting at Scale* (Prophet) | 제외 | https://peerj.com/preprints/3190/ — cmdstan 무겁고 프로모션 계약 부재로 Phase A 제외 |

## 2. conversion — 퍼널 단계분해 + 통계 검정

| 역할 | 논문 | Phase | PDF |
|---|---|---|---|
| 핵심 | Sismeiro & Bucklin (2004), *Modeling Purchase Behavior at an E-Commerce Web Site: A Task-Completion Approach*, JMR 41(3) | A(축약형: 비율+검정) | DOI: https://journals.sagepub.com/doi/10.1509/jmkr.41.3.306.35985 · 무료본: https://www.semanticscholar.org/paper/963e428f5b5bb6013ef873d11ea0497056fbaa1c |
| 보조 | Esmeli et al. (2021), *Towards Early Purchase Intention Prediction in Online Session Based Retailing Systems*, Electronic Markets 31 | B | https://link.springer.com/article/10.1007/s12525-020-00448-x (OA, ✅ 검증 — 구 링크 00463-6 이 아니라 **00448-x**) |
| 보조 | Montgomery et al. (2004), *Modeling Online Browsing and Path Analysis Using Clickstream Data*, Marketing Science 23(4) | B(마르코프) | https://www.andrew.cmu.edu/user/alm3/papers/purchase%20conversion.pdf (✅ CMU 검색 확인) |

## 3. behavior — 방문 유형론 + 상품 군집화

| 역할 | 논문 | Phase | PDF |
|---|---|---|---|
| 핵심 | Fader, Hardie & Lee (2005), *"Counting Your Customers" the Easy Way: An Alternative to the Pareto/NBD Model* (BG/NBD), Marketing Science 24(2) | B | https://www.brucehardie.com/papers/018/fader_et_al_mksc_05.pdf (✅ 검증) |
| 보조 | Fader, Hardie & Lee (2005), *RFM and CLV: Using Iso-Value Curves for Customer Base Analysis*, JMR 42(4) | B | https://www.brucehardie.com/papers/rfm_clv_2005-02-16.pdf |
| 보조 | Chen, Sain & Guo (2012), *Data Mining for the Online Retail Industry: A Case Study of RFM Model-Based Customer Segmentation*, J. Database Marketing 19 | **A(k-means)** | DOI: https://link.springer.com/article/10.1057/dbm.2012.17 · 무료본: https://www.researchgate.net/publication/263329040 |
| 보조 | Moe (2003), *Buying, Searching, or Browsing: Differentiating Between Online Shoppers Using In-Store Navigational Clickstream*, J. Consumer Psychology 13(1-2) | **A(유형론→군집 라벨)** | DOI: https://myscp.onlinelibrary.wiley.com/doi/10.1207/S15327663JCP13-1&2_03 · 무료본: https://www.researchgate.net/publication/237931720 |

## 4. churn — 피처 카탈로그 + 비율 검정

| 역할 | 논문 | Phase | PDF |
|---|---|---|---|
| 핵심 | Lemmens & Croux (2006), *Bagging and Boosting Classification Trees to Predict Churn*, JMR 43(2) | B | https://research.tilburguniversity.edu/files/1425373/lemmens_bagging.pdf (✅ 검증) · 대안: https://www.aurelielemmens.com/wp-content/uploads/2018/07/Bagging-and-boosting-classification-trees-to-predict-churn.pdf |
| 보조 | Lundberg & Lee (2017), *A Unified Approach to Interpreting Model Predictions* (SHAP), NeurIPS | B | https://arxiv.org/abs/1705.07874 |
| 보조 | Ahn et al. (2020), *A Survey on Churn Analysis in Various Business Domains*, IEEE Access 8 | **A(피처 카탈로그·이탈 정의 근거)** | https://www.semanticscholar.org/paper/c4b810efe7f18ea3735744fb7c8fc768e7bb6e9e (OA — IEEE DOI: 10.1109/ACCESS.2020.3042657) |

## 5. abuse — 봇 시그널 + 이상 3유형 체계

| 역할 | 논문 | Phase | PDF |
|---|---|---|---|
| 핵심 | Liu, Ting & Zhou (2008), *Isolation Forest*, ICDM | B | https://cs.nju.edu.cn/zhouzh/zhouzh.files/publication/icdm08b.pdf (✅ 검증) |
| 보조 | Breunig et al. (2000), *LOF: Identifying Density-Based Local Outliers*, SIGMOD | B | https://www.dbs.ifi.lmu.de/Publikationen/Papers/LOF.pdf · ACM: https://dl.acm.org/doi/10.1145/342009.335388 |
| 보조 | Tan & Kumar (2002), *Discovery of Web Robot Sessions Based on Their Navigational Patterns*, DMKD 6 | **A(봇 피처)** | DOI: https://link.springer.com/article/10.1023/A:1013228602957 · 무료본: https://www.researchgate.net/publication/220451883 |
| 보조 | Chandola, Banerjee & Kumar (2009), *Anomaly Detection: A Survey*, ACM CSUR 41(3) | **A(Point/Contextual/Collective 3유형 체계)** | https://conservancy.umn.edu/handle/11299/215731 (UMN TR 07-017) · ACM: https://dl.acm.org/doi/10.1145/1541880.1541882 |

---

## Phase A 에서 실제 구현에 쓰는 논문 (다운로드 우선순위)

1. **S-H-ESD** (arXiv 1704.07706) — `analysis/timeseries.py` STL+GESD 명세의 원천
2. **STL** (Cleveland 1990) — period·robust 파라미터 근거
3. **Sismeiro & Bucklin 2004** — conversion 단계분해 프레임(축약형 근거 문서화용)
4. **Chen 2012 + Moe 2003** — behavior 피처 벡터·군집 라벨 어휘
5. **Ahn 2020** — churn 피처 카탈로그·activity-based 이탈 정의 인용
6. **Tan & Kumar 2002 + Chandola 2009** — abuse 3-트랙 피처·유형 매핑

Wilson CI·two-proportion z-검정(`analysis/proportions.py`)은 표준 통계 기법이라 별도 논문 불요 — 설계 문서에는 교과서 수준 인용으로 충분.

> ⚠️ ResearchGate 직링크는 세션에 따라 로그인 벽이 뜰 수 있다 — 막히면 DOI/Semantic Scholar 경유.
