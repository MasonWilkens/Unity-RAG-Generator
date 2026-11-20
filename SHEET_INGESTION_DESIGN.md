# Proposed opt-in live-sheet ingestion

Status: design only. The RAG builder does not currently fetch live Sheets, and this document does not authorize connecting, exporting, uploading or changing any sheet.

## Scope and consent

A future adapter should require an explicit read-only source and a named spreadsheet and tab allowlist. Show the exact destination, fields and intended retention to the user before the first fetch. Prefer existing authorized connectors or APIs; never collect, print, copy or index credentials. Keep local retrieval and all remote model calls separately opt-in. Do not upload sheet content to a model provider without specific authorization.

## Snapshot and evidence

Store a bounded, versioned snapshot outside the Unity project, with tab name, selected range, header mapping, fetch time, source identity, content hash and a stable snapshot ID. Record whether values or formulas were fetched and whether rows are partial. Cite the snapshot and row/column coordinates, never represent them as live values. Preserve previous generations and avoid exposing sensitive cells; configurable excluded columns and mandatory credential-path exclusions apply before storage or transfer.

## Freshness and failure behavior

A snapshot is an observation at fetch time, not proof of the current sheet. Report `current` only when a supported trustworthy version check confirms it; otherwise use `unknown`, `stale` or `unavailable`. Distinguish generated Unity assets from their source rows and never silently rebind citations. On partial responses, access failures, changed schemas, ambiguous identifiers or uncertain outcomes, fail closed and retain the last complete generation. Sheet writes and Unity imports remain separate authoring actions, not operations performed by indexing.

## Validation before activation

Use synthetic data and mock providers for read-only allowlisting, headers, row identity, version changes, partial fetches, secret exclusion, atomic publication and stale-citation tests. Compare retrieval against source-reviewed development and held-out questions, including a second real project only if the user identifies and authorizes one. Agree on a safe freshness policy and access scope before any real sheet connection; no background watcher is enabled by this design.
