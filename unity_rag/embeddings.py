"""Local embedding adapter. Providers expose config, passages(texts), and query(text)."""
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


DEFAULT_BATCH_SIZE = 4


def validate_batch_size(value):
    if type(value) is not int or not 1 <= value <= 128:
        raise ValueError('Embedding batch size must be an integer between 1 and 128.')


class FastEmbedProvider:
    def __init__(self, model=DEFAULT_MODEL, cache_dir=None, batch_size=DEFAULT_BATCH_SIZE):
        validate_batch_size(batch_size)
        self.batch_size = batch_size
        try:
            runtime = version("fastembed")
        except PackageNotFoundError as exc:
            raise ValueError('Install local embeddings with: pip install "unity-rag[semantic]"') from exc
        self.config = {"provider": "fastembed", "model": model, "runtime": runtime,
                       "adapter_version": 1}
        self.cache_dir = str(Path(cache_dir).resolve()) if cache_dir else None
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.config["model"],
                                        cache_dir=self.cache_dir, threads=4)
        return self._model

    def passages(self, texts):
        return [v.tolist() for v in self._load().passage_embed(texts, batch_size=self.batch_size)]

    def query(self, text):
        return next(self._load().query_embed(text)).tolist()
