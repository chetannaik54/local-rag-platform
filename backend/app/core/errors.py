"""Application errors and their JSON envelope: {"error": {"code", "message"}}."""


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class OllamaError(AppError):
    """Raised when Ollama is unreachable, a model is missing, or a request fails."""

    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(code, message, status)


def envelope(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
