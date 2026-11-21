# Candidate: multipart code retrieval

Complete essential evidence improves from **48/52 to 51/52** compared with the
[previous candidate](literal-assets-v1.md), with three improved cases and no
regressions. All **11/11 multipart questions** and **40/40 development questions**
pass. Empty-evidence controls remain **6/8**. Overall, **57/60** cases pass with no
runtime errors. Compared with the preserved original [baseline](../baseline.md),
nine cases improve and none regress.

The three development misses now include the property setter's event/cache
updates, both the shoot condition and timestamp assignment, and both scheduling
and destruction methods in the final evidence budget. Held-out results remain
**15/16** complete essential evidence and **2/4** empty-evidence controls. The
three held-out failures are retained below; no tuning used these outcomes.

The retrieval and production gates remain failed. Source evidence coverage and
agent-reviewed labels do not establish generated-answer correctness or independent
review. Selection uses parsed references and declared scope, not inferred execution
order; string-valued dispatch is not promoted into a resolved call relationship.

This is a query-only change. It reuses the previous candidate's immutable indexes
without rebuilding, adding embeddings or changing chunk sizes. Separate windows
from one long method and distinct overloads remain eligible evidence. Existing
result/context limits still apply, so smaller budgets can omit required passages.
Bounds and ambiguity leave the original order unchanged. This evaluation does not
establish large-project latency or memory limits.

See the [pre-evaluation scope](multipart-code-v1-plan.md) and
[machine-readable report](multipart-code-v1.json). Reproduce using the original
lock and previous candidate's workspace bindings in the corpus README. The
portable report removes only absolute project/Git-root paths, retains the full
local report hash, and records comparisons against both earlier reports. The
implementation hash identifies the exact local source bytes used in this run;
Git line-ending normalization can change it in another checkout. Timings are
descriptive local measurements.


Input status: **completed**. Scope: **independent-candidate**.
Frozen input SHA-256: `2bb402c51a5d368b026253d3aea22d5fb6e9c6678f350d8d2e4535d8068d5bfd`.
Implementation SHA-256: `87cf8cff0b24de84721224a85b6c0b012b1adcb241ff413aa7c6f1ec137650e8`.

Counts include failed cases. Essential coverage requires every labeled group in the final evidence budget.
JSON contains descriptive 95% Wilson intervals; questions from the same project are correlated.

| Split / project / category | Questions | Essential retrieved | Essential packed | No-evidence outcome | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| split: development | 40 | 36/36 | 36/36 | 4/4 | 0 |
| split: held-out | 20 | 15/16 | 15/16 | 2/4 | 0 |
| project: Path-Creator | 20 | 18/18 | 18/18 | 2/2 | 0 |
| project: UnityPlayground | 20 | 18/18 | 18/18 | 2/2 | 0 |
| project: UnityBubbleGame | 20 | 15/16 | 15/16 | 2/4 | 0 |
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
| category: unsupported-asset | 2 | not measured | not measured | 0/2 | 0 |

## Failures

| Project | Case | Category | Diagnosis | Missing packed groups |
| --- | --- | --- | --- | --- |
| UnityBubbleGame | stone-vs-freeze | cross-file | retrieval_selection | 2 |
| UnityBubbleGame | binary-prefab-reference | unsupported-asset | no_evidence_outcome | — |
| UnityBubbleGame | binary-pool-setting | unsupported-asset | no_evidence_outcome | — |

An index-representation miss means the required literal passage was absent from stored evidence; it does not by itself distinguish ingestion, mapping, transformed asset facts, or chunk boundaries.

## Acceptance

Held-out retrieval gate passed: **False**.
Independent corpus prerequisites met: **False**.
Generated-claim support and explanation abstention: **not evaluated**. This runner uses extractive answers and makes no generation calls. The 95% supported-claims target remains open.
Repository independence and reviewer declarations require external review; a manifest cannot prove them.
