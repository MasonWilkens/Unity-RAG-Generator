# Release preparation

The project is an experimental local CLI. `0.1.0` is the current unreleased
package version; preparing artifacts does not publish a release. The version is
defined once in `unity_rag/__init__.py` and exposed by `unity-rag --version`.

## Prerequisites and tested environments

- Python 3.10 or newer, with SQLite FTS5, pip and venv.
- A .NET SDK, not just the runtime. The analyzer currently accepts SDK 6+ and
  uses its bundled Roslyn assemblies without fetching NuGet packages. New CI
  lanes target SDK 8 and 10. Local SDK 6 compatibility is a legacy check, not a
  recommendation to install an old SDK.
- Git for the complete integration suite. No Unity Editor, Ollama, OpenAI key
  or private Unity repository is needed for these checks.
- Network access for package installation and the tokenizer vocabulary used by
  token-budget tests. Model inference and model downloads are not part of CI.

Configured CI lanes: Windows 2022/Python 3.10/.NET 8; Ubuntu 22.04/Python 3.12/.NET 8;
Windows 2022/Python 3.12/.NET 10; Ubuntu 24.04/Python 3.13/.NET 10. These are intended
coverage, not claims of passing remote runs. See
[public validation records](tests/evaluation/README.md) for observed results.
macOS and other interpreter/SDK combinations remain unverified.

## Run checks and build

From a checkout, create an isolated tool environment. Windows PowerShell:

```powershell
python -m venv .unity-rag/release-tools
.unity-rag/release-tools/Scripts/python -m pip install -r requirements/release.txt -r requirements/test.txt
.unity-rag/release-tools/Scripts/python scripts/run_checks.py --jobs 4
.unity-rag/release-tools/Scripts/python -m build --outdir dist
.unity-rag/release-tools/Scripts/python -m twine check --strict dist/*
.unity-rag/release-tools/Scripts/python scripts/verify_distribution.py
```

On Linux, use `.unity-rag/release-tools/bin/python` for the environment's Python.
Use a fresh output directory for each build (`--outdir dist/candidate-2` and
`--dist-dir dist/candidate-2`) so stale versions cannot be mistaken for the candidate.
The default `python -m build` builds the wheel from the newly produced source
archive. It uses an isolated environment and the pinned setuptools backend.

`run_checks.py` runs every `tests/test_*.py` module in bounded worker processes.
Missing prerequisites, any skipped tests, failures, or source changes during the
run fail the check. Reports include exact runtime/dependency versions, test counts,
source fingerprint and individual logs. Do not edit implementation/tests mid-run.

`verify_distribution.py` audits archive contents and source hashes, then installs
each artifact in a separate temporary venv outside the checkout. It clears Python
import overrides and common API credentials, uses isolated Python invocations,
checks the installed module path, and runs both CLI entry points. The Unity fixture
and workspace paths contain spaces. Checks cover C# and GUID lookup, asset/script
links, literal source/hash citations, no generation calls, unchanged refreshes,
changed-source refreshes and preservation of the old generation. It verifies that
indexing changes neither source project files nor installed analyzer sources.

Optional dependency packaging can also be checked:

```powershell
.unity-rag/release-tools/Scripts/python scripts/verify_distribution.py --extras semantic,tokens --report .unity-rag/release-install-extras.json
```

This installs/imports FastEmbed and tiktoken without fetching an embedding model.
It does not establish embedding quality or model-cache availability. The full test
suite exercises semantic contracts using deterministic fixture providers.

## Artifact contents and dependency policy

The wheel contains Python runtime modules, three Roslyn source/config files and
distribution metadata/license. The source archive adds selected documentation,
the install verifier and its synthetic fixture. The complete test suite and
historical evaluation reports remain in the Git repository, not the source archive.
Credentials, local indexes, pilot data, compiled analyzer outputs and caches must
never appear in either artifact. An unexpected archive path fails verification.

Runtime dependencies have compatible ranges in `pyproject.toml`; direct test/build
tools are pinned in `requirements/`. This is not a full transitive dependency lock
or a claim of byte-identical builds. Installation reports record all resolved
dependencies and artifact SHA-256 hashes so the tested environment is inspectable.
Treat updates to pins, Python/.NET lanes and packaging as changes requiring the
same checks. No automated publishing or repository write token is configured.

## Before publishing a beta

1. Review and commit the intended implementation, tests, fixtures, workflow and
   release documents. This working tree contains substantial earlier untracked
   development; a successful local build does not prove those files exist in Git.
   Do not bulk-add credentials, caches, private source or historical report dumps.
2. Run the workflow on that exact commit. A fresh Git checkout is the authority
   for missing tracked files. Require every lane and clean-install check to pass.
3. Run the independent quality, durability, performance and pilot gates in
   [the production plan](PRODUCTION_READINESS_PLAN.md); preserve failed results.
4. Resolve the hosted provider's pricing/configuration validity if offering that
   optional feature. Generated explanations still require source review.
5. Finalize version/release notes, rebuild/retest, and record commit, artifact hashes
   and CI report links. Publish only those reviewed artifacts through an explicit
   release action. Package-name availability and publishing credentials are not
   configured by this batch.

The workflow follows the [GitHub Python CI guide](https://docs.github.com/en/actions/tutorials/build-and-test-code/python).
Archive selection follows [setuptools distribution guidance](https://setuptools.pypa.io/en/latest/userguide/miscellaneous.html).
