# Unity RAG

A local-first foundation for software that answers questions about Unity projects using their source content.

The current version inventories Unity projects, maps C# code and serialized assets, and provides an initial keyword retrieval prototype. It does not yet generate answers or use embeddings. It runs locally without API keys. Asset parsing uses PyYAML; C# mapping additionally requires a .NET SDK.

## Quick start

Requires Python 3.10+ with SQLite FTS5 support.

Install the project and its parser dependency before running the commands:

    python -m pip install -e .

From this repository:

```powershell
python -m unity_rag index "F:\YourUnityProject"
python -m unity_rag search "player movement speed"
python -m unity_rag search "damage" --json
```

Optional installation into a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\unity-rag --help
```

An index holds one project. Use separate database paths for multiple projects:

```powershell
python -m unity_rag --db .unity-rag/game.sqlite3 index "F:\YourUnityProject"
python -m unity_rag --db .unity-rag/game.sqlite3 search "inventory"
```

## Current behavior

- Scans Assets, Packages, and ProjectSettings for supported source, documentation, shader, JSON, and text-serialized Unity asset files.
- Excludes generated directories, hidden directories, symbolic links, binary files, and files over 2 MB. Reports skipped candidate files.
- Splits text into up to 80-line chunks with 16-line overlap and ranks keyword matches with SQLite FTS5 BM25.
- Rebuilding replaces the previous index, removing stale content. Run indexing again after source changes.
- Stores the local index in `.unity-rag/`, excluded from Git. Indexes contain source text; treat them with the same care as the project.

Unity scenes and prefabs must be text serialized to be useful. The tool does not resolve GUID relationships, parse C# symbols, follow external packages, or honor project `.gitignore` rules yet. Keyword search may miss conceptual matches or split identifiers; retrieval quality needs evaluation before adding answer generation.

## Development

```powershell
python -m unittest discover -s tests -v
```

## Planned stages

1. Unity-aware retrieval: GUID-to-asset mapping, C# symbol chunks, configurable exclusions, incremental indexing.
2. Semantic retrieval: interchangeable local embedding providers, hybrid ranking, and a representative evaluation set.
3. Grounded answers: model adapter, cited responses, insufficient-evidence handling, and protection against instructions embedded in retrieved files.
4. Local interface: chat and project selection, followed by optional Unity Editor integration.

Keep source ingestion, retrieval, and answer generation separate so model providers and interfaces can change independently.

## Stage 1: project discovery

The roadmap is in [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md). Discovery inventories a project independently of the early retrieval prototype.

    python -m unity_rag scan "F:\YourUnityProject" --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag status --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag status --workspace "F:\UnityKnowledge\YourGame" --json

The workspace must be outside the target Unity project. The tool writes an inventory.sqlite3 database there and leaves the source project unchanged. One workspace belongs to one project. Status displays the last scan, not a live freshness check. Repeat scan to rebuild the inventory; incremental updates come in Stage 7.

The report contains Unity version, Git branch/commit/dirty state when available, package declarations, package lock data, assembly declarations, file categories, SHA-256 hashes for inspected files, and explicit exclusion/error reasons. Packages are reported without downloading or following external dependencies. Embedded package files under Packages are inventoried normally. Files with the .asset extension remain generic serialized assets until Stage 3 identifies their types.

Optional JSON configuration:

    {
      "include": ["Assets", "Packages", "ProjectSettings"],
      "exclude": ["Library", "Temp", ".git", ".env", ".env.*", "Assets/ThirdParty"],
      "max_file_bytes": 2000000,
      "external_packages": "report_only"
    }

Pass this using --config discovery.json. Configuration lists replace defaults. Exclusion patterns match project-relative paths (using forward slashes) or entry names, case-insensitively. Included directories must stay inside the project; links and Windows reparse points are excluded. Project .gitignore rules are not interpreted.

Excluded directories represent pruned subtrees, so counts do not include unseen children. Files above the size limit are recorded without reading or hashing them. Binary/non-UTF-8 files and unknown extensions receive explicit statuses. SHA-256 hashes describe inspected content, not an assertion that a file is supported.

Configuration and the full report are saved in the database. Currently pass --config again on each scan to reuse custom settings. Scans read files as visited, not as a filesystem-wide atomic snapshot; avoid editing during a scan when a consistent baseline matters. Supported text means inspected UTF-8 with a recognized extension, not completed semantic analysis. Git metadata describes the containing repository, including when the Unity project is a subdirectory.

The existing index and search commands remain the initial keyword retrieval prototype and do not yet consume discovery results. Code relationships and asset relationships are the next two stages. The discovery tests create small synthetic Unity projects in temporary directories; no Unity installation is needed to run them.

## Stage 2: C# code mapping

C# mapping requires a .NET SDK version 6 or newer on PATH, in addition to Python. Discovery and keyword retrieval still run without .NET. The mapper builds its bundled helper using Roslyn libraries already installed with the selected SDK; no NuGet packages are downloaded. Build outputs are stored in the knowledge workspace. It never builds or executes the target Unity project.

After discovery:

    python -m unity_rag map-code --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag symbols "Player" --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag code-status --workspace "F:\UnityKnowledge\YourGame" --json

Symbol output includes exact IDs. Use one to query resolved references, distinguishing overloads:

    python -m unity_rag references "Assembly-CSharp::M:Game.Player.Hit(System.Int32)" --workspace "F:\UnityKnowledge\YourGame"

Add --json to any of these commands for structured results. Code-map JSON includes declarations, signatures, modifiers, attributes, leading comments, inheritance declarations, physical source locations, reference edges, compiler diagnostics, and preprocessor directives/disabled text. Symbol IDs include the assembly and Roslyn documentation identity; partial declarations retain distinct source locations under the same ID.

To select compilation symbols or supply metadata references:

    python -m unity_rag map-code --workspace "F:\UnityKnowledge\YourGame" --define UNITY_EDITOR --define UNITY_STANDALONE_WIN --reference "F:\UnityAssemblies\UnityEngine.CoreModule.dll"

Repeat --define and --reference as needed. Reference DLLs are read as compiler metadata, not executed. No Unity symbols are assumed by default. C# 10 is the default language version; --language-version selects another version supported by the installed Roslyn. Inactive branches are preserved as text rather than mapped as active declarations. Run separate maps to inspect alternative configurations; each successful run replaces the previous map in that workspace.

### Scope and limitations

- Nearest assembly definitions/references assign source groups. Default runtime/Editor grouping is explicitly approximate. Platform filters, define constraints, version defines, first-pass assembly rules, and the exact Unity reference profile are not applied.
- Each source assembly is analyzed independently with host .NET references and explicit DLL references. Cross-assembly source binding is not automatic. References that require missing Unity or package assemblies remain unresolved.
- A resolved edge means Roslyn bound it in the supplied analysis context, not that Unity's actual compilation or runtime dispatch was reproduced. Candidate targets and compiler errors are preserved.
- Direct and inherited MonoBehaviour/ScriptableObject ancestry can be identified when the base symbols resolve. Missing bases are marked unresolved.
- Serialized-field results are candidates based on visibility and attributes. Unity's supported field-type rules and serialized asset values are not evaluated yet.
- C# mapping uses the saved discovery inventory. Modified/removed inventoried code, assembly declarations, or metadata requires a new scan. New files are only discovered by scanning again. A new scan invalidates lookup of the previous code map until map-code is rerun.
- Lookups inspect a saved map, not live source. The first implementation rebuilds the complete map; incremental updates remain Stage 7 work.
- Local variables, implicit compiler-generated operations, reflection, dynamic runtime targets, and complete XML documentation reference graphs are outside current mapping coverage.
- Large projects have not yet been benchmarked. The analyzer currently has a three-minute operation timeout.

The analyzer source is included in Python package data, so installed distributions retain mapping support. Integration tests use synthetic Unity-shaped C# fixtures, including small Unity type stubs, and require an installed SDK. Those stubs are test inputs only; real project analysis does not invent missing Unity types.

Roslyn API reference: https://learn.microsoft.com/dotnet/api/microsoft.codeanalysis.semanticmodel

## Stage 3: Unity asset mapping

Run discovery first. Run map-code before map-assets if you want C# class connections; asset-only mapping also works without a saved code map or .NET.

    python -m unity_rag scan "F:\YourUnityProject" --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag map-code --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag map-assets --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag assets "Ogre" --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag asset-show "Assets/Enemies/Ogre.prefab" --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag asset-users "Assets/Scripts/Ogre.cs" --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag asset-status --workspace "F:\UnityKnowledge\YourGame" --json

Asset search accepts a path substring, exact GUID, or asset ID. Asset inspection accepts an exact path, GUID, or ID and prints the selected asset's objects, incoming/outgoing references, and prefab overrides. Asset users accepts an exact path, GUID, asset ID, serialized object ID, or C# type ID. Its output is direct references, not a recursive dependency closure. Add --json to list commands for machine-readable output.

### Stored evidence

- GUIDs from inventoried metadata, with duplicate and invalid GUID diagnostics.
- Serialized documents identified by file path and local fileID, plus class ID, type name, object name, stripped-object flag, and physical line ranges.
- Serialized fields and values, preserving scalar text instead of guessing C# types.
- Local GameObject/component and transform links; cross-file asset references; script references; source prefab, prefab instance, and corresponding-source relationships.
- Script-to-class candidates from the current code map. A matching explicit serialized class identifier is distinguished from a filename-based inference. Missing or ambiguous mappings are left unresolved.
- ScriptableObject candidates when the associated mapped type has that ancestry.
- Raw prefab modification records and a deliberately limited resolved subset.

Binary assets are represented through inventory and metadata, not decoded. GUID resolution identifies an asset even when its imported sub-object cannot be verified; this is reported as resolved_asset_only rather than resolved_object. Built-in assets, unavailable packages, dangling GUIDs, missing fileIDs, and duplicate GUIDs remain explicit unresolved cases.

### Prefab override coverage

The initial resolver supports direct scalar property overrides on objects in a plain source prefab. It records the serialized base value, override value, instance identity, and both source locations. It does not merge these into source data or claim to calculate runtime values.

Nested source prefab chains, stripped-source targets, dotted/array property paths, object-reference overrides, additions/removals, and duplicate overrides are not resolved into effective values. Their original records are retained. Prefab variants that directly override scalar properties on a plain source prefab are within the supported subset. A full expanded prefab hierarchy remains future work.

### Freshness, parsing, and limits

Asset mapping checks the hashes of the serialized assets, metadata, and mapped C# files it reads. Rescan after changing source. A new discovery snapshot invalidates saved asset lookups. Rebuilding the C# map with different evidence also invalidates its asset links; rerun map-assets afterward. If a saved code map is stale, refresh map-code before map-assets. Commands operate on saved snapshots and do not continuously watch files.

The parser uses PyYAML's node representation without constructing arbitrary objects. It rejects aliases, duplicate mapping keys, duplicate document IDs, and excessive YAML nesting. Malformed files receive diagnostics while other assets continue mapping. No source files are modified.

Tests cover synthetic scenes, nested prefab references, direct variants, stripped objects, duplicate names/GUIDs, malformed data, missing references, binary metadata, source preservation, stale snapshots, and actual Roslyn-to-asset connections. Validation against a real Unity repository and large-project performance measurements remain outstanding.

Unity serialization reference:
https://docs.unity.com/en-us/engine/6000.0/manual/working-with-scenes/text-scene-format/yaml-prefab-serialization

## Stage 4: retrieval over code and asset maps

This is the preferred retrieval workflow. The original index/search commands remain available as the separate raw-text prototype.

    python -m unity_rag build-retrieval --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag retrieve "attack cooldown" --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag retrieve "movement speed" --kind asset_object --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag retrieve "CanAttack" --mode exact --expand 2 --workspace "F:\UnityKnowledge\YourGame" --json
    python -m unity_rag retrieve "Assets/Scripts/Enemy.cs" --mode used-by --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag retrieve "Game.Enemy" --mode inherits --workspace "F:\UnityKnowledge\YourGame"
    python -m unity_rag retrieval-status --workspace "F:\UnityKnowledge\YourGame"

Prerequisites are a current discovery scan, C# map, and asset map linked to that C# map. After changes, run scan, map-code, map-assets, then build-retrieval. Index replacement is transactional. Lookup rejects stale map fingerprints rather than silently mixing snapshots. It does not detect live source edits until you scan; building retrieval checks hashes of supported source files.

### Search and structured modes

- search: exact aliases first, followed by keyword matches ranked using SQLite FTS5 BM25. Lower scores rank higher; scores are not confidence probabilities.
- exact: IDs, names, qualified symbols, asset paths, GUIDs, and recorded names without keyword fallback. Overloads and ambiguous names retain all matching identities.
- references: outgoing mapped relationships.
- used-by: incoming relationships. Exact asset paths/GUIDs prioritize the asset identity over declarations in that file.
- inherits: outgoing base-type/interface relationships.
- derived-types: incoming base-type/interface relationships.

Structured modes use exact aliases/identities and do not interpret natural-language questions. Relations retain their resolution status, including unresolved or inferred links. Results are direct relationships rather than transitive dependency closures. Unknown external symbol IDs can still match recorded edges.

Keyword matching splits PascalCase, camelCase, and snake_case while preserving full identifiers. It uses OR matching after removing a small fixed set of common question words. The query is quoted as literal search terms; raw FTS operators are not accepted. Keyword candidates are capped at max(100, 10*limit). Candidate counts describe that shortlist, not a complete hit count. Exact aliases are not subject to the keyword candidate cap.

Use --limit (1–100) and optionally --kind for code_type, code_member, asset, asset_object, prefab_override, or documentation. Kind filters apply only to search/exact modes.

### Evidence and context

- Method/member chunks preserve original source text and line references. Large members split into 120-line windows with 20-line overlap.
- Type records summarize signatures and members as parsed facts, with citations for partial declarations.
- Asset/object records include names, serialized settings, references, and resolution status. Large object records are grouped into batches of 50 entries.
- Supported prefab overrides remain derived interpretations with both base and override citations.
- Documentation is indexed as original source.
- Results carry project identity, indexed Git state, map fingerprints, content hashes, evidence type, and sources. Metadata-only assets may have path-only citations rather than fabricated line numbers.

Use --expand (0–20) to attach a bounded number of containing or directly related records. Expansion is separate from ranked results and preserves the relationship that justified it. It does not merge inferred facts into source evidence. Text records can still be large; token budgeting and semantic reranking are Stage 5 work.

The implementation currently loads map reports to validate fingerprints and supports small/medium development fixtures. Large-repository performance has not been benchmarked. Retrieval produces evidence, not generated answers.

### Reproducible development baseline

The synthetic project is in tests/fixtures/retrieval_project; the labeled questions are in tests/evaluation/retrieval_v1.json. Build its maps in a workspace outside that fixture directory, then run:

    python -m unity_rag evaluate-retrieval tests/evaluation/retrieval_v1.json --workspace "<fixture workspace>" --limit 5

The saved baseline is tests/evaluation/baseline_v1.json. It measures unique relevant entity IDs within the top five evidence records, excluding context expansion. There are eight questions covering keyword code search, identifier lookup, GUID lookup, serialized settings, documentation, script users, and inheritance. Each currently labels one relevant entity, so perfect precision@5 is 0.2 under the fixed-denominator metric even when fewer than five results are returned.

This small development set verifies expected behavior; it is not an independent quality estimate. Add representative real-project questions and held-out judgments before drawing broader conclusions.

SQLite ranking reference: https://www.sqlite.org/fts5.html
