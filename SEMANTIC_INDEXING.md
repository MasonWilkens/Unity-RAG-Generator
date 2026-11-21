# Local semantic indexing

Semantic indexing is optional. Base discovery, mapping, exact/keyword retrieval
and extractive answers work without an embedding runtime or API credentials.

```sh
python -m pip install ".[semantic,tokens]"
unity-rag refresh --project ../YourUnityProject --workspace ../UnityKnowledge/YourGame --semantic --semantic-batch-size 4
unity-rag retrieve "How is heat reduced?" --workspace ../UnityKnowledge/YourGame --mode semantic
unity-rag retrieve "How is heat reduced?" --workspace ../UnityKnowledge/YourGame --mode hybrid
```

## Passage inputs and storage

The default adapter uses FastEmbed and a local model cache. The first model load
can download model files. The model/provider configuration is recorded with the
index; changing it requires compatible rebuilding.

Evidence records retain their full source identity and citations. Semantic inputs
add a heading and overlapping text windows (currently up to 1,000 body characters
with an 800-character stride). Window hashes allow retrieval to identify the
winning passage without replacing original source evidence. This character policy
is not the embedding model's actual tokenizer limit; model-aware limits remain a
refinement target.

Newly built indexes include literal serialized-object passages as `asset_source`.
An unrestricted semantic build embeds these alongside parsed asset facts, which
increases its passage count. Use explicit `--semantic-kind` selections when a
smaller corpus is intended; saved kind restrictions remain in effect on refresh.
Direct `Locate SomeIdentifier` requests use exact indexed aliases without query
embedding, even when the requested mode is semantic or hybrid.

Normalized vectors must be finite, nonzero and dimensionally consistent. Verified
cached vectors can be reused. Search currently scans stored passage vectors and
aggregates scores per evidence record; this is not an approximate-neighbor server.

## Batching, cancellation and refresh

Builds validate the configured batch size, stage work in bounded batches and save
durable embedding checkpoints. Checkpoint identities include provider/configuration
and passage hashes. Completed checkpoints can survive cancellation and be reused
on a later attempt. Corrupt, incompatible or incomplete inputs must fail validation.

Managed refresh publishes a new generation only after required maps and semantic
work complete and source/configuration checks pass. The previous active snapshot
remains available after failed or interrupted work. Retention is separate from
refresh; old generations are not automatically deleted.

## Measuring behavior

Use fixed snapshots and model/cache settings when comparing semantic and hybrid
ranking. Record batch size, source identities, provider/model versions, phase
timings, memory and cancellation/restart outcomes. The profiling tools expose
their required inputs through `--help`.

CI uses deterministic fixture providers for semantic contracts. The optional
dependency install check imports FastEmbed without downloading an embedding model.
Neither check establishes model quality, model-cache availability or performance
on a large project. See [benchmark guidance](BENCHMARK.md) and
[production readiness](PRODUCTION_READINESS_PLAN.md).
