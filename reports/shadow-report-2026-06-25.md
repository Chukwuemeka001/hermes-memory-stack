# Memory Shadow Report

**Status:** PASS

## Metrics

| Metric | Value |
|---|---:|
| Shadow events | 12 |
| Avg savings | 67.44% |
| Min savings | 57.5% |
| Avg full tokens | 4648 |
| Avg projected tokens | 1513.5 |
| P95 projected tokens | 1976.0 |
| Semantic source rate | 100.0% |
| Answer-usage events | 5 |
| Used-missing count | 0 |
| Raw block events | 0 |
| Over-budget events | 0 |
| Safety pin drops | 0 |
| Determinism violations | 0 |
| Duplicate turns deduped | 0 |

## Relevance sources

| Source | Count |
|---|---:|
| `memories-index:20 hits via subprocess:python3.14` | 7 |
| `memories-index:20 hits via direct` | 5 |

## Top skipped refs

| Ref | Count |
|---|---:|
| `memory#12` | 12 |
| `memory#14` | 12 |
| `memory#15` | 12 |
| `memory#20` | 12 |
| `memory#24` | 12 |
| `memory#28` | 12 |
| `memory#35` | 12 |
| `memory#38` | 12 |
| `memory#5` | 12 |
| `memory#9` | 12 |

## Top used-but-missing refs

| Ref | Count |
|---|---:|
| — | 0 |

## Rollout decision

**PASS** — Projection can be trialed in low-risk lanes; keep shadow logging on.

### Next actions

- Enable projected mode only for low-risk summaries/status checks.
- Continue shadow logs with answer_usage enabled for serious work.

