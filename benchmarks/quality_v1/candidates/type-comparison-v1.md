# Candidate: evidence from both named types

The new frozen synthetic comparison set improves from **0/9 to 9/9** complete
passage cases, with **34/34 essential passages** surviving both retrieval and
real extractive-answer packing. All **12/12 fallback controls** pass. These cases
cover namespaced identities, overloads, partial types, properties, distinct
members and long split methods. The [development reports](type-comparison-development/)
retain the baseline and both attempts; source/label hashes were frozen before
runtime changes. The second attempt extends third-type detection to lowercase
aliases; its outcomes are identical to the first. No labels were changed.

The existing public regression corpus remains at **51/52** complete essential
evidence, **8/8** no-evidence outcomes, and **59/60** overall. All **40/40** development
cases pass. No cases improve or regress from the [preceding candidate](cross-file-v1.md),
and there are no runtime errors. The remaining failure stays in the report; no
runtime changes followed this evaluation. Compared with the original baseline,
eleven cases improve and none regress.

The retrieval gate passes; the production gate remains open. New synthetic tests
establish mechanism behavior, while repeated-corpus results provide regression
evidence. Neither replaces independent source-label review or generated-claim
evaluation. No model inference or provider credentials were used.

Explicit comparison forms include `Compare A and B for charge and cooldown`,
`How do A and B differ in charge and cooldown?`, and `What is the difference
between A and B for charge and cooldown?`. Names can be qualified or backtick
quoted; connectors include `and`, `with`, `vs`, and `versus`. Ambiguity, absent
implementation evidence, third types and exhausted bounds preserve ordinary
ranking. Selection does not prove behavioral equivalence, difference, completeness,
or a call relationship. At most two chunks per side are selected, with each side's
first passage preceding complementary chunks. Protected anchors retain priority.
Tiny result/context budgets can omit a side; limits are never enlarged.

The query-only rule reuses existing indexes. Each named alias is capped at 100
type chunks and each scope at 200 method/property chunks. Shared reader limits
cap references at 1,000 per member, total records at 7,000, serialized characters
at 20 million, and freshness dependencies at 64 files. Queries are limited to
32 words and 4,096 characters. Namespace/assembly collisions require an
unambiguous indexed identity; equally supported competing overloads are not guessed.

See the [pre-implementation scope](type-comparison-v1-plan.md) and
[machine-readable report](type-comparison-v1.json). All original corpus settings,
labels and snapshots are unchanged. Portable reports remove only local paths
listed in `redactions` and retain the full report hashes. Implementation hashes
bind executed local bytes; line-ending normalization may change them elsewhere.
Timings describe this run, not validated large-project performance bounds.


Input status: **completed**. Scope: **independent-candidate**.
Frozen input SHA-256: `2bb402c51a5d368b026253d3aea22d5fb6e9c6678f350d8d2e4535d8068d5bfd`.
Implementation SHA-256: `a6ab5762aa8a1b21ca8ed264c21d46eec4af48c22e6928fd9b4276455fab19da`.

Counts include failed cases. Essential coverage requires every labeled group in the final evidence budget.
JSON contains descriptive 95% Wilson intervals; questions from the same project are correlated.

| Split / project / category | Questions | Essential retrieved | Essential packed | No-evidence outcome | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| split: development | 40 | 36/36 | 36/36 | 4/4 | 0 |
| split: held-out | 20 | 15/16 | 15/16 | 4/4 | 0 |
| project: Path-Creator | 20 | 18/18 | 18/18 | 2/2 | 0 |
| project: UnityPlayground | 20 | 18/18 | 18/18 | 2/2 | 0 |
| project: UnityBubbleGame | 20 | 15/16 | 15/16 | 4/4 | 0 |
| category: absent-symbol | 6 | not measured | not measured | 6/6 | 0 |
| category: behavior | 14 | 14/14 | 14/14 | not measured | 0 |
| category: configuration | 1 | 1/1 | 1/1 | not measured | 0 |
| category: cross-file | 1 | 0/1 | 0/1 | not measured | 0 |
| category: lifecycle | 5 | 5/5 | 5/5 | not measured | 0 |
| category: multi-part | 11 | 11/11 | 11/11 | not measured | 0 |
| category: persistence | 3 | 3/3 | 3/3 | not measured | 0 |
| category: physics | 5 | 5/5 | 5/5 | not measured | 0 |
| category: relationships | 4 | 4/4 | 4/4 | not measured | 0 |
| category: serialized-asset | 4 | 4/4 | 4/4 | not measured | 0 |
| category: timing | 4 | 4/4 | 4/4 | not measured | 0 |
| category: unsupported-asset | 2 | not measured | not measured | 2/2 | 0 |

## Failures

| Project | Case | Category | Diagnosis | Missing packed groups |
| --- | --- | --- | --- | --- |
| UnityBubbleGame | stone-vs-freeze | cross-file | retrieval_selection | 2 |

An index-representation miss means the required literal passage was absent from stored evidence; it does not by itself distinguish ingestion, mapping, transformed asset facts, or chunk boundaries.

## Acceptance

Held-out retrieval gate passed: **True**.
Independent corpus prerequisites met: **False**.
Generated-claim support and explanation abstention: **not evaluated**. This runner uses extractive answers and makes no generation calls. The 95% supported-claims target remains open.
Repository independence and reviewer declarations require external review; a manifest cannot prove them.
