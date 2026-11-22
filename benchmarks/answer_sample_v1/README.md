# Fresh Boids answer sample v1

This is a 12-question, agent-authored candidate on a repository new to this
project's benchmarks. It is one small sample, not the multi-project production
gate or an independent review. See the [pre-generation plan](PLAN.md),
[frozen manifest](manifest.json) and [question/source references](questions.json).

Source: [SebLague/Boids](https://github.com/SebLague/Boids/tree/fdda4db1368df92fbacd8a644bbe6dc61d705441),
revision `fdda4db1368df92fbacd8a644bbe6dc61d705441`, declared Unity 2019.1.3f1.
The [MIT license](Boids-LICENSE.txt) applies to its source excerpts. Mapping does
not launch Unity or establish Editor compatibility. Project copies and indexes
remain ignored local artifacts.

Four questions cover individual code behaviors, three cover multiple code parts,
one covers a cross-file relationship, two compare saved asset values with C#
initializers, one requires runtime observation and one names a missing member.
Reviewer guidance was authored from the pinned source before retrieval/generation.
Guidance is not independent source labeling and may itself need correction during
review. Record any corrections separately; do not rewrite frozen inputs.

## Reproduction

Use a source checkout with the test prerequisites, .NET SDK and Ollama installed.
The sample fixes local `qwen3.5:9b`, its exact installed digest, thinking disabled,
seed 42, 2,048 output tokens and a 120-second request timeout. Retrieval uses five
results, search mode, no graph expansion, 24,000 evidence characters, no followups
and no automated claim review. The missing-member question uses exact lookup.
A seed and digest do not guarantee identical output on different hardware.

```sh
git -c core.autocrlf=false clone https://github.com/SebLague/Boids.git .unity-rag/answer-sample-sources/Boids
git -C .unity-rag/answer-sample-sources/Boids checkout fdda4db1368df92fbacd8a644bbe6dc61d705441
unity-rag refresh --project .unity-rag/answer-sample-sources/Boids --workspace .unity-rag/answer-sample-workspace --no-semantic
python scripts/evaluate_answer_sample.py prepare benchmarks/answer_sample_v1/manifest.json --workspace .unity-rag/answer-sample-workspace --output .unity-rag/my-answer-sample.lock.json
python scripts/evaluate_answer_sample.py run .unity-rag/my-answer-sample.lock.json --output .unity-rag/my-answer-sample-run
```

The required model digest is in the manifest. The runner refuses another digest
or a remote model. An unavailable model does not trigger a download or fallback.
Use new lock/output names. Preparation verifies exact dataset bytes, cited source
lines, a clean repository and completed fresh index; it makes no generation
request. The lock adds the Python and bundled analyzer hashes, exact index files,
snapshot and stable provider settings. Rebuilding indexes or changing code needs
a new lock, even when the portable sample inputs are unchanged.

Running rechecks the lock before generation and after all questions. Failures
remain in `report.json`; changed inputs invalidate the run. The output directory
contains a `started.json` marker even if interrupted. There are no automatic
retries. An interrupted run or a changed setup must be retained and explained
before making a separately named run. A complete capture exits 0; failed output
contracts or invalid inputs exit 1. This exit code is not a quality acceptance.

The runner removes only temporary absolute `answer.project` fields before writing
the report and binding `review.json` to its exact bytes. It creates `pending.json`
with no reviewer assigned. Inspect the pinned sources using the
[acceptance guide](../../ANSWER_ACCEPTANCE.md), fill judgments in a separately
saved review, and score it with `review-answer-acceptance`. Reviewer identity and
independence are declarations, not authentication. Frozen locks are integrity
records, not signed proof of origin or independent review.

## Results

The [first report](run-1/report.json) retains all 12 outcomes: 11 passed output and
citation contracts, while `initial-velocity` failed with a network/timeout error
after 120.14 seconds. It was not retried. There were no input-integrity validation
errors. The runtime-observation question returned `insufficient_evidence` with
the required runtime flag, and the missing member returned `insufficient_evidence`.
Those are contract/status observations, not independent judgments of correctness.

There are 48 retained claims, zero reviewed claims, and an
[unfilled review packet](run-1/review.json). The [acceptance result](run-1/pending.json)
is pending and false. Even fully supportive judgments could not override the
retained failed generation. The recorded zero supported claims means none have
been judged; it is not a measured zero-percent factual-accuracy rate.

[Run provenance](run-1/provenance.json) retains the lock binding with local
manifest/workspace paths omitted. The exact local lock stays in the ignored
workspace; its original hash is retained. Timings were captured while development
checks were also running and are not a controlled performance benchmark. No
hosted inference was used.

Retrieval, prompts, model settings and labels were not tuned against these
outputs. Actual independent source and answer review remain pending. Next, review
the captured claims and investigate the initial request timeout separately using
development controls; do not replace this run with a warmed or retried result.
