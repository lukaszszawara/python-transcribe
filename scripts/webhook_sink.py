"""Prosty odbiornik webhooków do testów lokalnych.

Uruchomienie: python -m scripts.webhook_sink [port]
Każde otrzymane zdarzenie wypisuje się na stdout i dopisuje do logs/webhook_received.jsonl.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import settings

RECEIVED_LOG = Path(settings.webhook_log_path).parent / "webhook_received.jsonl"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw.decode("utf-8", "replace")}

        RECEIVED_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "path": self.path,
            "event": self.headers.get("X-Webhook-Event"),
            "job_id": self.headers.get("X-Job-Id"),
            "signature": self.headers.get("X-Webhook-Signature"),
            "payload": payload,
        }
        with open(RECEIVED_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        status = payload.get("status", "unknown")
        print(f"[webhook] {self.path} job={record['job_id']} status={status} "
              f"words={payload.get('word_count')} vtt={len(payload.get('vtt_content') or '')} B",
              flush=True)

        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


def main(port: int = 8977) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"odbiornik webhooków nasłuchuje na http://0.0.0.0:{port}/webhook")
    print(f"zapis: {RECEIVED_LOG}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nzatrzymano")
    finally:
        server.server_close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8977)
