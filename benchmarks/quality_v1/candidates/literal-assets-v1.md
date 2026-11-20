# Candidate: literal asset evidence and explicit lookup

Compared with the preserved [baseline](../baseline.md), complete essential evidence
improves from **44/52 to 48/52** and no-evidence controls from **4/8 to 6/8**.
Six cases improve and none regress. Development coverage improves from 29/36 to
33/36; held-out coverage remains 15/16. All 60 cases complete without runtime errors.

The four literal serialized-asset cases now pass. Three development code-behavior
questions and three held-out cases still fail. No tuning used the candidate's
held-out failures. The retrieval and production gates remain failed; agent-reviewed
labels and extractive evidence delivery do not establish generated-answer accuracy.

Added asset-source chunks: 328 in Path Creator, 2,132 in Unity Playground, and 12
in Unity Bubble Game. Total evidence chunks grow from 883 to 1,211, 10,463 to
12,595, and 1,353 to 1,365 respectively. This increases index storage and optional
unrestricted embedding work; this run does not establish a large-project memory
or latency budget.

See the [pre-evaluation scope](literal-assets-v1-plan.md) and
[complete machine-readable report](literal-assets-v1.json). Reproduce with the
original lock and candidate workspace bindings described in the corpus README.
The portable report removes only absolute project/Git-root paths and retains
the full local report hash. Case timings are descriptive local measurements.


Input status: **completed**. Scope: **independent-candidate**.
Frozen input SHA-256: `2bb402c51a5d368b026253d3aea22d5fb6e9c6678f350d8d2e4535d8068d5bfd`.
Implementation SHA-256: `15e88d87b0bb3c9953b0444b381d8c51c6e7a75f80ecf17f51d1d41c35369fb5`.

Counts include failed cases. Essential coverage requires every labeled group in the final evidence budget.
JSON contains descriptive 95% Wilson intervals; questions from the same project are correlated.

| Split / project / category | Questions | Essential retrieved | Essential packed | No-evidence outcome | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| split: development | 40 | 33/36 | 33/36 | 4/4 | 0 |
| split: held-out | 20 | 15/16 | 15/16 | 2/4 | 0 |
| project: Path-Creator | 20 | 17/18 | 17/18 | 2/2 | 0 |
| project: UnityPlayground | 20 | 16/18 | 16/18 | 2/2 | 0 |
| project: UnityBubbleGame | 20 | 15/16 | 15/16 | 2/4 | 0 |
| category: absent-symbol | 6 | not measured | not measured | 6/6 | 0 |
| category: behavior | 14 | 14/14 | 14/14 | not measured | 0 |
| category: configuration | 1 | 1/1 | 1/1 | not measured | 0 |
| category: cross-file | 1 | 0/1 | 0/1 | not measured | 0 |
| category: lifecycle | 5 | 5/5 | 5/5 | not measured | 0 |
| category: multi-part | 11 | 8/11 | 8/11 | not measured | 0 |
| category: persistence | 3 | 3/3 | 3/3 | not measured | 0 |
| category: physics | 5 | 5/5 | 5/5 | not measured | 0 |
| category: relationships | 4 | 4/4 | 4/4 | not measured | 0 |
| category: serialized-asset | 4 | 4/4 | 4/4 | not measured | 0 |
| category: timing | 4 | 4/4 | 4/4 | not measured | 0 |
| category: unsupported-asset | 2 | not measured | not measured | 0/2 | 0 |

## Failures

| Project | Case | Category | Diagnosis | Missing packed groups |
| --- | --- | --- | --- | --- |
| Path-Creator | replace-bezier | multi-part | retrieval_selection | 1 |
| UnityPlayground | shot-cooldown | multi-part | retrieval_selection | 1, 2 |
| UnityPlayground | destruction-delay | multi-part | retrieval_selection | 2 |
| UnityBubbleGame | stone-vs-freeze | cross-file | retrieval_selection | 2 |
| UnityBubbleGame | binary-prefab-reference | unsupported-asset | no_evidence_outcome | — |
| UnityBubbleGame | binary-pool-setting | unsupported-asset | no_evidence_outcome | — |

An index-representation miss means the required literal passage was absent from stored evidence; it does not by itself distinguish ingestion, mapping, transformed asset facts, or chunk boundaries.

## Acceptance

Held-out retrieval gate passed: **False**.
Independent corpus prerequisites met: **False**.
Generated-claim support and explanation abstention: **not evaluated**. This runner uses extractive answers and makes no generation calls. The 95% supported-claims target remains open.
Repository independence and reviewer declarations require external review; a manifest cannot prove them.
