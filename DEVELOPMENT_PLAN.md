# Unity RAG Builder — Development Plan

## Product goal

Point the tool at a Unity repository, automatically map its code and assets, and create a persistent knowledge system that answers project-specific questions with source references.

The initial product is a local command-line application. Repository analysis works without opening Unity. Optional Editor integration comes later.

## Stage 1 — Discover and inventory the Unity project

- Validate project structure and identify Unity version, package declarations, assembly definitions, and Git state.
- Inventory scripts, scenes, prefabs, ScriptableObject candidates, materials, shaders, documentation, metadata, and other assets.
- Configure included folders, exclusions, file-size limits, and external package handling.
- Identify binary files, missing files, unreadable content, and unsupported formats.
- Create a persistent workspace with configuration, file hashes, and scan results.
- Leave the source project unchanged; keep generated data in a configurable separate location.

Deliverable: inventory with counts, coverage, exclusions, and diagnostics.

Completion: a known sample project produces the expected inventory; skipped or unsupported candidates have explicit reasons.

## Stage 2 — Map the C# code

- Extract namespaces, types, inheritance declarations, methods, properties, fields, attributes, comments, signatures, and source locations.
- Identify Unity components and serialized-field declarations where resolvable.
- Record assembly boundaries and compilation conditions.
- Resolve symbol references and calls when compilation context is available.
- Explicitly mark unresolved or ambiguous relationships.
- Use Roslyn, retaining syntax analysis when dependencies prevent semantic resolution.

Deliverable: searchable code map for definitions and references.

Completion: fixtures cover overloads, partial classes, inheritance, conditional compilation, and missing dependencies; uncertain relationships are never presented as confirmed.

## Stage 3 — Map assets and connect them to code

- Map GUIDs from .meta files to paths.
- Parse supported text-serialized scenes, prefabs, and assets.
- Identify serialized objects by asset identity and local file ID.
- Connect GameObjects, components, scripts, and referenced assets.
- Extract serialized values and their source locations.
- Record prefab source relationships and overrides.
- Resolve a documented subset of prefab inheritance; flag unsupported cases.
- Describe binary assets through metadata without claiming to understand their contents.
- Keep script initializers, serialized values, resolved overrides, and runtime observations distinct.

Deliverable: combined code-and-asset map.

Completion: hand-verified fixtures cover nested prefabs, variants, missing references, and duplicate names.

### Milestone A — Trustworthy Unity project mapper

Useful repository exploration without a language model, with provenance and visible coverage limits.

## Stage 4 — Build structured and keyword retrieval

- Chunk code around methods and classes with surrounding context.
- Create readable asset records with names, components, settings, and references.
- Preserve source locations, project identity, hashes, and relationship provenance.
- Add identifier-aware keyword matching and exact symbol/GUID/path lookups.
- Add references, used-by, and inheritance queries.
- Expand relevant hits with related code or assets.
- Distinguish original source, parsed facts, and derived interpretations.

Deliverable: human-readable search and machine-readable evidence interface.

Completion: versioned representative questions retrieve expected supporting evidence; establish baseline measurements.

## Stage 5 — Add semantic and hybrid retrieval

- Add interchangeable embedding providers.
- Cache embeddings by content hash and model configuration.
- Combine semantic retrieval, keywords, and structured lookups.
- Deduplicate evidence and evaluate reranking and relationship expansion.
- Budget context and record ranking diagnostics.
- Version indexes when embedding configuration changes.

Deliverable: hybrid retrieval for natural-language questions.

Completion: improve conceptual-question retrieval without degrading exact-symbol performance; retain extra components only when measured value justifies them.

## Stage 6 — Build the first complete RAG assistant

- Add a configurable model adapter and ask command.
- Require citations for project-specific factual claims.
- Distinguish facts, interpretations, and missing evidence.
- Bound follow-up retrieval across multiple files.
- Validate citations against the indexed snapshot.
- Treat retrieved text as evidence, never authority.
- Identify questions requiring Editor or runtime information.
- Make remote transfer of project content explicit in provider configuration.

