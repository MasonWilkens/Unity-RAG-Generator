# Cross-file evidence and unavailable serialization candidate

This batch is developed against new synthetic fixtures, without inspecting or
tuning against individual held-out questions. The original v1 questions, labels,
sources, budgets and acceptance thresholds stay unchanged.

- Select a caller passage and complementary implementation from another file
  through one resolved call edge. Require a unique named caller type, literal
  call-site coverage and additional parsed identifier coverage in the target.
  Keep overload identities; do not infer string dispatch, unresolved calls,
  interface implementations or virtual dispatch. Preserve existing anchors and
  bounded result/context budgets. Include both files and partial declarations as
  freshness dependencies. Ambiguous or oversized searches keep existing ordering.
- Recognize narrow questions about saved/serialized values in a named Unity asset.
  Use the saved inventory and map to report unreadable, unparsed or ambiguous
  targets. Do not substitute related code defaults for unavailable serialized
  values. Expose the reason in retrieval and both answer styles; do not call a
  generation provider when no qualifying source can be supplied. Diagnostics
  describe the indexed snapshot, not the current Editor or unseen project files.
- Exercise resolved delegation, overloads, long target methods, stale source
  dependencies, string/comment decoys, polymorphic calls, duplicate names,
  supported assets, malformed YAML, binary files and unmaterialized LFS pointers.

This is one-hop evidence selection, not execution tracing or a guarantee that an
answer is complete. Unavailable-source handling is restricted to identifiable
single-asset serialization questions. General code and structured lookup queries
keep their existing behavior. No index rebuild, generation or embedding inference
is required for this batch.

The caller scope is bounded to 200 chunks, 200 calls per source entity and 1,000
inspected call/chunk combinations; target reads use the existing 100-chunk,
1,000-reference and 64-dependency-file bounds. The shared reader also caps records
and serialized characters. At most two target chunks are added alongside their
caller. Asset name resolution examines at most 10,000 serialized inventory rows;
larger inventories leave the existing ordering unchanged. Explicit code-loading
or parsing questions are outside the saved-value guard.

The new fixture checks pass. All 40 frozen development cases also pass without
changed labels, settings or indexes. These checks precede held-out evaluation.

Freeze implementation before a single complete v1 evaluation. Publish comparisons
with the previous multipart candidate and original baseline, retaining all errors
and failures. Improvements on a repeatedly observed held-out corpus are regression
evidence, not a new independent generalization result. Human review and generated
claim support remain separate unmeasured gates.
