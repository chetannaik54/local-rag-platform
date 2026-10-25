"""Thin async client for the local Ollama HTTP API."""
import json
import logging
from collections.abc import AsyncIterator, Callable

import httpx
import ollama
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_ollama import ChatOllama, OllamaEmbeddings

from app.core.errors import OllamaError
from app.models.schemas import RuntimeSettings

log = logging.getLogger(__name__)


def model_matches(wanted: str, installed: str) -> bool:
    """'llama3.1' matches 'llama3.1:latest'; tagged names must match exactly."""
    if ":" not in wanted:
        wanted += ":latest"
    if ":" not in installed:
        installed += ":latest"
    return wanted == installed


class OllamaService:
    def __init__(self, settings: Callable[[], RuntimeSettings], timeout_s: float = 120.0):
        self._settings = settings
        self._timeout = timeout_s
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=5.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def base(self) -> str:
        return self._settings().ollama_url

    def _unreachable(self, exc: Exception) -> OllamaError:
        return OllamaError(
            "ollama_unreachable",
            f"Cannot reach Ollama at {self.base}. Make sure Ollama is installed and running "
            f"(start it with 'ollama serve' or open the Ollama app). ({type(exc).__name__})",
        )

    @staticmethod
    def _error_text(body: bytes) -> str:
        try:
            return str(json.loads(body).get("error", "")) or body.decode(errors="replace")[:300]
        except Exception:
            return body.decode(errors="replace")[:300]

    def _http_error(self, status: int, body: bytes, model: str | None) -> OllamaError:
        text = self._error_text(body)
        if status == 404 or "not found" in text.lower():
            return OllamaError(
                "model_missing",
                f"Model '{model}' is not installed in Ollama. Run: ollama pull {model}",
                424,
            )
        return OllamaError("ollama_error", f"Ollama returned HTTP {status}: {text}", 502)

    # -- discovery ---------------------------------------------------------
    async def version(self) -> str | None:
        try:
            r = await self._client.get(f"{self.base}/api/version", timeout=5.0)
            return r.json().get("version")
        except (httpx.HTTPError, ValueError):
            return None

    async def list_models(self) -> list[dict]:
        try:
            r = await self._client.get(f"{self.base}/api/tags", timeout=10.0)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if r.status_code != 200:
            raise self._http_error(r.status_code, r.content, None)
        return [
            {
                "name": m["name"],
                "size_bytes": m.get("size"),
                "family": (m.get("details") or {}).get("family"),
            }
            for m in r.json().get("models", [])
        ]

    async def ensure_models(self, *names: str) -> None:
        installed = [m["name"] for m in await self.list_models()]
        for n in names:
            if not any(model_matches(n, i) for i in installed):
                raise OllamaError(
                    "model_missing",
                    f"Model '{n}' is not installed in Ollama. Run: ollama pull {n}",
                    424,
                )

    def _map_exc(self, exc: Exception, model: str) -> OllamaError:
        """Translate ollama-client / httpx failures raised through LangChain."""
        if isinstance(exc, OllamaError):
            return exc
        if isinstance(exc, ollama.ResponseError):
            body = json.dumps({"error": exc.error}).encode()
            return self._http_error(exc.status_code, body, model)
        if isinstance(exc, (httpx.HTTPError, ConnectionError, OSError)):
            return self._unreachable(exc)
        return OllamaError("ollama_error", f"{type(exc).__name__}: {exc}", 502)

    # -- embeddings (LangChain OllamaEmbeddings) -----------------------------
    async def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        emb = OllamaEmbeddings(
            model=model, base_url=self.base, client_kwargs={"timeout": self._timeout}
        )
        try:
            vectors = await emb.aembed_documents(inputs)
        except Exception as exc:
            raise self._map_exc(exc, model) from exc
        if len(vectors) != len(inputs):
            raise OllamaError(
                "ollama_error",
                f"Embedding model '{model}' returned {len(vectors)} vectors for {len(inputs)} inputs. "
                "Is it an embedding model?",
                502,
            )
        return vectors

    # -- chat (LangChain ChatOllama) -----------------------------------------
    def _llm(self, model: str, options: dict) -> ChatOllama:
        return ChatOllama(
            model=model, base_url=self.base,
            temperature=options.get("temperature"),
            num_ctx=options.get("num_ctx"),
            num_predict=options.get("num_predict"),
            client_kwargs={"timeout": self._timeout},
        )

    @staticmethod
    def _lc_messages(messages: list[dict]):
        out = []
        for m in messages:
            role = m["role"]
            if role == "assistant" and m.get("tool_calls"):
                out.append(AIMessage(content=m["content"], tool_calls=[
                    {"name": t["name"], "args": t["args"], "id": t["id"], "type": "tool_call"}
                    for t in m["tool_calls"]]))
            elif role == "tool":
                out.append(ToolMessage(content=m["content"], tool_call_id=m["tool_call_id"]))
            else:
                out.append({"system": SystemMessage, "user": HumanMessage,
                            "assistant": AIMessage}[role](content=m["content"]))
        return out

    async def chat_stream_tools(
        self, model: str, messages: list[dict], tools: list[dict], options: dict
    ) -> AsyncIterator[dict]:
        """Yields {"token": str} while streaming, then one {"tool_calls": [...]} if the model asked."""
        calls: list[dict] = []
        try:
            llm = self._llm(model, options).bind_tools(tools)
            async for chunk in llm.astream(self._lc_messages(messages)):
                if chunk.content:
                    yield {"token": chunk.content}
                for tc in chunk.tool_calls or []:
                    calls.append({"id": tc.get("id") or f"call_{len(calls)}", "name": tc["name"],
                                  "args": tc.get("args") or {}})
        except Exception as exc:
            raise self._map_exc(exc, model) from exc
        if calls:
            yield {"tool_calls": calls}

    async def chat_stream(
        self, model: str, messages: list[dict], options: dict
    ) -> AsyncIterator[str]:
        try:
            async for chunk in self._llm(model, options).astream(self._lc_messages(messages)):
                if chunk.content:
                    yield chunk.content
        except Exception as exc:
            raise self._map_exc(exc, model) from exc

    async def chat(self, model: str, messages: list[dict], options: dict) -> str:
        try:
            out = await self._llm(model, options).ainvoke(self._lc_messages(messages))
        except Exception as exc:
            raise self._map_exc(exc, model) from exc
        return out.content if isinstance(out.content, str) else str(out.content)
