"""Errors. Every refusal carries a reason the UI can show the operator."""

from __future__ import annotations


class LabError(Exception):
    """Base error. ``detail`` is safe to show in the UI. ``status`` is the backend's HTTP code, if any."""

    http_status = 400

    def __init__(self, message: str, *, detail: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status

    def as_dict(self) -> dict:
        return {"error": self.message, "detail": self.detail, "type": type(self).__name__}


class GuardrailViolation(LabError):
    """The request would have touched the live lab, the host, or a hard limit."""

    http_status = 409


class NotFound(LabError):
    http_status = 404


class NotConfigured(LabError):
    """A required credential or backend is not configured."""

    http_status = 501


class BackendError(LabError):
    """Proxmox, Mist or a switch console returned an error."""

    http_status = 502
