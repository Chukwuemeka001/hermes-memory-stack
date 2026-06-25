# Memory Shadow Report

**Status:** WARN

## Metrics

| Metric | Value |
|---|---:|
| Shadow events | 9 |
| Avg savings | 70.72% |
| Min savings | 57.5% |
| Avg full tokens | 4648 |
| Avg projected tokens | 1360.56 |
| P95 projected tokens | 1976.0 |
| Semantic source rate | 100.0% |
| Answer-usage events | 2 |
| Used-missing count | 0 |
| Raw block events | 0 |
| Over-budget events | 0 |
| Safety pin drops | 0 |
| Determinism violations | 0 |
| Duplicate turns deduped | 0 |

## Warnings

- only 2 answer_usage event(s); need >= 5 for rollout confidence

## Relevance sources

| Source | Count |
|---|---:|
| `memories-index:20 hits via subprocess:python3.14` | 7 |
| `memories-index:20 hits via direct` | 2 |

## Top skipped refs

| Ref | Count |
|---|---:|
| `memory#12` | 9 |
| `memory#14` | 9 |
| `memory#15` | 9 |
| `memory#20` | 9 |
| `memory#24` | 9 |
| `memory#28` | 9 |
| `memory#35` | 9 |
| `memory#38` | 9 |
| `memory#5` | 9 |
| `memory#9` | 9 |

## Top used-but-missing refs

| Ref | Count |
|---|---:|
| — | 0 |

## Rollout decision

**WARN** — Do not flip live projection globally; tune and collect more evidence.

### Next actions

- Inspect top_skipped_refs and top_used_missing_refs.
- Raise budget or relevance reserve if required context is consistently skipped.
- Add pin rules for any skipped safety/identity/operational items.

