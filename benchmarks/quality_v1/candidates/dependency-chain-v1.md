# Candidate: bounded two-call dependency evidence

The new frozen development set improves from **0/6 to 6/6** complete passage
cases, with **20/20 required passages** delivered through retrieval and real
extractive-answer packing. All **16/16 fallback controls** pass. Both literal
call sites are required, including bridges that have no query terms. The
[development reports](dependency-chain-development/) retain the baseline and
both attempts, plus the previous comparison-set regression (9/9 passage cases
and 12/12 controls). The second attempt adds a default-interface safeguard;
frozen source and label bytes were not changed.

The unchanged public regression corpus remains at **59/60** overall outcomes,
**51/52** complete essential evidence, and **8/8** no-evidence controls. All
**40/40** development cases pass. There are no runtime errors and no improvements
or regressions from the [two-type comparison candidate](type-comparison-v1.md).
Compared with the original baseline, eleven cases improve and none regress.
The remaining failure stays below; no runtime changes followed this evaluation.

For a question naming one unambiguous caller type, selection follows exactly two
resolved method calls and supplies the caller, intermediate call site and up to
two complementary endpoint chunks. It preserves canonical overload/namespace
identities and freshness dependencies for selected hops and partial declarations.
The endpoint must add requested identifier coverage. Nonvirtual instance
methods and same-owner helpers are supported. Existing protected anchors retain
priority, and result/context budgets are unchanged; small budgets can omit hops.

Cycles, equally supported identity paths compatible with existing anchors,
unresolved calls, missing bodies, virtual/override/interface dispatch, delegates
and string dispatch do not gain inferred implementations. Default interface
bodies are rejected even without an explicit virtual modifier. These are static
source dependencies, not execution order, argument flow, runtime dispatch or a
guarantee that the selected implementation executes.

Traversal is bounded to 200 root chunks, 64 visited member identities, 200 calls
per member, 1,000 call-site/chunk inspections and 128 two-edge candidate paths.
Target reads allow at most 100 chunks and reference reads at most 1,000 rows per
member; the shared reader caps total records at 7,000 and serialized characters
at 20 million. Freshness dependencies are limited to 64 files. Exceeding a bound
preserves original ordering. No index rebuild or provider inference is needed.

The retrieval gate passes while the production gate remains open. Synthetic
fixtures establish mechanism behavior, and this reused corpus provides regression
evidence. Independent source-label review, fresh acceptance data and generated
claim evaluation remain separate work. Timings describe this local run, not
validated performance limits for large projects.

See the [pre-implementation scope](dependency-chain-v1-plan.md) and
[machine-readable report](dependency-chain-v1.json). Original corpus labels,
budgets and snapshots remain unchanged. Portable reports remove only paths listed
in `redactions` and retain full report hashes. Implementation hashes bind exact
executed bytes and can differ after Git line-ending normalization.


Input status: **completed**. Scope: **independent-candidate**.
Frozen input SHA-256: `2bb402c51a5d368b026253d3aea22d5fb6e9c6678f350d8d2e4535d8068d5bfd`.
Implementation SHA-256: `b901edd5f0c497c2ed82b57eb48e85cd81c98459d5f0e80389cde8985326b49a`.

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
