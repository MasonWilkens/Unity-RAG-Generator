# Bounded two-hop dependency evidence

PR #6 is merged at `a3b84a23778aa66e2d80df3e7732e85951d79e98`. This separate
batch extends static dependency retrieval to exactly two resolved method calls.
Freeze six new source-passage cases and sixteen fallback controls before changing
runtime code. Sources and labels are bound by
`tests/fixtures/dependency-chain-v1/lock.json`; record the baseline and preserve
every attempt. Keep all quality_v1 labels, settings, thresholds and indexes fixed.

For a question naming one unambiguous caller type, select its relevant caller
chunk, the intermediate chunk containing the next call, and up to two endpoint
chunks with additional requested identifier coverage. An intermediate method can
lack query terms; the literal two-call path is its reason for inclusion. Preserve
namespaces, overload identities, partial declarations, literal source ranges and
freshness dependencies for every hop. Existing exact, structured, behavior,
one-hop and comparison priorities and result/context budgets remain authoritative.

Require both call edges to resolve to indexed executable methods. Do not infer
virtual/interface/override dispatch, delegates, string dispatch, missing targets,
argument flow, runtime order or execution. Reject cycles and equally supported
competing identity paths. Stop after two edges. Bound root chunks, expanded
members, call-site inspections, candidate paths, reference reads and dependency
files. Exceeding a bound preserves ordinary ordering. No index rebuild or provider
inference is required for existing workspaces.

Development cases exercise an otherwise irrelevant bridge, overload/namespace
identity, partial sources, one-hop extension, same-owner helpers, long bridges,
split endpoints and nonvirtual instance methods. Negative cases include competing
paths, cycles, a three-hop-only answer, unresolved/polymorphic/delegate/string
calls, redundant endpoints and comment/string decoys. Unit checks additionally
cover small budgets, semantic/hybrid integration, stale intermediate files,
out-of-window edges and exhausted bounds.

Exit checks: every labeled caller, intermediate call and endpoint passage arrives
within limit 5 / expand 0 / 24,000 characters; all fallback controls and previous
comparison fixtures pass. Check the existing forty development questions before
freezing runtime code for one full sixty-question regression run. Retain failures
without tuning against held-out cases. Publish reports, run the full local suite
and clean-checkout Windows/Linux CI, and open a separate PR for review.

This validates static source evidence, not execution paths or generated-answer
accuracy. Independent review and fresh acceptance evaluation remain later work.

```powershell
python scripts/evaluate_dependency_chains.py --workspace .unity-rag/dependency-chain-workspace --output .unity-rag/dependency-chain-candidates/baseline.json --refresh
# Reuse the immutable workspace after implementation; preserve each attempt:
python scripts/evaluate_dependency_chains.py --workspace .unity-rag/dependency-chain-workspace --output .unity-rag/dependency-chain-candidates/attempt-1.json
```
