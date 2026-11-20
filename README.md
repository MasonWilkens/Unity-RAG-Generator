# Unity RAG

A local-first command-line tool for building searchable knowledge from a Unity
repository. It inventories source files, maps C# symbols and serialized asset
relationships, and returns source-backed evidence through lexical, structured,
optional semantic and hybrid retrieval.

The current package is experimental. Literal excerpts are inspectable source
evidence; model-generated explanations are drafts requiring source review.

## Quick start

Use Python 3.10+ with SQLite FTS5 support and a .NET SDK for C# mapping. A Unity
Editor and an API key are not needed to index text-serialized assets or retrieve
source. Clone this repository, then install it:

```sh
python -m pip install .
unity-rag --version
unity-rag creation-preflight --project ../YourUnityProject --workspace ../UnityKnowledge/YourGame
unity-rag refresh --project ../YourUnityProject --workspace ../UnityKnowledge/YourGame --no-semantic
unity-rag retrieve "player movement speed" --workspace ../UnityKnowledge/YourGame --mode search
unity-rag ask "What changes damage?" --workspace ../UnityKnowledge/YourGame --mode search --style extractive
```

The Unity root needs `Assets` and `ProjectSettings`. Each workspace belongs to one
project and must be outside that project. The tool reads project files and writes
its index into the workspace. Run `refresh --workspace ...` after edits.
Use `python -m unity_rag` in place of `unity-rag` when preferred.

## Retrieval and chunking

- Discovery records paths, file kinds, hashes, assembly declarations and coverage
  diagnostics. Generated directories, links, sensitive paths and unsupported
  content are excluded or explicitly reported. Git LFS pointers need materializing.
- A bundled Roslyn helper maps C# declarations, signatures, references and source
  ranges. It builds with the selected SDK, without building or executing the Unity
  project. Supplied reference DLLs are read as metadata.
- YAML parsing maps asset GUIDs, serialized objects, component/script links and a
  conservative subset of prefab overrides. Inferred and unsupported relationships
  remain labeled rather than being promoted to confirmed Unity behavior.
- Structured source records preserve entity IDs and literal citations. Bounded,
  overlapping chunks retain parent identity; optional token-based chunk policies
  are available. Semantic inputs use overlapping passage windows. Final answer
  packing selects bounded, question-relevant source windows and verifies hashes.
- Lexical search uses SQLite FTS5; exact lookup supports paths, GUIDs and qualified
  symbols. Semantic search uses optional local embeddings. Hybrid ranking combines
  available signals while preserving source provenance.

```sh
unity-rag retrieve "Game.Enemy.CanAttack" --workspace ../UnityKnowledge/YourGame --mode exact --json
unity-rag retrieve "Assets/Enemy.cs" --workspace ../UnityKnowledge/YourGame --mode used-by --json
unity-rag coverage --workspace ../UnityKnowledge/YourGame --path Assets/Enemy.cs
unity-rag workspace-status --workspace ../UnityKnowledge/YourGame
```

## Optional semantic search and explanations

```sh
python -m pip install ".[semantic,tokens]"
unity-rag refresh --workspace ../UnityKnowledge/YourGame --semantic
unity-rag retrieve "How does weapon cooling work?" --workspace ../UnityKnowledge/YourGame --mode hybrid
unity-rag ask "How does weapon cooling work?" --workspace ../UnityKnowledge/YourGame --mode hybrid --model YOUR_INSTALLED_OLLAMA_MODEL --show-sources
```

FastEmbed downloads its model on first use; embedding and retrieval then run
locally. The default explanation adapter uses a locally configured Ollama model.
Optional OpenAI generation requires explicit remote-transfer permission and local
credential/spending configuration. See [OpenAI behavior](OPENAI.md). Its current
pricing validity window must be reviewed before new hosted use.

Generated claims must cite supplied evidence. Unknown citations, source changes,
malformed responses and truncated output are rejected. These checks do not prove
factual correctness. Runtime behavior, live Inspector state and performance need
runtime evidence; initializers and serialized defaults cannot establish them.

## Maintained workspaces

Refresh stages a new generation, checks sources and configuration, then publishes
it atomically. Readers pin one completed generation. Failed or interrupted work
preserves the previous active snapshot. Supported incremental paths reuse maps
or embeddings; uncertain changes fall back to full mapping. Old generations are
retained; retention inspection does not automatically delete them.

Preflight checks local prerequisites, not full Unity compatibility or enough disk
space for every project. Assembly/platform rules and prefab variants are only
partially modeled. Binary assets, live spreadsheets, texture/audio contents and
unmaterialized LFS objects are not general-purpose retrieval evidence. Indexes
contain source text and should be treated like the original project.

The separate `index`/`search` commands are a legacy keyword prototype and do not
use the maintained mapping pipeline described above.

## Development and release checks

```sh
python -m pip install -r requirements/test.txt
python scripts/run_checks.py --jobs 4
```

The runner requires test prerequisites and fails on skipped tests. Synthetic
fixtures cover mapping, ranking, source checks, model-output contracts and workspace
recovery. Tests do not require private repositories, credentials or model inference.

See [release preparation](RELEASE.md) for isolated wheel/source installation checks
and the Windows/Linux CI matrix. [Evaluation inputs](tests/evaluation/README.md)
explain the public synthetic data and the limits of the recorded results.

- [Development stages](DEVELOPMENT_PLAN.md)
- [Production-readiness plan](PRODUCTION_READINESS_PLAN.md)
- [Refinement roadmap](REFINEMENT_PLAN.md)
- [Benchmark and evidence budgets](BENCHMARK.md)
- [Semantic indexing](SEMANTIC_INDEXING.md)
- [Workspace creation improvements](RAG_CREATION_PLAN.md)
