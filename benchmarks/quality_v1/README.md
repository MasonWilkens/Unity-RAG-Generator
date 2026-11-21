# Public Unity evidence baseline v1

This is a 60-question, **agent-reviewed candidate** corpus, selected before its
first retrieval evaluation. It measures literal source evidence in retrieval and
the final extractive answer context. It does not establish generated-answer
correctness or independently reviewed production quality.

| Repository | Pinned revision | Unity version | Split | Questions |
| --- | --- | --- | --- | ---: |
| [Path Creator](https://github.com/SebLague/Path-Creator) | `619c4c39d8a5a5d85914cc4c315121c288c32327` | 2021.3.2f1 | development | 20 |
| [Unity Playground](https://github.com/Unity-Technologies/UnityPlayground) | `1784663d1790de7015b3adaa29e57c3276a8f19d` | 6000.0.66f2 | development | 20 |
| [Unity Bubble Game](https://github.com/superzhan/UnityBubbleGame) | `6232673b440396cab240a49c8b9024d81dac9ff8` | 5.2.3p2 | held-out | 20 |

Versions are declared by the pinned projects, not tested Editor compatibility.
The mapper never launches Unity or builds project code. Source code excerpts in
the labels are covered by the adjacent upstream MIT license notices. Project
copies and indexes are local, ignored artifacts; they are not vendored here.
No questions target bundled third-party plugin implementations.

## Recorded baseline

See the [failure report](baseline.md) and [machine-readable results](baseline.json).
All essential passages reached final context for 44/52 answerable questions:
15/18 Path Creator, 14/18 Playground, and 15/16 held-out Bubble Game. Empty-evidence
controls passed 4/8. The held-out essential-coverage rate exceeds 90%, but the
held-out empty-evidence failures keep the retrieval gate failed. No cases errored
or were removed from denominators. Human review and generated-answer support
remain unmeasured. No ranker or label tuning was performed after observing scores.

The first [development candidate](candidates/literal-assets-v1.md) adds literal
YAML passages and exact lookup intent. It improves complete evidence to 48/52
and empty-evidence controls to 6/8, with six improved cases and no regressions.
Held-out coverage stays 15/16; the acceptance gate remains failed. The original
baseline above and all frozen labels/settings remain unchanged.

The [multipart code candidate](candidates/multipart-code-v1.md) improves complete
evidence from 48/52 to 51/52, including all 11 multipart questions. All 40
development cases pass; held-out outcomes and empty-evidence controls are
unchanged. Three cases improve with no regressions from the previous candidate.
This query-only change reuses its indexes; it requires no additional refresh.
The acceptance gate remains failed and generated answers remain unevaluated.

## Scope and selection

There are 52 answerable passage questions, six deliberately absent-symbol controls,
and two unsupported-asset questions. Unity Bubble Game stores its selected prefabs
in binary format; those two questions therefore expect no extractive evidence
answer about exact serialized fields. Returning vaguely related code is recorded
as a failed empty-evidence outcome, not as a proven incorrect generated claim.

Questions cover behavior, timing/cooldowns, multiple requested parts, cross-file
evidence, relationships, physics, lifecycle, persistence and serialized fields.
Quotes and physical ranges were selected by source inspection and checked for
unique occurrence and SHA-256 agreement **before querying retrieval**. The corpus
is a small convenience sample, not random sampling or an independent human review.
Empty-evidence tests do not replace a reviewed explanation-abstention benchmark.

`manifest.json` freezes a search-only baseline: five results, no relationship
expansion, 24,000 context characters, no embedding or generation calls. Thresholds
are 90% complete essential evidence and a separate, currently unmeasured 95%
supported-claims target. Independent-corpus prerequisites also require human label
review, at least three projects, 60 questions, and 20 held-out questions.

An essential group may contain alternative passages; every group must be present
to pass a question. A serialized YAML quote missing from indexed JSON facts is
reported as an index-representation miss. That is not proof the parsed field value
or the script relationship is wrong. Field/fact-specific mapping evaluation is a
future extension; this suite specifically tests literal evidence delivery.

## Reproduce

From the Unity RAG repository root, install the tool and its documented .NET SDK
prerequisite. The following PowerShell commands create independent public copies.
Use new directories; do not discard changes in existing copies.

```powershell
$env:GIT_LFS_SKIP_SMUDGE = '1'
git -c core.autocrlf=false clone https://github.com/SebLague/Path-Creator.git .unity-rag/quality-corpus/Path-Creator
git -C .unity-rag/quality-corpus/Path-Creator -c core.autocrlf=false checkout --detach 619c4c39d8a5a5d85914cc4c315121c288c32327
git -c core.autocrlf=false clone https://github.com/Unity-Technologies/UnityPlayground.git .unity-rag/quality-corpus/UnityPlayground
git -C .unity-rag/quality-corpus/UnityPlayground -c core.autocrlf=false checkout --detach 1784663d1790de7015b3adaa29e57c3276a8f19d
git -c core.autocrlf=false clone https://github.com/superzhan/UnityBubbleGame.git .unity-rag/quality-corpus/UnityBubbleGame
git -C .unity-rag/quality-corpus/UnityBubbleGame -c core.autocrlf=false checkout --detach 6232673b440396cab240a49c8b9024d81dac9ff8

unity-rag refresh --project .unity-rag/quality-corpus/Path-Creator --workspace .unity-rag/quality-indexes/Path-Creator --no-semantic
unity-rag refresh --project .unity-rag/quality-corpus/UnityPlayground --workspace .unity-rag/quality-indexes/UnityPlayground --no-semantic
unity-rag refresh --project .unity-rag/quality-corpus/UnityBubbleGame --workspace .unity-rag/quality-indexes/UnityBubbleGame --no-semantic

New-Item -ItemType Directory -Force .unity-rag/quality-v1
unity-rag freeze-quality-benchmark benchmarks/quality_v1/manifest.json --output .unity-rag/quality-v1/frozen.json
unity-rag evaluate-quality .unity-rag/quality-v1/frozen.json --output .unity-rag/quality-v1/baseline.json
```

The selected revisions must remain clean, and source bytes must match the reviewed
hashes. Git line-ending conversion must stay disabled for these copies. If a
supported source file is an LFS pointer, source validation will reject its labels
until the exact source is available. No models or API credentials are needed.

A measured failure returns exit code 1 **after saving both reports**. Keep that
result. Do not adjust labels, budgets or thresholds to improve the same baseline.
Source changes or relabeling require a new corpus version and a documented reason.

The run-specific lock includes local generation paths and exact database hashes;
it belongs in the ignored workspace. Rebuilding from identical sources can change
timestamps and database hashes. Compare source identities, labels and settings,
and retain each candidate's own implementation/index hashes. The portable baseline
report in this directory omits the checkout-specific index snapshot's absolute
project path; its original full-report hash is retained for traceability.

## Future comparisons

For a rebuilt candidate, prepare a JSON object mapping all three project names to
the new workspace paths, then run `evaluate-quality` with `--workspaces` and a new
output filename. Ranker-only changes may use the original pinned indexes directly.
Questions and source snapshots must remain identical.

Develop fixes on Path Creator and Unity Playground. Using Unity Bubble Game's
individual failures to guide retrieval changes retires it as an independent
holdout; recruit and freeze a replacement before claiming generalization. Merely
marking the same dataset held out again does not restore independence.
