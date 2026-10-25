"""Local speech-to-text via faster-whisper (optional dependency; nothing leaves the machine)."""
import asyncio
import logging
import tempfile
from pathlib import Path

from app.core.errors import AppError

log = logging.getLogger(__name__)


class VoiceService:
    def __init__(self, model_name: str):
        self._name = model_name
        self._model = None
        self._lock = asyncio.Lock()

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading whisper model '%s' (first use downloads it)", self._name)
            self._model = WhisperModel(self._name, device="cpu", compute_type="int8")
        return self._model

    def _transcribe(self, path: str, language: str | None) -> tuple[str, str | None]:
        segments, info = self._load().transcribe(path, language=language, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip(), info.language

    async def transcribe(self, data: bytes, suffix: str, language: str | None) -> tuple[str, str | None]:
        if not self.available():
            raise AppError(
                "voice_unavailable",
                "Speech-to-text is not installed. Run: pip install faster-whisper", 501)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            path = f.name
        try:
            async with self._lock:  # one transcription at a time
                try:
                    return await asyncio.to_thread(self._transcribe, path, language)
                except AppError:
                    raise
                except Exception as exc:
                    log.exception("transcription failed")
                    raise AppError("transcription_failed",
                                   f"Could not transcribe audio: {type(exc).__name__}", 422) from exc
        finally:
            Path(path).unlink(missing_ok=True)
