"""The hand-written layer over the generated operations: auth, retries, idempotency keys, typed errors and
``wait_for_video``."""

from __future__ import annotations

import email.utils
import json
import os
import random
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional, Tuple, Union, cast
from urllib.parse import quote, unquote, urlsplit

import httpx

from ._errors import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
    VideoFailedError,
    WaitTimeoutError,
    ZebracatError,
    error_class,
    message_of,
)
from ._generated.operations import GeneratedOperations
from ._types import NotGiven, Operation, RequestOptions
from ._version import __version__

if TYPE_CHECKING:
    from ._generated.types import FileContent, TaskView, UploadLinkStored

DEFAULT_BASE_URL = "https://api.zebracat.ai"
TOKEN_PATH = "/api/oauth/token"

#: Statuses worth another attempt. A 429 is always retried (the API refused before doing any work); a 5xx only
#: when resending cannot do the work twice (see ``Zebracat._safe_to_resend``).
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: ``wait_for_video`` on an API that does not send ``poll_after_s`` yet: seconds per phase.
_FALLBACK_POLL_S = {"planning": 10.0, "generating": 30.0, "rendering": 15.0}
#: Phases where nothing changes until the caller acts (or ever): ``wait_for_video`` returns.
_STOP_PHASES = frozenset({"completed", "plan_ready", "needs_input"})
_FAILED_PHASES = frozenset({"failed", "cancelled"})


