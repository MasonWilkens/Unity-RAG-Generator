# Fresh answer sample v1

Base: PR #8, commit `70773c1c3ac55fc79fdf9045c9a086dd3c65c7e6`.
This follow-up is stacked on the unmerged review workflow.

Prepare a first, small fresh-project answer sample using SebLague/Boids at
`fdda4db1368df92fbacd8a644bbe6dc61d705441` (MIT). This repository has not been
used by this project's earlier retrieval or answer benchmarks. It is one project,
not the three-project production corpus or evidence of generalization.

Before generation, freeze 12 questions, source-based reviewer guidance, exact
dataset bytes, repository revision, answer budgets, model identity and the existing
95% claim-support policy. Include code behavior, multipart/cross-file behavior,
saved asset values, runtime limits and missing-source controls. Labels are
agent-authored and must not be described as independent human review.

Use the installed local qwen3.5:9b model, thinking disabled, seed 42, no follow-up
queries or automated claim review, search retrieval with five results and zero
graph expansion, 24,000 evidence characters and 2,048 output tokens. Freeze the
installed model digest too. Run once after freezing; retain every failure and do
not tune prompts, retrieval, labels or settings against these outputs. A later
retry or changed setup needs a separately named run with its reason recorded.

Add an offline preparation/run workflow that refuses changed manifests, datasets,
project sources, index generations, implementation, policy, provider settings or
model identity before generation. Recheck after generation and retain invalid
outputs. Never overwrite a lock, run directory or review. Record run provenance,
create an unfilled acceptance packet and keep production acceptance false.

Fix a prerequisite in evaluation: OpenAI's usage counters and spending telemetry
change normally after a request. Compare stable provider settings separately,
retain before/after telemetry, and still invalidate actual model, budget, timeout
or other configuration changes. Verify this with synthetic credentials and mocked
transport; this batch makes no hosted requests or pricing-policy changes.

Test freeze/run mutation failures, model changes, output preservation, CLI exits,
real provider telemetry and the unfilled review path. Run the full suite and
clean-install CI. Publish the frozen inputs, source license, captured outputs and
pending review with temporary checkout paths removed before hashing the review.
Independent source and answer review remain unassigned; do not fill those in on
someone else's behalf. No production-quality pass is claimed by this batch.
