"""Reproducible entity-level retrieval evaluation; no model judges."""
import json
from pathlib import Path
from .retrieval import retrieve


def evaluate(workspace, dataset, limit=5):
    data = json.loads(Path(dataset).read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1 or not data.get("questions"):
        raise ValueError("Evaluation requires schema_version 1 and a nonempty questions list.")
    scores = []
    for item in data["questions"]:
        relevant = set(item["relevant_entities"])
        if not relevant:
            raise ValueError("Each evaluation question must label at least one relevant entity.")
        result = retrieve(workspace, item["query"], mode=item.get("mode", "search"),
                          limit=limit, kind=item.get("kind"))
        ranked = []
        for row in result["results"]:
            entity = row.get("entity_id") or row.get("source")
            if entity not in ranked:
                ranked.append(entity)
        hits = relevant.intersection(ranked)
        reciprocal = next((1 / (i + 1) for i, entity in enumerate(ranked) if entity in relevant), 0)
        scores.append({"id": item["id"], "query": item["query"], "retrieved_entities": ranked,
                       "relevant_entities": sorted(relevant), "recall_at_k": len(hits) / len(relevant),
                       "precision_at_k": len(hits) / limit, "reciprocal_rank": reciprocal})
    return {"dataset_version": data.get("version"), "k": limit, "questions": len(scores),
            "recall_at_k": sum(s["recall_at_k"] for s in scores) / len(scores),
            "precision_at_k": sum(s["precision_at_k"] for s in scores) / len(scores),
            "mean_reciprocal_rank": sum(s["reciprocal_rank"] for s in scores) / len(scores),
            "evaluation_unit": "Unique entity IDs in the top-k evidence records; context expansion excluded. Structured queries label source entities.",
            "results": scores}
