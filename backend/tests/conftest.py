import json
import math
import re
import time
import zlib

import pytest
from fastapi.testclient import TestClient

from app.core.config import AppConfig
from app.core.errors import OllamaError
from app.main import create_app

DIM = 512


def fake_embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding: related texts really are closer."""
    v = [0.0] * DIM
    for w in re.findall(r"[a-z0-9]{3,}", text.lower()):
        v[zlib.crc32(w.encode()) % DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class FakeOllama:
    """Same interface as OllamaService, no network."""

    def __init__(self):
        self.models = ["llama3.1:8b", "nomic-embed-text:latest"]
        self.down = False
        self.chat_calls: list[list[dict]] = []
        self.reply = None  # override reply text
        self.tool_script: list[list[dict]] = []
        self.after_tool = None
        self.tool_specs: list[dict] = []

    async def aclose(self):
        pass

    def _check(self):
        if self.down:
            raise OllamaError("ollama_unreachable", "Cannot reach Ollama at http://localhost:11434.")

    async def version(self):
        return "0.0-test"

    async def list_models(self):
        self._check()
        return [{"name": m, "size_bytes": 1, "family": "test"} for m in self.models]

    async def ensure_models(self, *names):
        from app.services.ollama_service import model_matches

        self._check()
        for n in names:
            if not any(model_matches(n, m) for m in self.models):
                raise OllamaError("model_missing", f"Model '{n}' is not installed. Run: ollama pull {n}", 424)

    async def embed(self, model, inputs):
        self._check()
        return [fake_embed(re.sub(r"^search_(document|query): ", "", t)) for t in inputs]

    async def chat(self, model, messages, options):
        self._check()
        self.chat_calls.append(messages)
        # a real model resolves "it"; the fake just prepends the prior conversation
        return messages[-1]["content"].replace("Conversation:", "").replace("Latest question:", "")

    async def chat_stream_tools(self, model, messages, tools, options):
        """Scripted tool use: pop one batch of calls from `tool_script` per model turn."""
        self._check()
        self.chat_calls.append(messages)
        self.tool_specs = tools
        if self.tool_script and messages[-1]["role"] != "tool":
            calls = self.tool_script.pop(0)
            yield {"tool_calls": [{"id": f"c{i}", "name": c["name"], "args": c.get("args", {})}
                                  for i, c in enumerate(calls)]}
            return
        text = self.reply or "The answer is in the document [1]."
        if messages[-1]["role"] == "tool":
            text = self.after_tool or f"Tool said: {messages[-1]['content']}"
        for tok in re.findall(r"\S+\s*", text):
            yield {"token": tok}

    async def chat_stream(self, model, messages, options):
        self._check()
        self.chat_calls.append(messages)
        text = self.reply or "The answer is in the document [1]."
        for tok in re.findall(r"\S+\s*", text):
            yield tok


@pytest.fixture
def fake():
    return FakeOllama()


@pytest.fixture
def client(tmp_path, fake):
    cfg = AppConfig(data_dir=tmp_path, similarity_threshold=0.25, _env_file=None)
    with TestClient(create_app(cfg, ollama=fake, run_scheduler=False)) as c:
        c.sv = c.app.state.services
        yield c


def wait_ready(client, doc_id, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/documents/{doc_id}").json()
        if d["status"] != "processing":
            return d
        time.sleep(0.05)
    raise AssertionError("document still processing")


def upload(client, name, content: bytes, ctype="text/plain"):
    r = client.post("/api/documents/upload", files=[("files", (name, content, ctype))])
    return r


def upload_ready(client, name, content: bytes):
    r = upload(client, name, content)
    assert r.status_code == 202, r.text
    doc = r.json()["results"][0]["document"]
    return wait_ready(client, doc["id"])


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        ev = re.search(r"^event: (.+)$", block, re.MULTILINE).group(1)
        data = json.loads(re.search(r"^data: (.+)$", block, re.MULTILINE).group(1))
        events.append((ev, data))
    return events



# ---------------------------------------------------------------- multi-tenant helpers
PW = "correct-horse-1"
PARIS = b"The Eiffel Tower is located in Paris France. It was completed in 1889 for the World Fair."
BREAD = b"To bake sourdough bread you need flour water salt and an active starter. Proof overnight."


def make_client(tmp_path, fake, **cfg):
    from app.core.config import AppConfig
    from app.main import create_app

    base = {"data_dir": tmp_path, "similarity_threshold": 0.25, "_env_file": None}
    return TestClient(create_app(AppConfig(**{**base, **cfg}), ollama=fake, run_scheduler=False))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class World:
    """An auth-enabled platform with helpers to make organizations, users and logins."""

    def __init__(self, client, root_token):
        self.c = client
        self.root = bearer(root_token)

    def login(self, email, password=PW):
        r = self.c.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return bearer(r.json()["token"])

    def org(self, slug, password=PW):
        """Create an organization; returns (org json, admin headers)."""
        email = f"admin@{slug}.test"
        r = self.c.post("/api/orgs", json={"name": slug.title(), "slug": slug, "admin_email": email,
                                           "admin_password": password}, headers=self.root)
        assert r.status_code == 201, r.text
        return r.json(), self.login(email, password)

    def user(self, admin, name, role="member", password=PW, **extra):
        r = self.c.post("/api/users", json={"name": name, "role": role, "email": f"{name}@x.test",
                                            "password": password, "api_key": False, **extra}, headers=admin)
        assert r.status_code == 201, r.text
        return self.login(f"{name}@x.test", password), r.json()


@pytest.fixture
def world(tmp_path, fake):
    with make_client(tmp_path, fake, auth_enabled=True, admin_email="root@example.com",
                     admin_password="Sup3r-secret-pw!") as c:
        c.sv = c.app.state.services
        w = World(c, (tmp_path / "admin_token.txt").read_text().strip())
        w.fake = fake
        yield w


def ready(c, headers, name, data):
    r = c.post("/api/documents/upload", files=[("files", (name, data))], headers=headers)
    assert r.status_code == 202, r.text
    doc = r.json()["results"][0]["document"]
    end = time.time() + 20
    while time.time() < end:
        d = c.get(f"/api/documents/{doc['id']}", headers=headers).json()
        if d["status"] != "processing":
            return d
        time.sleep(0.05)
    raise AssertionError("still processing")
