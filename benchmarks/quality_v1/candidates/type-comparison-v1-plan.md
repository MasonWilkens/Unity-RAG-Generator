# Two-class comparison retrieval: first batch

Freeze new synthetic sources, source-line labels, controls and budgets before
changing retrieval. The lock in `tests/fixtures/type-comparison-v1/lock.json`
binds nine positive passage cases and twelve conservative fallback controls to
the merged baseline commit. Preserve the baseline and every subsequent attempt.
The corpus covers reversed names, qualified names and namespace collisions,
overloads, partial declarations, separate members, long split methods, properties,
missing types, ambiguous implementations and comment/string/field decoys.

Implement a separate, bounded selector for explicit comparisons between exactly
two named types. Resolve each identity independently; reject collisions instead
of weakening the existing single-type resolver. Reserve relevant executable
source from each side before adding complementary passages. Parsed identifier
references explain selection; they do not establish behavioral equivalence,
differences, runtime order or a call relationship. Preserve overload identities,
literal citations, partial/source freshness dependencies, exact lookup, kind
filters and existing result/context limits. Tiny budgets may omit a side; never
silently enlarge the budget or label selection as answer completeness.

No deeper call traversal, new embeddings, generation, asset changes or index
rebuild for existing workspaces belongs in this batch. Unsupported intent,
ambiguity and exhausted bounds preserve existing ordering.

Exit checks: all new source-passage and fallback controls pass; both requested
implementations survive real answer packing at limit 5 / expand 0 / 24,000
characters. Exercise semantic/hybrid integration with deterministic mocked scores,
freshness failures, exact lookup, filters and smaller budgets in unit tests.
Run the existing forty development cases before freezing runtime code for one
unchanged sixty-case regression evaluation. Retain any failure without tuning
against held-out cases. Publish comparisons with the preceding cross-file
candidate, then run the full local suite and clean-checkout Windows/Linux CI.

The synthetic set is development evidence. Repeated use of quality_v1 provides
regression evidence, not fresh independent acceptance. Independent human labels,
generated-claim review and production approval remain separate later work.

Reproduce the development baseline/candidate with:

```powershell
python scripts/evaluate_type_comparisons.py --workspace .unity-rag/type-comparison-workspace --output .unity-rag/type-comparison-candidates/baseline.json --refresh
# After implementation, reuse the immutable workspace and choose a new output:
python scripts/evaluate_type_comparisons.py --workspace .unity-rag/type-comparison-workspace --output .unity-rag/type-comparison-candidates/candidate.json
```
