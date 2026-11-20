"""Small shared types: the operation table entry, the "not given" sentinel and per-call options."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, TypedDict


@dataclass(frozen=True)
class Operation:
    """One API operation, as the generator read it from the OpenAPI document."""

    operation_id: str
    method: str
    path: str
    #: ``"json"``, ``"multipart"`` or ``None`` (no request body).
    body: Optional[str] = None
    #: A POST that spends money: it carries an ``Idempotency-Key``.
    paid: bool = False
    #: The server answers a repeated ``Idempotency-Key`` with the first answer, so a 5xx may be retried.
    replays_idempotency_key: bool = False
    deprecated: bool = False


class NotGiven:
    """Marks an argument the caller left out (``None`` is sent as JSON ``null``)."""

    _instance: Optional["NotGiven"] = None

    def __new__(cls) -> "NotGiven":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = NotGiven()


class RequestOptions(TypedDict, total=False):
    """Per-call overrides."""

    #: Seconds before one HTTP attempt times out.
    timeout: float
    #: Retries after the first attempt (429 and 5xx, see the README).
    max_retries: int
    #: Extra headers for this call.
    headers: Dict[str, str]
    #: The ``Idempotency-Key`` for a paid POST (same as the method's ``idempotency_key`` argument).
    idempotency_key: str
