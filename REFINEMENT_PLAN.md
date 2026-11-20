# Unity RAG refinement roadmap

The objective is reliable creation of useful RAG workspaces across Unity repositories.
The implementation already supports the core discovery, mapping, retrieval, answer
and refresh stages. Refinement must be measured against fixed source evidence.

## 1. Freeze evaluation and diagnose failures

Version questions, source hashes, target entities, decisive passages, negative
controls and budgets before tuning. Separate development, same-project holdout
and independent-project holdout. Distinguish missing ingestion, wrong mapping,
ranking, evidence packing and generation errors.

Exact-name lookup and synthetic regression controls are implemented. Broader
independent evaluation remains open; see [benchmark guidance](BENCHMARK.md).

## 2. Make semantic builds predictable

Bound batches and staging memory, preserve durable per-batch checkpoints, reuse
verified passage embeddings, and test cancellation/restart without replacing a
healthy index. Record build phases, peak memory and elapsed time on stated hardware.
Existing bounded/checkpointed builds need broader large-project measurements.

## 3. Improve ranking and evidence selection

Keep exact symbol/GUID/path behavior stable. Measure lexical, semantic and hybrid
ranking on identical snapshots and budgets. Account for identifier splitting,
qualified names, behavior intent and bounded relationships. Guard against
irrelevant near-name matches and directional mistakes in behavioral questions.

Retain decisive source passages inside final context limits. Prefer literal
source evidence and preserve uncertainty for inferred or derived relationships.
Changes should improve labeled failure cases without regressing controls.

## 4. Improve chunking and Unity coverage

Preserve symbols, parent identities, physical ranges and source hashes when
splitting long content. Measure statement-aware boundaries, overlap and embedding
model token limits. Make unsupported single lines/content visible.

Broaden tested C# assembly/define/reference configurations and asset relationships.
Use observed failures to prioritize prefab variants, nested instances, overrides
and package boundaries. Unsupported cases must remain explicit.

## 5. Harden maintained workspaces

Require supported incremental paths to match clean rebuilds. Test edits, additions,
deletions, renames, changed metadata, schema/version changes, concurrent writers,
interruption and recovery. Keep publication atomic and readers pinned to one
completed generation. Define retention and upgrade policy before destructive cleanup.

## 6. Separate generated drafts from approval

Validate citations and current sources, enforce bounded requests, represent missing
evidence and reject malformed/truncated responses. Preserve model failures for
review. Model-assisted review does not constitute human approval or proof of
runtime behavior. Keep hosted transfer explicit and local retrieval key-free.

## 7. Release and independent pilot

Build and audit distributable packages, test fresh installations outside the
checkout and run CI from committed source. Collect performance and usability
evidence from independent developers. The complete release gates and proposed
quality targets are in [the production plan](PRODUCTION_READINESS_PLAN.md).

Synthetic fixtures and historical development observations are diagnostic controls;
they do not close the independent quality or production-readiness gates.
