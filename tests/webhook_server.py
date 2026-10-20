"""A real HTTP server on 127.0.0.1 that records what it is sent (webhook tests)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RecordingServer:
    """Accepts any POST, records method, path, headers and body, answers *status*."""

    def __init__(self, status: int = 200, location: str | None = None) -> None:
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "headers": {k: v for k, v in self.headers.items()},
                        "body": self.rfile.read(length),
                    }
                )
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # keep test output quiet
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def bodies(self) -> list[dict]:
        """Request bodies parsed as JSON."""
        return [json.loads(r["body"]) for r in self.requests]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
