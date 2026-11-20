"""Zebracat: make videos with AI from Python. See https://api.zebracat.ai/docs."""

from ._client import DEFAULT_BASE_URL, Zebracat
from ._errors import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InsufficientFundsError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    VideoFailedError,
    WaitTimeoutError,
    ZebracatError,
)
from ._generated import types
from ._types import NOT_GIVEN, NotGiven, RequestOptions
from ._version import __version__

__all__ = [
    "DEFAULT_BASE_URL",
    "NOT_GIVEN",
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "AuthenticationError",
    "BadRequestError",
    "ConflictError",
    "InsufficientFundsError",
    "InternalServerError",
    "NotFoundError",
    "NotGiven",
    "PermissionDeniedError",
    "RateLimitError",
    "RequestOptions",
    "VideoFailedError",
    "WaitTimeoutError",
    "Zebracat",
    "ZebracatError",
    "__version__",
    "types",
]
