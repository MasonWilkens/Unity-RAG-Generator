# Quality benchmark: Public Unity evidence baseline v1

Input status: **completed**. Scope: **independent-candidate**.
Frozen input SHA-256: `a569e70b17a221df7edbb20fc4b13a2256ba35f500db29127ec8e0475aec18b4`.
Implementation SHA-256: `baefbb7c3cb15eaf71b195e67fc73cc8c69ebab6c1f03ca3a08917520e92985e`.

Counts include failed cases. Essential coverage requires every labeled group in the final evidence budget.
JSON contains descriptive 95% Wilson intervals; questions from the same project are correlated.

| Split / project / category | Questions | Essential retrieved | Essential packed | No-evidence outcome | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| split: development | 40 | 29/36 | 29/36 | 2/4 | 0 |
| split: held-out | 20 | 15/16 | 15/16 | 2/4 | 0 |
| project: Path-Creator | 20 | 15/18 | 15/18 | 1/2 | 0 |
| project: UnityPlayground | 20 | 14/18 | 14/18 | 1/2 | 0 |
| project: UnityBubbleGame | 20 | 15/16 | 15/16 | 2/4 | 0 |
| category: absent-symbol | 6 | not measured | not measured | 4/6 | 0 |
| category: behavior | 14 | 14/14 | 14/14 | not measured | 0 |
| category: configuration | 1 | 1/1 | 1/1 | not measured | 0 |
| category: cross-file | 1 | 0/1 | 0/1 | not measured | 0 |
| category: lifecycle | 5 | 5/5 | 5/5 | not measured | 0 |
| category: multi-part | 11 | 8/11 | 8/11 | not measured | 0 |
| category: persistence | 3 | 3/3 | 3/3 | not measured | 0 |
| category: physics | 5 | 5/5 | 5/5 | not measured | 0 |
| category: relationships | 4 | 4/4 | 4/4 | not measured | 0 |
| category: serialized-asset | 4 | 0/4 | 0/4 | not measured | 0 |
| category: timing | 4 | 4/4 | 4/4 | not measured | 0 |
| category: unsupported-asset | 2 | not measured | not measured | 0/2 | 0 |

## Failures

| Project | Case | Category | Diagnosis | Missing packed groups |
| --- | --- | --- | --- | --- |
| Path-Creator | replace-bezier | multi-part | retrieval_selection | 1 |
| Path-Creator | follower-prefab-speed | serialized-asset | index_representation | 1 |
| Path-Creator | path-prefab-name | serialized-asset | index_representation | 1 |
| Path-Creator | missing-symbol-b | absent-symbol | no_evidence_outcome | — |
| UnityPlayground | shot-cooldown | multi-part | retrieval_selection | 1, 2 |
| UnityPlayground | destruction-delay | multi-part | retrieval_selection | 2 |
| UnityPlayground | bee-damage | serialized-asset | index_representation | 1 |
| UnityPlayground | laser-lifetime | serialized-asset | index_representation | 1 |
| UnityPlayground | missing-symbol-b | absent-symbol | no_evidence_outcome | — |
| UnityBubbleGame | stone-vs-freeze | cross-file | retrieval_selection | 2 |
| UnityBubbleGame | binary-prefab-reference | unsupported-asset | no_evidence_outcome | — |
| UnityBubbleGame | binary-pool-setting | unsupported-asset | no_evidence_outcome | — |

An index-representation miss means the required literal passage was absent from stored evidence; it does not by itself distinguish ingestion, mapping, transformed asset facts, or chunk boundaries.

## Acceptance

Held-out retrieval gate passed: **False**.
Independent corpus prerequisites met: **False**.
Generated-claim support and explanation abstention: **not evaluated**. This runner uses extractive answers and makes no generation calls. The 95% supported-claims target remains open.
Repository independence and reviewer declarations require external review; a manifest cannot prove them.
