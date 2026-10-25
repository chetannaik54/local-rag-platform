"""Batching + model-specific prefixes on top of the Ollama embedding endpoint."""
from app.models.schemas import RuntimeSettings


def _prefixes(model: str) -> tuple[str, str]:
    # nomic-embed-text was trained with task prefixes; other models use none.
    if "nomic-embed" in model:
        return "search_document: ", "search_query: "
    return "", ""


class EmbeddingService:
    def __init__(self, ollama, batch_size: int = 32):
        self._ollama = ollama
        self._batch = batch_size

    async def embed_documents(self, texts: list[str], settings: RuntimeSettings) -> list[list[float]]:
        prefix, _ = _prefixes(settings.embedding_model)
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            batch = [prefix + t for t in texts[i : i + self._batch]]
            out.extend(await self._ollama.embed(settings.embedding_model, batch))
        return out

    async def embed_query(self, text: str, settings: RuntimeSettings) -> list[float]:
        _, prefix = _prefixes(settings.embedding_model)
        return (await self._ollama.embed(settings.embedding_model, [prefix + text]))[0]
