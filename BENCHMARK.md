# Benchmarks and evidence budgets

Use fixed source snapshots, versioned labels and consistent settings when comparing
retrieval changes. Source-path hits, decisive passages, packed evidence and factual
answer quality are separate measurements.

## Public synthetic controls

The repository includes authored fixtures under `tests/fixtures` and the public
labels listed in [evaluation inputs](tests/evaluation/README.md). They require no
private project or credentials. Build the retrieval fixture in a separate workspace:

```sh
unity-rag refresh --project tests/fixtures/retrieval_project --workspace ../fixture-knowledge --no-semantic
unity-rag evaluate-retrieval tests/evaluation/retrieval_v1.json --workspace ../fixture-knowledge --limit 5
```

Passage controls use `tests/fixtures/passage_project` and
`tests/evaluation/passage-development-v1.json`. They exercise long methods,
cooling/cooldown formulas, shader/UI/configuration evidence and misleading text.
These are development controls, not independent real-project acceptance.

```sh
unity-rag refresh --project tests/fixtures/passage_project --workspace ../passage-knowledge --no-semantic
unity-rag evaluate-passages --dataset tests/evaluation/passage-development-v1.json --workspace ../passage-knowledge --mode search --limit 5 --context-chars 1400
```

## Frozen multi-project quality runs

`freeze-quality-benchmark` validates and pins inputs **before** retrieval evaluation.
It records repository identities/revisions, source and label hashes, completed
workspace generations, question categories, evidence budgets and acceptance targets.
The existing `freeze-benchmark` command is the older entity-regression tool;
its baseline-first contract is unchanged.

```sh
unity-rag freeze-quality-benchmark benchmarks/quality_v1/manifest.json --output .unity-rag/quality-v1/frozen.json
unity-rag evaluate-quality .unity-rag/quality-v1/frozen.json --output .unity-rag/quality-v1/baseline.json
```

Prepare the project copies and indexes first, following the
[public corpus instructions](benchmarks/quality_v1/README.md). The runner writes
JSON plus a Markdown failure report and refuses to replace earlier reports.
Exit codes: 0 means the held-out retrieval gate passed, 1 means measured failures
or case errors, and 2 means inputs changed during evaluation. Invalid inputs found
before evaluation also fail immediately. A zero exit code is **not** production
approval: generated-answer support and independent human review remain separate.

Use `--workspaces candidates.json` to compare rebuilt indexes against the same
frozen sources, questions and budgets. The file maps every project name to a
workspace path (relative to that file). Candidate sources must match the original
corpus; advancing a workspace during evaluation invalidates the result. Reranking
can use the default pinned generations. Each run records implementation and index
hashes, embedding configuration when used, and runtime/dependency versions.

Reports distinguish missing index representation, retrieval selection, context
packing, labeled negative evidence, empty-evidence outcomes, and evaluation errors.
Errors remain in denominators. Every essential passage group must be represented
for a question to count as complete; alternatives within a group are allowed.
Per-project, split and category counts accompany rates and descriptive 95% Wilson
intervals. Correlated, convenience-sampled questions do not establish population
accuracy. An index-representation miss alone cannot identify whether ingestion,
mapping, transformed asset facts or chunk boundaries caused the omission.

The public 60-question corpus is an agent-reviewed candidate, not an independently
human-approved acceptance suite. Synthetic controls cannot satisfy real-corpus
requirements; a previously tuned project cannot be declared held out. After using
a held-out failure to guide tuning, retire that repository as an independent
holdout and version a new one. Keep the original failed baseline.

## Label and snapshot requirements

Record the exact source hashes, expected entity IDs or paths, essential quoted
passages and their physical line ranges. Add relevant negative controls and
unanswerable questions. Freeze question categories, source snapshots, retrieval
settings and context budgets before comparing changes. Keep failure cases.

The passage evaluator verifies reviewed source hashes and literal quote/range
matches. It reports retrieval separately from the evidence actually packed for
an answer. A filename hit without the required passage is insufficient.
The benchmark helpers retain dataset, index and implementation identities.

Use a held-out repository for independent evaluation. Questions held out from the
same project and synthetic controls do not establish cross-project generalization.
Report counts and denominators per project/category alongside aggregate metrics.

## Evidence and request budgets

Character budgets are available without optional dependencies. Token budgets use
a selected counter; install the `tokens` extra for tiktoken encodings. Source windows
preserve literal text and citation ranges. Oversized or unsupported records are
reported or excluded when they cannot fit.

The full-input budget includes instructions, schema, messages and an output reserve;
it is distinct from the evidence-only budget. Estimates are not exact provider
token usage or proof of answer correctness. Hosted generation requires separate
explicit permission and configuration.

## Repeatable development tools

The scripts in `scripts/` accept explicit frozen manifests, workspaces, model caches
and output destinations. Comparison tools can isolate packing or vector-scanning
changes while keeping inputs fixed. Use their `--help` output for required inputs.
A real-project answer probe requires both `--real-workspace` and `--real-dataset`;
no private dataset is bundled.

Record exact commands, package/model versions, source identities, timings and all
failed outcomes. Publish only source material and reports that you are authorized
to share. The independent production gates are in
[PRODUCTION_READINESS_PLAN.md](PRODUCTION_READINESS_PLAN.md).
