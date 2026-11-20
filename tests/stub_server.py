"""A local HTTP server that answers scripted responses and records every request it gets."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit


@dataclass
class Recorded:
    method: str
    path: str
    query: Dict[str, List[str]]
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    headers: Dict[str, str] = field(default_factory=dict)


class StubServer:
    def __init__(self) -> None:
        self.requests: List[Recorded] = []
        self._routes: Dict[Tuple[str, str], List[Reply]] = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append(
                    Recorded(self.command, parts.path, parse_qs(parts.query), {k.lower(): v for k, v in self.headers.items()}, body)
                )
                queue = stub._routes.get((self.command, parts.path)) or []
                reply = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else Reply(404, {"error": "no stub"}))
                payload = b"" if reply.body is None else json.dumps(reply.body).encode()
                self.send_response(reply.status)
                if payload:
                    self.send_header("Content-Type", "application/json")
                for k, v in reply.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def on(self, method: str, path: str, *replies: Reply) -> None:
        """Answer ``method path`` with ``replies`` in order; the last one repeats."""
        self._routes[(method, path)] = list(replies)

    def calls(self, method: Optional[str] = None, path: Optional[str] = None) -> List[Recorded]:
        return [r for r in self.requests if (method is None or r.method == method) and (path is None or r.path == path)]

    def __enter__(self) -> "StubServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
