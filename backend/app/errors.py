"""Explicit application failures shared by the store and API."""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}
