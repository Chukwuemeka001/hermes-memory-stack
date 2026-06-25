# Memory Shadow Report

**Status:** WARN

## Metrics

| Metric | Value |
|---|---:|
| Shadow events | 1 |
| Avg savings | 0.0% |
| Min savings | 0.0% |
| Avg full tokens | 17 |
| Avg projected tokens | 17 |
| P95 projected tokens | 17.0 |
| Semantic source rate | 100.0% |
| Answer-usage events | 1 |
| Used-missing count | 0 |
| Raw block events | 0 |
| Over-budget events | 0 |
| Safety pin drops | 0 |
| Determinism violations | 0 |
| Duplicate turns deduped | 0 |

## Warnings

- average savings 0.0% below threshold 99.0%
- only 1 answer_usage event(s); need >= 5 for rollout confidence

## Relevance sources

| Source | Count |
|---|---:|
| `memories-index:0 hits via direct-empty+subprocess:python3.14` | 1 |

## Top skipped refs

| Ref | Count |
|---|---:|

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

