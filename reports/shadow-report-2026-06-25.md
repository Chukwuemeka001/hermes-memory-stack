# Memory Shadow Report

**Status:** WARN

## Metrics

| Metric | Value |
|---|---:|
| Shadow events | 7 |
| Avg savings | 74.5% |
| Min savings | 74.5% |
| Avg full tokens | 4648 |
| Avg projected tokens | 1184.71 |
| P95 projected tokens | 1186.0 |
| Semantic source rate | 100.0% |
| Answer-usage events | 0 |
| Used-missing count | 0 |
| Raw block events | 0 |
| Over-budget events | 0 |
| Safety pin drops | 0 |
| Determinism violations | 0 |
| Duplicate turns deduped | 0 |

## Warnings

- no answer_usage telemetry; cannot verify used-but-skipped context

## Relevance sources

| Source | Count |
|---|---:|
| `memories-index:20 hits via subprocess:python3.14` | 7 |

## Top skipped refs

| Ref | Count |
|---|---:|
| `memory#1` | 7 |
| `memory#12` | 7 |
| `memory#13` | 7 |
| `memory#14` | 7 |
| `memory#15` | 7 |
| `memory#19` | 7 |
| `memory#20` | 7 |
| `memory#23` | 7 |
| `memory#24` | 7 |
| `memory#26` | 7 |

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

