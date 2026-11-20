# Production readiness

Recorded 2026-11-20. Target: a dependable local beta that can create and maintain
a useful RAG workspace for another developer's Unity project. A hosted service,
Unity Editor integration and automatically trusted generated answers are outside
this release target. Optional hosted generation remains opt-in.

## 1. Release preparation and reproducible installation - baseline complete

Verified on commit `1bb9ce5e944b39ebfcc89cd13920b4dd9f677887`: 529 tests with no
skips; all four Windows/Linux CI configurations; wheel/source package checks and
fresh installations; optional dependency installation checks. Both the
[push run](https://github.com/MasonWilkens/Unity-RAG-Generator/actions/runs/44805804482)
and [PR run](https://github.com/MasonWilkens/Unity-RAG-Generator/actions/runs/44805817698)
passed. Later changes must pass the checks again.

- Package Python modules, runtime metadata and all three Roslyn helper sources.
- Build both a wheel and source distribution; check metadata and archive contents.
- Install each artifact into its own fresh environment outside the checkout.
  Map a synthetic Unity project, retrieve code and asset relationships, verify
  literal cited excerpts, and test unchanged and changed-source refreshes.
- Automate unit/integration tests, artifact checks and install checks on Windows
  and Linux. Record exact Python, .NET and resolved dependency versions.
- Capture intended implementation and tests in Git before releasing. Local
  untracked source files are not a reproducible public baseline.
- Keep release uploads manual until the release gates pass. See [RELEASE.md](RELEASE.md).

Exit gate: a clean clone builds and installs without this development machine's
files; CI succeeds on the intended commit; the installed tool completes the
fixture workflow. A local artifact pass alone does not close this gate.

## 2. Independent quality evidence - active

The next batch adds a pre-evaluation freeze format, multi-project evidence runner,
candidate-index comparisons, and failure reports. The public candidate corpus in
`benchmarks/quality_v1` contains 60 source-inspected questions across three pinned
Unity repositories, with one repository held out. Labels are explicitly
agent-reviewed. This completes evaluation infrastructure, not the independent
human-review or generated-answer acceptance gates. See that directory for baseline
results and reproduction instructions.

Use at least three unfamiliar Unity repositories and approximately 60
source-reviewed questions. Hold an entire repository out of development tuning.
Freeze sources, labels, essential passages, question categories, answer budgets
and acceptance rules before tuning. Evaluate retrieval, final packed evidence,
unsupported claims and appropriate abstention separately; retain failures.

Proposed beta targets, to freeze before evaluation: at least 90% of answerable
held-out questions receive every essential labeled passage within the final
evidence budget, and at least 95% supported claims in a separately reviewed answer
sample. Publish per-project/category counts, denominators and uncertainty.
These are targets, not achieved results. The small existing development benchmark
and synthetic fixtures do not establish generalization or independent review.

## 3. Unity compatibility

Publish a tested Unity/C#/asset compatibility matrix. Improve assembly references,
asmdef configuration, conditional defines and Editor/player distinctions.
Prioritize prefab variants and overrides using observed benchmark failures.
Represent unsupported or ambiguous relationships as unresolved.

Exit gate: supported relationships agree with reviewed Unity source and serialized
data across the matrix; incomplete analysis remains visible to the user.

## 4. Index durability and upgrades

Exercise large repositories, low disk space, damaged indexes, concurrent readers
and writers, process termination and upgrades. Define index version compatibility,
migration/rebuild policy and retention. Preserve the last usable generation when
a refresh fails and document recovery procedures.

Exit gate: fault and upgrade tests demonstrate recovery without corrupting or
silently replacing the previous usable snapshot.

## 5. Performance and provider reliability

Measure first build, incremental refresh, peak memory and query latency on small,
medium and large projects with stated hardware and corpus sizes. Set performance
budgets from those measurements. Improve map memory use and linear vector search
when justified by evidence. Refine chunk boundaries around statements and align
semantic chunk limits with the embedding model's actual tokenizer.

Harden provider configuration, limits, timeouts and error reporting. The current
OpenAI pricing table has a validity deadline of 2026-11-20; verify current prices
and update the policy before advertising hosted generation as ready. Never extend
its date without verification. Local release tests must not require API keys.

Exit gate: documented operating limits, reproducible performance results and
tested provider failure behavior; no expired configuration advertised as ready.

## 6. Independent pilot and usable operation

Improve prerequisite checks, cache/storage guidance, progress, coverage reporting
and troubleshooting. Have independent developers install, create, query, refresh
and upgrade workspaces using only the documentation. Resolve recurring failures.

Exit gate: successful independent pilot tasks, reviewed remaining limitations,
and a release checklist attached to the exact tested commit and artifact hashes.

## First batch scope and evidence

This batch implements packaging, release documentation, isolated installation
checks and automated checks. It does not certify semantic answer quality or
complete the independent pilot. Validation results are recorded in
[public evaluation records](tests/evaluation/README.md). CI coverage is only
verified by the exact GitHub runs linked above; future changes need fresh results.
