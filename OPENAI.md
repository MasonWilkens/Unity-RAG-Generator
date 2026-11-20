# Optional OpenAI explanations

Indexing and local retrieval need no API key. Ollama is the default generation
adapter. OpenAI is an opt-in adapter for explanations from selected retrieved
evidence; a request transfers the question and that evidence to the provider.

The implementation requires all of `--allow-remote`, `--api-key-file`,
`--spend-ledger` and `--budget-usd`. Supply your own credential file outside version
control. The tool loads it only for a request and does not save its contents in
configuration, output or the spending ledger. Never commit credentials or indexes.

```sh
unity-rag ask "What sets the attack cooldown?" --workspace ../UnityKnowledge/YourGame --provider openai --api-key-file "<credential-file>" --allow-remote --spend-ledger "<ledger-file>" --budget-usd 1 --max-api-calls 1 --followups 0 --mode search --show-sources
```

The placeholders are intentionally generic. Installation and CI do not provision
credentials, send project evidence, or run this example.

## Current adapter limits

The adapter is restricted to a configured model, official Responses endpoint,
explicitly priced service tiers and structured output. It rejects redirects,
environment proxies, refusals, malformed/incomplete output and unexpected usage.
There are no automatic retries, endpoint overrides or model fallbacks.

The pricing policy in `unity_rag/openai_generation.py` is valid through 2026-11-20.
Requests after that date fail closed until pricing is reverified and the policy
is deliberately updated. This is a code-policy deadline, not information about
any user's credentials, account balance or credit expiration. The current adapter
should not be advertised as ready for future hosted use without that review.

## Spending and evidence controls

Each request reserves a conservative upper bound in a persistent SQLite ledger
before network access. Keep the same ledger for the agreed budget. Reservations
are not automatically refunded after uncertain outcomes; missing usage or an
unexpected response can block further requests. Recorded token use and local
estimates are not a provider billing statement.

Questions and evidence are size-bounded. Explicit token budgets can constrain
the complete prompt/schema/evidence and output reserve, using the optional token
counter. Token estimates do not replace the spending reservation.

Citation validation checks supplied evidence identities and current source hashes.
`--show-sources` displays literal supporting excerpts for inspection. A valid
response or model-assisted review remains a draft requiring source review.

Unit tests use synthetic credentials and mocked transport. Clean-install checks
use extractive answers and make no hosted generation calls.