class Zebracat(GeneratedOperations):
    """Client for the Zebracat public API.

    Authenticate with an API key (``X-API-KEY``, billed from the API balance) or an OAuth access token
    (``Authorization: Bearer zoa_…``, billed from the account's plan credits)::

        client = Zebracat()                                   # reads ZEBRACAT_API_KEY
        client = Zebracat(api_key="sk-…")
        client = Zebracat(access_token="zoa_…", refresh_token="zor_…", client_id="…",
                          on_token_refresh=save_tokens)        # refreshes once on a 401

    Every API operation is a method named after its ``operationId`` (``create_video``, ``get_video``, …).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        on_token_refresh: Optional[Callable[[Dict[str, Any]], None]] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        max_retry_wait: float = 60.0,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        if api_key is None and access_token is None:
            api_key = os.environ.get("ZEBRACAT_API_KEY") or None
        if api_key and access_token:
            raise ZebracatError("Pass either api_key or access_token, not both.")
        if not api_key and not access_token:
            raise ZebracatError(
                "No credentials: pass api_key=… (or set ZEBRACAT_API_KEY), or access_token=… for OAuth."
            )
        if refresh_token and not client_id:
            raise ZebracatError("refresh_token needs the OAuth client_id it was issued to.")
        self.api_key = api_key
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.on_token_refresh = on_token_refresh
        self.base_url = (base_url or os.environ.get("ZEBRACAT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        #: A 429 or 5xx asking to wait longer than this (seconds) is raised instead of slept through.
        self.max_retry_wait = max_retry_wait
        self._default_headers = dict(default_headers or {})
        self._http = http_client or httpx.Client(follow_redirects=False)
        self._owns_http = http_client is None
        self._refresh_lock = threading.Lock()
        # Test seams: replaced by the test suite to skip real waiting.
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.monotonic

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "Zebracat":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ requests

    def _auth_headers(self) -> Dict[str, str]:
        if self.api_key:
            return {"X-API-KEY": self.api_key}
        return {"Authorization": f"Bearer {self.access_token}"}

    @staticmethod
    def _safe_to_resend(op: Operation) -> bool:
        """True when sending the request again cannot do its work twice."""
        return op.method in _SAFE_METHODS or op.replays_idempotency_key

    def _request(
        self,
        op: Operation,
        *,
        path: Dict[str, Any],
        query: Dict[str, Any],
        body: Any,
        idempotency_key: Optional[str],
        request_options: Optional[RequestOptions],
    ) -> Any:
        opts: RequestOptions = request_options or {}
        url = self.base_url + _fill_path(op.path, path)
        params = {k: _query_value(v) for k, v in query.items() if not isinstance(v, NotGiven) and v is not None}
        request_id = uuid.uuid4().hex
        headers = {
            "User-Agent": f"zebracat-python/{__version__}",
            "Accept": "application/json",
            "X-Request-ID": request_id,
            **self._default_headers,
        }
        if op.paid:
            headers["Idempotency-Key"] = idempotency_key or opts.get("idempotency_key") or f"zc-{uuid.uuid4().hex}"
        headers.update(opts.get("headers") or {})
        content = _encode_body(op, body)
        headers.update(content.pop("headers", {}))
        max_retries = opts.get("max_retries", self.max_retries)
        timeout = opts.get("timeout", self.timeout)

        attempt = 0
        refreshed = False
        while True:
            sent_token = self.access_token
            try:
                response = self._http.request(
                    op.method,
                    url,
                    params=params,
                    headers={**headers, **(self._auth_headers() if op.auth else {})},
                    timeout=timeout,
                    **content,
                )
            except httpx.TimeoutException as exc:
                if attempt < max_retries and self._safe_to_resend(op):
                    attempt += 1
                    self._sleep(_backoff(attempt))
                    continue
                raise APITimeoutError(f"{op.method} {op.path} timed out: {exc}", request_id=request_id) from exc
            except httpx.TransportError as exc:
                never_sent = isinstance(exc, httpx.ConnectError)
                if attempt < max_retries and (never_sent or self._safe_to_resend(op)):
                    attempt += 1
                    self._sleep(_backoff(attempt))
                    continue
                raise APIConnectionError(f"{op.method} {op.path} failed: {exc}", request_id=request_id) from exc

            if response.status_code == 401 and op.auth and self.refresh_token and not refreshed:
                refreshed = True
                self._refresh(sent_token)
                continue
            if response.status_code in RETRY_STATUSES and attempt < max_retries:
                if response.status_code == 429 or self._safe_to_resend(op):
                    wait = self._retry_wait(response, attempt + 1)
                    if wait is not None:
                        attempt += 1
                        self._sleep(wait)
                        continue
            return _parse(response, request_id)

    def _retry_wait(self, response: httpx.Response, attempt: int) -> Optional[float]:
        """Seconds to wait before the next attempt, or None when the API asks for longer than ``max_retry_wait``."""
        after = retry_after_seconds(response.headers.get("Retry-After"))
        if after is None:
            return _backoff(attempt)
        return after if after <= self.max_retry_wait else None

    def _refresh(self, sent_token: Optional[str]) -> None:
        """Swap the refresh token for a new pair (the API rotates both). Another thread may have done it already."""
        with self._refresh_lock:
            if self.access_token != sent_token:
                return
            form = {"grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": self.client_id}
            if self.client_secret:
                form["client_secret"] = self.client_secret
            try:
                response = self._http.post(
                    self.base_url + TOKEN_PATH,
                    data=form,
                    headers={"User-Agent": f"zebracat-python/{__version__}", "Accept": "application/json"},
                    timeout=self.timeout,
                )
            except httpx.TransportError as exc:
                raise APIConnectionError(f"OAuth token refresh failed: {exc}") from exc
            body = _body_of(response)
            if response.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
                raise AuthenticationError(
                    "The access token expired and the refresh token was refused: sign in again.",
                    status_code=response.status_code,
                    body=body,
                    headers=response.headers,
                    request_id=response.headers.get("X-Request-ID"),
                )
            self.access_token = body["access_token"]
            self.refresh_token = body.get("refresh_token") or self.refresh_token
            if self.on_token_refresh:
                self.on_token_refresh(body)

    # ------------------------------------------------------------------ helpers

    def upload_with_link(
        self,
        file: Union[str, "os.PathLike[str]", "FileContent"],
        *,
        use: str = "picture",
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        kind: Optional[str] = None,
        name: Optional[str] = None,
    ) -> "UploadLinkStored":
        """Upload one file through a one-time upload link and return where it is stored.

        ``file`` is a local path, bytes, an open binary file or ``(filename, content[, content_type])``. ``use``
        picks the accepted types and caps (``picture``, ``translate``, ``clone``, ``swap``, ``voice_clone``); send
        the answer's ``url`` to the call ``use`` names (a ``translate`` video goes in ``translate_video(video_url=…)``,
        a picture's ``upload`` in ``create_video(uploads=[…])``). Two calls: ``create_upload_link``, then the file
        goes to the link's ``upload_url`` without credentials (``upload_to_link``).
        """
        if isinstance(file, (str, os.PathLike)):
            path = os.fspath(file)
            with open(path, "rb") as handle:
                data = handle.read()
            part: Optional[Tuple[str, bytes, str]] = (
                os.path.basename(path), data, content_type or "application/octet-stream"
            )
        else:
            part = _file_part(file)
            if part is None:
                raise ZebracatError("file must be a path, bytes, an open binary file or (filename, content).")
        filename = filename or (part[0] if part[0] != "upload" else None)
        fields: Dict[str, Any] = {"use": use, "filename": filename, "content_type": content_type, "kind": kind, "name": name}
        link = self.create_upload_link(**{k: v for k, v in fields.items() if v is not None})
        upload_url = cast(Dict[str, Any], link)["upload_url"]
        marker = "/uploads/link/"
        link_path = urlsplit(upload_url).path
        if marker not in link_path:
            raise ZebracatError(f"Unexpected upload_url: {upload_url}")
        token = unquote(link_path.split(marker, 1)[1])
        return self.upload_to_link(token, body=part)

    def wait_for_video(
        self,
        task_id: str,
        *,
        timeout: float = 1800.0,
        poll_interval: Optional[float] = None,
        on_update: Optional[Callable[["TaskView"], None]] = None,
    ) -> "TaskView":
        """Read ``get_video`` until the video stops changing on its own, waiting ``poll_after_s`` between reads.

        Returns the video when its phase is ``completed`` (``video_url`` is the file), ``plan_ready`` (a plan made
        with ``auto_generate=False``: show the ``quote``, call ``generate_video``, then wait again) or
        ``needs_input`` (answer ``question`` with ``answer_video_question``). Raises :class:`VideoFailedError` when
        it ends ``failed`` or ``cancelled``, and :class:`WaitTimeoutError` after ``timeout`` seconds (the video
        keeps going; wait again later). ``poll_interval`` overrides the API's ``poll_after_s``.
        """
        deadline = self._clock() + timeout
        while True:
            answer = self.get_video(task_id)
            if on_update:
                on_update(answer)
            video: Dict[str, Any] = cast(Dict[str, Any], answer)
            phase = video.get("phase")
            if phase in _FAILED_PHASES:
                reason = video.get("error")
                if isinstance(reason, dict):
                    reason = reason.get("message") or reason.get("code")
                raise VideoFailedError(f"Video {task_id} {phase}" + (f": {reason}" if reason else ""), video=video)
            if "poll_after_s" in video:
                wait = video["poll_after_s"]
                if wait is None:
                    return answer
            elif phase in _STOP_PHASES:
                return answer
            else:
                wait = _FALLBACK_POLL_S.get(str(phase), 15.0)
            if poll_interval is not None:
                wait = poll_interval
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise WaitTimeoutError(f"Video {task_id} is still {phase} after {timeout:g} s.", video=video)
            self._sleep(max(1.0, min(float(wait), remaining)))


# ---------------------------------------------------------------------- module helpers


def _fill_path(template: str, values: Dict[str, Any]) -> str:
    for name, value in values.items():
        text = str(value)
        # "", "." and ".." would resolve to another endpoint once the URL is normalized: refuse them.
        if text in ("", ".", ".."):
            raise ZebracatError(f"Invalid {name}: {text!r}")
        template = template.replace("{" + name + "}", quote(text, safe=""))
    return template


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _strip_not_given(body: Any) -> Any:
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if not isinstance(v, NotGiven)}
    return body


def _file_part(value: Any) -> Optional[Tuple[str, bytes, str]]:
    """``(filename, bytes, content_type)`` for a file value, read once so retries can resend it; None otherwise."""
    if isinstance(value, (bytes, bytearray)):
        return ("upload", bytes(value), "application/octet-stream")
    if isinstance(value, tuple) and len(value) in (2, 3):
        name, data = value[0], value[1]
        reader = getattr(data, "read", None)
        if reader is not None:
            data = reader()
        kind = value[2] if len(value) == 3 else "application/octet-stream"
        return (str(name), bytes(data), kind)
    if hasattr(value, "read"):
        name = os.path.basename(getattr(value, "name", "") or "upload")
        return (name, value.read(), "application/octet-stream")
    return None


def _encode_body(op: Operation, body: Any) -> Dict[str, Any]:
    body = _strip_not_given(body)
    if body is None or isinstance(body, NotGiven):
        return {}
    if op.body == "binary":
        part = _file_part(body)
        if part is None:
            raise ZebracatError("body must be bytes, an open binary file or (filename, content[, content_type]).")
        return {"content": part[1], "headers": {"Content-Type": part[2]}}
    if op.body != "multipart":
        return {"json": body}
    files: Dict[str, Tuple[str, bytes, str]] = {}
    data: Dict[str, Any] = {}
    for key, value in body.items():
        if value is None:
            continue
        part = _file_part(value)
        if part is not None:
            files[key] = part
        elif isinstance(value, (dict, list)):
            data[key] = json.dumps(value)
        elif isinstance(value, bool):
            data[key] = "true" if value else "false"
        else:
            data[key] = str(value)
    return {"data": data, "files": files or None}


def _body_of(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _parse(response: httpx.Response, request_id: str) -> Any:
    status = response.status_code
    if status >= 400:
        body = _body_of(response)
        rid = (body.get("request_id") if isinstance(body, dict) else None) or response.headers.get("X-Request-ID")
        cls = error_class(status)
        kwargs: Dict[str, Any] = dict(
            status_code=status, body=body, headers=response.headers, request_id=rid or request_id
        )
        if cls is RateLimitError:
            kwargs["retry_after"] = retry_after_seconds(response.headers.get("Retry-After"))
        raise cls(message_of(body, status), **kwargs)
    if 300 <= status < 400:
        return response.headers.get("Location")
    return _body_of(response)


def retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """``Retry-After`` as seconds: a number, or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def _backoff(attempt: int) -> float:
    """0.5 s, 1 s, 2 s, … capped at 8 s, minus up to 25 % jitter."""
    base = min(8.0, 0.5 * (2 ** (attempt - 1)))
    return base * (1 - 0.25 * random.random())
