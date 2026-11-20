# Literal asset evidence and explicit lookup candidate

Implementation scope selected from the Path Creator and Unity Playground
development cases, before examining this candidate's held-out results:

- Store literal YAML as bounded `asset_source` chunks within parsed object ranges.
  Preserve physical source lines, object IDs, hashes and parent asset IDs.
- Keep YAML in a separate keyword index so it cannot alter the existing code and
  parsed-fact document frequencies. Explicit kind searches use that index.
- For serialized/saved-value queries, pair mapped facts with literal passages
  from the same object. Preserve verified anchor ordering and dependencies.
- Treat complete identifier lookup requests as exact indexed-name requests,
  retaining overloads and returning no fallback for missing names. General
  behavioral questions retain normal retrieval.

The first development attempt exposed a code-search ranking regression from
putting YAML in the shared keyword corpus. The separate index fixes that issue.
Both local attempts are retained in the ignored evaluation workspace. Scene
regression checks also require existing verified anchors to keep their first
result slot, with literal companions following them.

Keep the v1 baseline, labels, source revisions, settings and acceptance thresholds
unchanged. Build candidate workspaces separately and evaluate against the original
frozen lock. Do not use Unity Bubble Game failures to guide this batch. Retain all
candidate failures; human review and generated-answer accuracy remain separate
unmeasured gates. This batch evaluates search mode, not embedding-model quality.

The index pipeline change requires refresh. Older snapshots remain readable;
they do not gain literal evidence without rebuilding. Unrestricted semantic
builds now include extra asset passages, so their storage/inference work grows.
