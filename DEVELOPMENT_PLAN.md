# Unity RAG development plan

Goal: point a local tool at a Unity repository, map its code and assets, and create
a maintained RAG workspace with inspectable source evidence. Ingestion, retrieval
and answer generation remain separate so providers and interfaces can change.

## Stages and current status

1. **Project discovery — implemented.** Inventory source and asset files, capture
   Unity/package/assembly metadata, hashes and coverage gaps, and exclude generated
   or sensitive content without modifying the Unity project.
2. **C# mapping — implemented with stated limits.** Use Roslyn to record symbols,
   source ranges, signatures, serialized-field candidates, inheritance and reference
   edges. Bindings depend on supplied defines/references and the selected SDK;
   the tool does not reproduce every Unity compilation rule.
3. **Asset mapping — implemented with stated limits.** Read Unity YAML and metadata,
   resolve GUIDs and file IDs, connect components and scripts, and preserve
   unresolved/ambiguous relationships. Conservative scalar prefab overrides are
   supported; general nested/variant/structural resolution remains incomplete.
4. **Structured and lexical retrieval — implemented.** Build source-backed evidence
   and relationship indexes with exact names, qualified overload lookup, keywords,
   paths, GUIDs and bounded related context. Evaluate target and passage coverage.
5. **Local semantic and hybrid retrieval — implemented.** Use an interchangeable
   embedding adapter, passage windows, cache reuse, durable checkpoints, bounded
   batches, cancellation handling and fixed-input ranking comparisons. Broader
   independent quality and scale evidence remain open.
6. **Grounded answers — closed as a review-required prototype.** Support literal
   excerpts and optional model-generated explanations with citations, typed evidence
   gaps, bounded requests and source freshness checks. Generated claims and gaps
   remain drafts. Passing citation contracts or model-assisted review does not
   establish correctness, completeness or runtime behavior.
7. **Maintained workspaces — implemented.** Publish immutable generations atomically,
   pin readers, preserve prior snapshots on failed refresh, detect source/config
   changes and reuse supported maps/embeddings. Retention previews and explicit
   idle-refresh tickets are available; unsupported cases use conservative fallbacks.
8. **Release and developer experience — active.** Package the bundled analyzer,
   document prerequisites and limitations, verify clean installations, and run
   automated checks from a committed checkout on Windows and Linux.
9. **Optional Unity Editor integration — future.** Export more accurate compilation
   or runtime context only through explicit Editor integration. A general Editor
   interface and live-state answers are outside the current local CLI milestone.

## Current implementation boundaries

- Source/metadata hashes and exact citations support provenance checks. They are
  point-in-time observations, not locks on the entire Unity project.
- Supported incremental documentation, assembly and asset edits must match a
  clean rebuild; missing or changed provenance triggers the full fallback.
- Inactive C# branches, unsupported assets and uncertain references are reported.
  Configuration and platform constraints require broader Unity compatibility work.
- Optional draft batches use explicit manifests, persistent spending reservations,
  deadlines and human review. Generated code is not executed. The old project-bound
  pilot CLI is disabled; reusable batches use `unity_rag.backlog` with explicit inputs.
- No background watcher, paid run, generation deletion or publishing happens merely
  by installing the package.

## Validation and next milestones

The public suite uses synthetic Unity repositories and mocked generation providers.
It checks implementation behavior, including failures and recovery. Historical
private-project experiments remain local; see [evaluation scope](tests/evaluation/README.md).

[Release preparation](RELEASE.md) defines package and clean-checkout checks.
[Production readiness](PRODUCTION_READINESS_PLAN.md) sets the remaining independent
quality, Unity compatibility, durability, performance and pilot gates.
The [refinement roadmap](REFINEMENT_PLAN.md) records more detailed retrieval work.
