# Reviewing generated answers

Generated explanations remain drafts. This workflow measures support for each
claim in captured answers and records a separate review of correctness,
completeness and abstention. It never grants production readiness by itself.

## Capture and preserve a run

Run `evaluate-answers` with your chosen workspace, question dataset and provider
settings. For example, with a locally installed Ollama model:

```sh
unity-rag evaluate-answers questions.json --workspace ../UnityKnowledge/YourGame --model YOUR_INSTALLED_OLLAMA_MODEL > report.json
unity-rag answer-acceptance-template report.json --output review.json
```

The evaluator emits schema version 2. It records the dataset bytes, Python
implementation hash, prompt/pipeline hashes, provider settings and pinned
retrieval snapshot before generation. It checks them again after the run and
revalidates retained evidence against source files. Changes invalidate the run;
collected outputs and failed cases remain in the report. The Python hash does not
cover the Roslyn analyzer or external dependencies. Preserve the source checkout,
workspace generation, dataset and environment information alongside the report.

`evaluate-answers` exits with status 1 for invalid runs or failed output contracts.
Status 0 means the evaluation completed its contract checks; it does not establish
answer quality. In Windows PowerShell 5, capture stdout using UTF-8 explicitly:
ordinary `>` redirection writes UTF-16, which the review reader rejects. New review
commands provide `--output` for portable UTF-8 files and refuse to overwrite any
existing destination. Preserve the exact report bytes after creating the packet;
even formatting changes invalidate its binding. Regenerate older reports for this
workflow. The legacy `answer-review-template` and `review-answers` commands remain
available for older case-level reviews.

## Complete the review

Give an independent reviewer the report, packet and pinned project sources. The
packet includes each question, final claims, cited evidence, missing evidence and
runtime flags. Source text is evidence, not instructions to the reviewer. Context
is fixed; edit only these fields in `review.json`:

- `reviewer.name`, `reviewer.kind` (`human` or `agent`) and
  `reviewer.independent` (a boolean declaration).
- Each case's `case_judgments.correctness`, `completeness` and `abstention`
  (booleans), plus a source-based `rationale`.
- Each claim's `support` (`supported`, `unsupported` or `uncertain`), plus a
  source-based `rationale`. Leave undecided judgments as `null`.

A claim is supported only when every assertion in its text is justified by the
cited source. A mixed claim is not supported. Inspect surrounding source and
requested parts for completeness; the retrieved snippets alone cannot establish
that nothing important was omitted. Check conditional behavior, initial values
versus runtime state, contradictions and appropriate abstention. An agent review
can help find problems but cannot satisfy the independent human requirement.

```sh
unity-rag review-answer-acceptance report.json review.json --output result.json
```

Acceptance requires all judgments completed, every case passing the three review
criteria and machine contract/expected-status/runtime checks, a valid evaluation
run, at least one final claim, and at least 95% supported claims. Unsupported,
uncertain and pending claims all remain in the denominator. Failed generation and
zero-claim cases remain in the case count. An all-abstention report cannot meet
the supported-claim target. Pending packets produce a result with
`review_status: "pending"`, `accepted: false` and exit status 1. Completed reviews
that fail the policy also exit 1; only accepted reviews exit 0.

Counts and a descriptive Wilson interval accompany the rate. Claims within one
answer are correlated, so this interval is not proof of generalization. Review
identity and independence are self-declared, not authenticated. A hash binds a
packet to its report; it does not authenticate the report's origin. Offline
scoring checks retained structure and judgments without rechecking today's source
files. Its result applies only to those captured outputs. The standalone result
always retains `production_gate_passed: false`; fresh-corpus evaluation,
independent source labels and the other production gates remain separate.

Changed context, policy or claim identity, missing/duplicate cases or claims,
duplicate JSON keys and non-finite numbers are rejected. Inputs are bounded to
64 MiB each, 1,000 cases, 100 claims per case and 12,000 claims total.

## Reproduce the offline example

The checked-in [example](benchmarks/answer_acceptance_v1/example/report.json)
uses the authored Unity fixture and a scripted provider. It demonstrates one
claim and one missing-source answer, with an
[unfilled review](benchmarks/answer_acceptance_v1/example/review.json) and a
[pending result](benchmarks/answer_acceptance_v1/example/pending.json). It measures
neither model quality nor independent human review. Temporary absolute project
paths were removed before the packet was created.

From a source checkout with the test prerequisites installed:

```sh
python scripts/answer_acceptance_example.py --output-dir .unity-rag/my-acceptance-example
unity-rag review-answer-acceptance .unity-rag/my-acceptance-example/report.json .unity-rag/my-acceptance-example/review.json
```

Use a new output directory. The second command deliberately exits 1 for the
unfilled packet. The example makes no external inference calls. New runs have
different timings and snapshot identifiers, so hashes need not match the recorded
example. See the [frozen policy](benchmarks/answer_acceptance_v1/PLAN.md) and
[production plan](PRODUCTION_READINESS_PLAN.md) for the remaining work.