Deliverable: working local RAG assistant with inspectable evidence.

Completion: evaluate correctness, support, citations, and abstention, including misleading comments, contradictions, incomplete evidence, and unanswerable questions.

### Milestone B — First working Unity RAG system

Select a repository, build a workspace, search it, and ask grounded questions. A polished interface is not required.

## Stage 7 — Keep knowledge current

- Detect additions, modifications, renames, and deletions.
- Update parsing, embeddings, relationships, and derived records.
- Handle branch switches and dirty working trees.
- Update atomically to avoid mixed snapshots.
- Recover interrupted indexing.
- Provide status, refresh, rebuild, and diagnostics.
- Add optional watching after explicit refresh is reliable.

Deliverable: maintained workspace with freshness and health reporting.

Completion: incremental updates equal clean rebuild results after edits, deletions, renames, and branch changes.

## Stage 8 — Package the local product

- Installation and dependency checks.
- Project selection and provider configuration.
- Isolated workspaces for multiple projects.
- Progress, coverage, failures, and freshness.
- Human-readable and machine-readable output.
- Supported Unity versions and serialization features.
- Performance measurements on representative projects.
- Repeatable releases and upgrades.

Deliverable: installable local application with documented limits.

Completion: a new user installs, indexes, asks, edits, refreshes, and verifies updated answers without developer assistance.

### Milestone C — Maintainable local product

Repeatable, diagnosable workflow suitable for evolving repositories.

## Stage 9 — Optional Unity Editor integration

- Resolve assets and prefab state through Unity APIs.
- Capture Editor scene/component information.
- Navigate to sources and assets.
- Include selected logs or runtime observations.
- Compare repository-derived relationships with Editor results.

Deliverable: optional companion package enriching the same workspace.

Completion: preserve distinct provenance and timestamps; never present runtime observations as permanent project facts.

## Architecture and validation

- Separate discovery, parsing, storage, retrieval, and generation.
- Python orchestration; focused .NET/Roslyn C# analysis component.
- SQLite for inventory, relationships, and keyword search.
- Replaceable vector storage and model adapters.
- Deterministic identities and relationships wherever possible.
- Preserve original evidence beneath summaries.
- Add evaluation fixtures alongside every stage.
- Read-only access to the target project.

Immediate scope: Stage 1 discovery, configuration, inventory storage, coverage reporting, and verification fixtures. Complete code and asset maps before expanding the keyword prototype into full RAG.

Technical references:
- https://learn.microsoft.com/dotnet/csharp/roslyn-sdk/compiler-api-model
- https://docs.unity.com/en-us/engine/6000.3/manual/working-with-scenes/text-scene-format/yaml-prefab-serialization

## Implementation status

- Stage 1: discovery and persistent inventory implemented and tested with synthetic Unity fixtures.
- Stage 2: initial Roslyn code mapper implemented. Declarations, attributes, comments, source locations, partial/overloaded symbol identities, assembly boundary metadata, preprocessor evidence, per-assembly references/calls, diagnostics, and saved lookup commands are available.
- Stage 2 follow-up: validate against a real Unity project; reconstruct Unity compilation profiles and source assembly dependencies; broaden serialized-field type validation and syntax-version coverage. Resolved bindings currently refer to the supplied analysis context, not guaranteed Unity compilation parity.
- Stage 3: initial asset mapper implemented. GUID inventory, serialized objects and values, local/cross-file references, script-to-code evidence, saved inspection commands, and direct scalar prefab overrides are covered by synthetic fixtures.
- Stage 3 follow-up: validate against a real project; expand prefab chains, structural and object-reference overrides, imported sub-assets, and performance coverage. Filename-to-class links remain explicitly inferred.
- Stage 4: map-backed evidence indexing, identifier-aware BM25, exact aliases, direct relationship queries, bounded context expansion, source/hash provenance, stale-index checks, and a versioned synthetic evaluation set are implemented.
- Stage 4 follow-up: real-project retrieval evaluation, broader judgments, and scale benchmarks. The original index/search commands remain a separate raw-text prototype.
- Stages 5–9 remain planned.
