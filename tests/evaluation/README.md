# Public evaluation inputs

The Git baseline contains authored synthetic controls and release validation
records. They run without private repositories, credentials or a Unity Editor.

- `retrieval_v1.json`: lexical and structured lookup over `fixtures/retrieval_project`.
- `stage6_fixture_acceptance_v1.json`: draft-answer controls over `fixtures/answer_project`.
- `grounding_adversarial_v1.json`: source-grounding challenge inputs.
- `greenhouse-synthetic-*.json`: source lookup and work-queue controls over
  `fixtures/greenhouse_project`.
- `passage-development-v1.json`: decisive-passage controls over `fixtures/passage_project`.
- `fixtures/type-comparison-v1/` (relative to `tests/`): frozen sources, labels and
  hash lock for nine two-type passage cases and twelve fallback controls. Run
  `scripts/evaluate_type_comparisons.py` to reproduce baseline/candidate checks;
  these are development mechanism tests, not independent acceptance.
- `fixtures/dependency-chain-v1/` (relative to `tests/`): frozen sources and labels
  for six two-call passage cases and sixteen fallback controls. Reproduce with
  `scripts/evaluate_dependency_chains.py`; the intermediate call sites are
  essential evidence, and the labels do not assert execution or data flow.
- `fixtures/datasets/authoring-*.json` (relative to `tests/`): disjoint synthetic
  label-contract controls, with hashes and quotes checked against the greenhouse
  source files. These do not constitute a held-out real-project benchmark.
- `release-*.json` and `release-*.md`: dated release-check evidence. Each record
  identifies its exact tested source/artifacts and whether remote CI actually ran.

## Local project experiments

Historical real-project evaluation dumps, private source labels, pilot queues and
project-specific setup guides remain local and are ignored by Git. Earlier
development notes describe those experiments; their report filenames are historical
references, not inputs required by CI or claims of public reproducibility.
Real-project evaluation requires your own workspace, source permissions and labels.

The full suite must not skip a test or need one of those local files to pass.
Passing these synthetic controls validates implementation contracts, not independent
retrieval generalization or factual accuracy of generated explanations.
