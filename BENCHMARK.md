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
unity-rag evaluate-passages tests/evaluation/passage-development-v1.json --workspace ../passage-knowledge --mode search --limit 5 --context-chars 1400
```

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
