# Workspace creation improvements

Preserve source exclusions, provenance checks, immutable generations and explicit
provider consent while making the tool easier to apply to a new Unity project.

1. **Explain coverage.** Report inventory status separately from indexed evidence,
   supported relationships and current source hashes. Distinguish excluded,
   unsupported, missing, not-yet-built and stale content. A saved coverage report
   is not a full freshness scan or a view of live external data.
2. **Measure useful questions.** Build source-reviewed question sets for common
   code, prefab, scene and authoring tasks. Label decisive passages and required
   facts, retain negative controls, and hold out an independent repository.
3. **Design explicit external-data ingestion.** Specify authorized sources, snapshot
   identities, freshness, field selection, sensitive-data exclusion and bounded
   storage. External spreadsheets are not currently fetched automatically. See
   [sheet ingestion design](SHEET_INGESTION_DESIGN.md).
4. **Improve prerequisites and onboarding.** Explain Python, SQLite FTS5, .NET,
   optional model dependencies, storage needs and recoverable errors. Verify the
   packaged tool outside the development checkout.
5. **Represent compilation context.** Record supplied defines/references and
   distinguish Editor/player/platform configurations. An optional Editor-exported
   profile may improve mapping where repository data is insufficient.
6. **Preserve evidence through retrieval.** Measure final packed passages, not just
   matching paths. Keep source ranges and hashes, derived/inferred labels and
   missing-evidence status visible to downstream answer providers.
7. **Validate maintenance and upgrades.** Keep the last usable generation after
   failure, detect source/configuration drift, and document rebuild and retention
   policy. Test supported incremental paths against full rebuilds.

Current implementation includes coverage inspection, preflight, versioned evidence,
source checks, bounded context packing and managed refresh. Complete independent
quality, Unity compatibility and operating-limit evidence remains open under the
[production-readiness plan](PRODUCTION_READINESS_PLAN.md).
