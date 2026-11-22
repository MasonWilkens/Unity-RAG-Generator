# Claim-level answer acceptance: first batch

Baseline: merged PR #7, commit `2931b7a7958c96eebd34f7ca64f97190b2218924`.
The existing case-level answer review cannot measure the production plan's 95%
supported-claim target. This batch adds a separate, offline review workflow and
keeps the legacy review commands compatible.

Freeze the review policy before implementation: supported claims / all final
claims must be at least 0.95, with at least one claim. Unsupported, uncertain and
pending claims stay in the denominator. Every case must also pass correctness,
completeness and abstention review, plus non-overridable evaluation contract and
expected status/runtime checks. A complete review must identify a human who
explicitly declares independence. Agent judgments remain distinguishable and
cannot satisfy that human-review requirement. Identity and independence are
declarations; the tool cannot authenticate them.

Create unfilled packets bound to the exact report bytes. Present each question,
final claims, evidence, gaps and runtime flags beside editable judgments. Bind
claim indices and hashes and reject changed review context, omitted/duplicated
cases or claims, modified policy and stale reports. Incomplete packets should
produce pending results, not successful acceptance. Failed generation cases and
zero-claim answers remain visible. Rate intervals are descriptive; claims within
one answer are correlated.

Strengthen answer evaluation provenance before any provider call: validate unique
case IDs/questions, capture the complete Python implementation hash, dataset,
provider settings and retrieval snapshot, then recheck them after evaluation.
Revalidate retained evidence against source files. Retain collected outputs and
mark the report invalid if inputs changed; failed cases keep their denominator.
CLI evaluation must return failure for invalid runs or failed output contracts.

This is a reviewed-output gate only. It does not replace the frozen retrieval
benchmark, independent source-label review, fresh acceptance repositories, or the
other production gates. Keep `production_gate_passed` false in the standalone
acceptance result. No new model calls, source-label edits or retrieval tuning are
needed to implement or verify this batch.

Verify threshold boundaries, pending/negative/agent reviews, altered claims or
context, duplicate keys/IDs, changed snapshots/sources/datasets/implementation,
failed cases, all-abstention reports, CLI exit codes and non-overwriting UTF-8
outputs. Use an explicitly scripted provider for an end-to-end fixture example;
leave its review unfilled and do not present it as model quality or human review.
Run the full suite and clean-checkout Windows/Linux CI before marking the new PR
ready. Actual independent reviews and a fresh generated-answer sample follow
after this tooling is available.
