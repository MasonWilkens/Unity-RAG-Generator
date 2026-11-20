"""Reproducible entity-level retrieval evaluation; no model judges."""
from .workspace import resolve_workspace, cache_workspace
import json
import hashlib
import time
from pathlib import Path
from .retrieval import retrieve


def evaluate(workspace, dataset, limit=5, mode="search", provider=None, expand=0, semantic_weight=0.8):
    workspace = resolve_workspace(workspace)
    dataset_bytes = Path(dataset).read_bytes()
    data = json.loads(dataset_bytes.decode("utf-8-sig"))
    if data.get("schema_version") != 1 or not data.get("questions"):
        raise ValueError("Evaluation requires schema_version 1 and a nonempty questions list.")
    if mode in ("semantic", "hybrid") and provider is None:
        from .semantic import semantic_status
        from .embeddings import FastEmbedProvider
        config = semantic_status(workspace)["config"]
        provider = FastEmbedProvider(config["model"], cache_workspace(workspace)/"models")
    scores = []
    snapshot = None
    for item in data["questions"]:
        relevant = set(item["relevant_entities"])
        if not relevant:
            raise ValueError("Each evaluation question must label at least one relevant entity.")
        started = time.perf_counter()
        result = retrieve(workspace, item["query"], mode=item.get("mode", mode),
                          limit=limit, kind=item.get("kind"), provider=provider, expand=expand,
                          semantic_weight=semantic_weight)
        current_snapshot = result.get('snapshot')
        if snapshot is not None and current_snapshot != snapshot:
            raise ValueError('Retrieval snapshot changed during evaluation. Retry.')
        snapshot = current_snapshot
        elapsed = time.perf_counter() - started
        expected = item.get("expected_relationship", {})
        ranked = []
        for row in result["results"]:
            if any(row.get(k) != v for k, v in expected.items()):
                continue
            entity = row.get("entity_id") or row.get("source")
            if entity not in ranked:
                ranked.append(entity)
        hits = relevant.intersection(ranked)
        reciprocal = next((1 / (i + 1) for i, entity in enumerate(ranked) if entity in relevant), 0)
        context_entities = {r.get("entity_id") or r.get("source") for r in result["context"]
                            if all(r.get(k) == v for k, v in expected.items())}
        scores.append({"category": item.get("category", "code"), "seconds": elapsed,
                       "context_recall_at_k": len(relevant.intersection(set(ranked) | context_entities))/len(relevant),
                       "id": item["id"], "query": item["query"], "retrieved_entities": ranked,
                       "relevant_entities": sorted(relevant), "recall_at_k": len(hits) / len(relevant),
                       "precision_at_k": len(hits) / limit, "reciprocal_rank": reciprocal})
    return {"snapshot": snapshot, "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
            "split": data.get("split", "development"), "expand": expand,
            "semantic_weight": semantic_weight, "retrieval_mode": mode, "dataset_version": data.get("version"), "k": limit, "questions": len(scores),
            "recall_at_k": sum(s["recall_at_k"] for s in scores) / len(scores),
            "precision_at_k": sum(s["precision_at_k"] for s in scores) / len(scores),
            "mean_reciprocal_rank": sum(s["reciprocal_rank"] for s in scores) / len(scores),
            "evaluation_unit": "Unique entity IDs in the top-k evidence records; context expansion excluded. Structured queries label source entities.",
            "results": scores}
