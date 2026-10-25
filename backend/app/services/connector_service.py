"""Folder and URL connectors: keep a document set in sync with an external source."""
import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import AppConfig
from app.core.errors import AppError
from app.database.db import now
from app.database.repos import Store
from app.services import parsers

log = logging.getLogger(__name__)
MAX_URL_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_SYNC = 500


def _is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError) as exc:  # UnicodeError: empty / over-long DNS labels
        raise AppError("bad_url", f"Cannot resolve host '{host[:80]}'.", 422) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


class ConnectorService:
    def __init__(self, config: AppConfig, store: Store, ingestion):
        self._cfg = config
        self._store = store
        self._ingestion = ingestion
        self._running: set[str] = set()
        self._task: asyncio.Task | None = None

    # -- validation --------------------------------------------------------
    def allowed_roots(self) -> list[Path]:
        return self._cfg.connector_root_list

    def org_roots(self, org_slug: str, is_super: bool) -> list[Path]:
        """Folders an organisation may attach. Tenants are confined to <root>/<their slug>; only the
        platform operator may use a whole connector root (so one customer cannot read another's files)."""
        return self.allowed_roots() if is_super else [(r / org_slug).resolve() for r in self.allowed_roots()]

    def validate(self, ctype: str, ref: str, org_slug: str = "default", is_super: bool = True) -> str:
        if ctype == "folder":
            p = Path(ref).expanduser().resolve()
            roots = self.org_roots(org_slug, is_super)
            if not any(p == r or r in p.parents for r in roots):
                raise AppError(
                    "path_not_allowed",
                    "Folder must be inside: " + ", ".join(str(r) for r in roots)
                    + " (the platform admin can change CONNECTOR_ROOTS).", 403)
            if not p.is_dir():
                raise AppError("not_found", f"Folder does not exist: {p}", 422)
            return str(p)
        parsed = urlparse(ref)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AppError("bad_url", "URL must start with http:// or https://", 422)
        self._check_host(parsed.hostname)
        return ref

    def _check_host(self, host: str) -> None:
        if not self._cfg.allow_private_urls and _is_private(host):
            raise AppError("bad_url", "URLs pointing at localhost or private networks are blocked "
                                      "(set ALLOW_PRIVATE_URLS=true to allow).", 403)

    # -- sync --------------------------------------------------------------
    async def sync(self, cid: str) -> str:
        c = self._store.get_connector(cid)
        if not c:
            raise AppError("not_found", "Connector not found.", 404)
        if cid in self._running:
            return "already running"
        self._running.add(cid)
        try:
            summary = await (self._sync_folder(c) if c["type"] == "folder" else self._sync_url(c))
            status = f"ok: {summary}"
        except AppError as exc:
            status = f"error: {exc.message}"
        except Exception as exc:
            log.exception("connector sync failed id=%s", cid)
            status = f"error: {type(exc).__name__}"
        finally:
            self._running.discard(cid)
        if self._store.get_connector(cid):
            self._store.update_connector(cid, last_sync=now(), last_status=status)
        log.info("connector %s sync %s", cid, status)
        return status

    async def _replace_or_add(self, c: dict, src: Path, name: str, ref: str, existing: dict | None) -> str:
        sha = await asyncio.to_thread(lambda: hashlib.sha256(src.read_bytes()).hexdigest())
        if existing and existing["sha256"] == sha:
            return "unchanged"
        if existing:
            await self._ingestion.delete(existing["id"])
        try:
            _, dup = await self._ingestion.ingest_path(src, name, c["type"], ref, c["id"], c["org_id"])
        except AppError as exc:
            log.warning("connector %s skipped %s: %s", c["id"], name, exc.message)
            return "skipped"
        return "duplicate" if dup else ("updated" if existing else "added")

    async def _sync_folder(self, c: dict) -> str:
        root = Path(c["ref"]).resolve()
        if not any(root == r or r in root.parents for r in self.allowed_roots()):
            raise AppError("path_not_allowed", "Folder is outside the allowed connector roots.", 403)
        if not root.is_dir():
            raise AppError("not_found", "Folder no longer exists.", 422)
        candidates = sorted(p for p in root.rglob("*")
                            if p.is_file() and p.suffix.lower() in parsers.ALLOWED_EXTENSIONS
                            and root in p.resolve().parents)
        truncated = len(candidates) > MAX_FILES_PER_SYNC
        files = candidates[:MAX_FILES_PER_SYNC]
        known = {d["source_ref"]: d for d in self._store.connector_documents(c["id"])}
        counts: dict[str, int] = {}
        seen = set()
        for p in files:
            rel = p.relative_to(root).as_posix()
            seen.add(rel)
            r = await self._replace_or_add(c, p, p.name, rel, known.get(rel))
            counts[r] = counts.get(r, 0) + 1
        if truncated:  # we only saw part of the folder, so "missing" does not mean "deleted"
            counts[f"skipped (folder has {len(candidates)} files, limit {MAX_FILES_PER_SYNC})"] = len(candidates) - len(files)
        else:
            for rel, d in known.items():
                if rel not in seen:
                    await self._ingestion.delete(d["id"])
                    counts["removed"] = counts.get("removed", 0) + 1
        return ", ".join(f"{v} {k}" for k, v in counts.items()) or "no files"

    async def _fetch(self, url: str) -> tuple[str, bytes]:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False,
                                     headers={"User-Agent": "local-rag-connector/1.0"}) as client:
            for _ in range(4):
                host = urlparse(url).hostname or ""
                self._check_host(host)
                async with client.stream("GET", url) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code != 200:
                        raise AppError("fetch_failed", f"Server returned HTTP {r.status_code}.", 502)
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_URL_BYTES:
                            raise AppError("file_too_large", "Page is larger than 5 MB.", 413)
                    return ctype, bytes(body)
        raise AppError("fetch_failed", "Too many redirects.", 502)

    @staticmethod
    def _html_to_text(raw: bytes) -> tuple[str, str]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw, "html.parser")
        title = (soup.title.string or "").strip() if soup.title else ""
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        return title, soup.get_text("\n")

    async def _sync_url(self, c: dict) -> str:
        url = c["ref"]
        ctype, raw = await self._fetch(url)
        host = urlparse(url).hostname or "page"
        if ctype == "application/pdf":
            name, data = f"{host}.pdf", raw
        elif ctype in ("text/html", "application/xhtml+xml"):
            title, text = await asyncio.to_thread(self._html_to_text, raw)
            name = re.sub(r"[^\w .-]+", "_", title or host)[:80].strip() + ".txt"
            data = f"{title}\n{url}\n\n{text}".encode()
        elif ctype.startswith("text/"):
            name, data = f"{host}.txt", raw
        else:
            raise AppError("unsupported_type", f"Unsupported content type '{ctype}'.", 415)
        tmp = self._cfg.data_dir / "uploads" / f".url-{c['id']}"
        tmp.write_bytes(data)
        try:
            existing = next(iter(self._store.connector_documents(c["id"])), None)
            r = await self._replace_or_add(c, tmp, name, url, existing)
        finally:
            tmp.unlink(missing_ok=True)
        return r

    # -- scheduler ---------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                for c in self._store.list_connectors():
                    if not c["enabled"] or c["interval_minutes"] <= 0 or c["id"] in self._running:
                        continue
                    last = datetime.fromisoformat(c["last_sync"]) if c["last_sync"] else None
                    due = (last is None or
                           datetime.now(UTC) - last >= timedelta(minutes=c["interval_minutes"]))
                    if due:
                        await self.sync(c["id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("connector scheduler error")
