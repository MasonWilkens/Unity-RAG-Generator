# Candidate: cross-file calls and unavailable serialization

Overall outcomes improve from **57/60 to 59/60** compared with the
[previous candidate](multipart-code-v1.md). Empty-evidence controls improve from
**6/8 to 8/8**: both unsupported serialized-asset cases now report an evidence gap
instead of returning related C# code. Complete essential evidence remains
**51/52**, with **40/40 development cases** and **11/11 multipart cases** passing.
Two cases improve, none regress, and no runtime errors occur. Compared with the
preserved original [baseline](../baseline.md), eleven cases improve and none regress.

The frozen **retrieval gate passes**: held-out evidence coverage is **15/16** and
held-out empty-evidence outcomes are **4/4**. The **production gate remains open**:
labels are agent-reviewed, independent human review is missing, and generated
claims and explanation abstention are not evaluated. Repeated use of this corpus
provides regression evidence, not a fresh independent generalization result.

The new one-hop call selector passes synthetic checks for caller/target passages,
overloads, long target methods, stale dependencies, conservative dispatch handling
and preserved budgets. It does **not** resolve the remaining held-out cross-file
case, which stays in the failure table below. No implementation change followed
the held-out evaluation. Future cross-file improvements need new development
cases and an independently reviewed acceptance sample.

Unavailable-serialization diagnostics describe the indexed snapshot. Refresh after
making sources readable. Name collisions require a full path; general code-loading
questions and explicit lookup/kind requests keep their prior behavior. The policy
is intentionally limited to identifiable single-asset questions.

This query-only change reuses the previous immutable indexes without changing
chunk sizes, labels or budgets. See the [pre-evaluation scope](cross-file-v1-plan.md)
and [machine-readable report](cross-file-v1.json). Reproduce using the original
lock and candidate workspace bindings in the corpus README. The portable report
removes only absolute project/Git-root paths and retains the original report hash.
The implementation hash binds the exact executed local bytes; Git line-ending
normalization can change it in another checkout. Timings are descriptive local
measurements, not established large-project performance limits.


Input status: **completed**. Scope: **independent-candidate**.
Frozen input SHA-256: `2bb402c51a5d368b026253d3aea22d5fb6e9c6678f350d8d2e4535d8068d5bfd`.
Implementation SHA-256: `bb03be783a77a0392f4a33e505925fe23101b90802f701c1d8b92a32550f7e6d`.

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
