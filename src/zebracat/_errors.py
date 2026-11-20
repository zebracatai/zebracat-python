"""Typed errors. Every error that came from the API carries the ``request_id`` to quote to support."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional


class ZebracatError(Exception):
    """Base class of every error this package raises."""

    request_id: Optional[str] = None


class APIConnectionError(ZebracatError):
    """The request never got an HTTP answer (DNS, refused connection, reset, timeout)."""

    def __init__(self, message: str, *, request_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class APITimeoutError(APIConnectionError):
    """The HTTP attempt timed out."""


class APIError(ZebracatError):
    """The API answered with an error status. The fields mirror the public error envelope."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.headers: Dict[str, str] = dict(headers or {})
        self.request_id = request_id
        data = body if isinstance(body, dict) else {}
        #: Machine-readable slug, e.g. ``insufficient_balance`` or ``invalid_choice``.
        self.code: Optional[str] = data.get("code")
        #: Error class: ``invalid_request``, ``authentication``, ``insufficient_funds``, ``rate_limited``, …
        self.type: Optional[str] = data.get("type")
        self.field: Optional[str] = data.get("field")
        self.hint: Optional[str] = data.get("hint")
        self.docs_url: Optional[str] = data.get("docs_url")
        self.retryable: Optional[bool] = data.get("retryable")
        self.allowed_values: Optional[List[Any]] = data.get("allowed_values")
        self.errors: List[Dict[str, Any]] = list(data.get("errors") or [])
        #: Where to add credit (402) or upgrade the plan (403), when the API names one.
        self.topup_url: Optional[str] = data.get("topup_url")
        self.upgrade_url: Optional[str] = data.get("upgrade_url")

    def __str__(self) -> str:
        text = f"{self.status_code} {super().__str__()}"
        if self.hint:
            text += f" (hint: {self.hint})"
        if self.request_id:
            text += f" [request_id={self.request_id}]"
        return text


class BadRequestError(APIError):
    """400 or 422: the request is invalid. ``field``, ``allowed_values`` and ``errors`` say what to fix."""


class AuthenticationError(APIError):
    """401: the API key or access token is missing, wrong, revoked or expired."""


class InsufficientFundsError(APIError):
    """402: not enough balance or credits. ``topup_url`` is where to add more."""


class PermissionDeniedError(APIError):
    """403: the plan, the token scope or the account does not allow this. ``upgrade_url`` when a plan would."""


class NotFoundError(APIError):
    """404 or 410: no such object of yours."""


class ConflictError(APIError):
    """409: the object is not in a state that allows this yet (e.g. downloading a video still being made)."""


class RateLimitError(APIError):
    """429: too many requests, or a daily cap. ``retry_after`` is the wait in seconds the API asked for."""

    def __init__(self, *args: Any, retry_after: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class InternalServerError(APIError):
    """5xx: the API failed or is unavailable. Safe calls were already retried."""


class VideoFailedError(ZebracatError):
    """``wait_for_video``: the video ended ``failed`` or ``cancelled``. ``video`` is the last answer."""

    def __init__(self, message: str, *, video: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.video = video
        self.phase = video.get("phase")
        self.error = video.get("error")


class WaitTimeoutError(ZebracatError):
    """``wait_for_video``: the timeout passed first. ``video`` is the last answer; the job keeps running."""

    def __init__(self, message: str, *, video: Optional[Mapping[str, Any]]) -> None:
        super().__init__(message)
        self.video = video


_BY_STATUS = {
    400: BadRequestError,
    401: AuthenticationError,
    402: InsufficientFundsError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    410: NotFoundError,
    422: BadRequestError,
    429: RateLimitError,
}


def error_class(status_code: int) -> type:
    if status_code in _BY_STATUS:
        return _BY_STATUS[status_code]
    if status_code >= 500:
        return InternalServerError
    return BadRequestError if status_code >= 400 else APIError


def message_of(body: Any, status_code: int) -> str:
    if isinstance(body, dict):
        for key in ("error", "detail", "message", "error_description"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("error"), str):
                return value["error"]
    if isinstance(body, str) and body.strip():
        return body.strip()[:500]
    return f"HTTP {status_code}"
