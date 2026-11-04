"""Mock submission API for the gate. Binds to 127.0.0.1 only; there is no route that edits or deletes a ledger entry.

GET  /reports                      list report ids
GET  /reports/<id>                 premise state, unresolved findings
POST /reports/<id>/attach          insert broken premises into the report
POST /reports/<id>/resolve         {"finding", "action", "reason", "actor"}
POST /reports/<id>/submit          {"actor"} -> 200 receipt, or 409 with unresolved findings
GET  /ledger/verify                chain check and head hash
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gate.service import Gate, GateError

HOST = "127.0.0.1"


def make_handler(gate):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _parts(self):
            return [p for p in self.path.split("?")[0].split("/") if p]

        def do_GET(self):
            p = self._parts()
            try:
                if p == ["reports"]:
                    return self._send(200, sorted(gate.reports))
                if len(p) == 2 and p[0] == "reports":
                    return self._send(200, gate.state(p[1]))
                if p == ["ledger", "verify"]:
                    ok, bad = gate.ledger.verify()
                    return self._send(200, {"ok": ok, "first_bad_seq": bad, **gate.ledger.head()})
            except GateError as e:
                return self._send(404, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            p = self._parts()
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            if len(p) != 3 or p[0] != "reports":
                return self._send(404, {"error": "not found"})
            rid, op = p[1], p[2]
            try:
                if op == "attach":
                    return self._send(200, gate.attach(rid))
                if op == "resolve":
                    return self._send(200, gate.resolve(rid, body.get("finding"), body.get("action"), body.get("reason"), body.get("actor")))
                if op == "submit":
                    ok, entry = gate.submit(rid, body.get("actor"))
                    return self._send(200 if ok else 409, entry)
            except GateError as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def _refuse(self):
            self._send(405, {"error": "ledger is append-only"})

        do_PUT = do_DELETE = do_PATCH = _refuse

        def log_message(self, *args):
            pass

    return Handler


def serve(ledger_path, port=0):
    httpd = ThreadingHTTPServer((HOST, port), make_handler(Gate(ledger_path)))
    return httpd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    httpd = serve(a.ledger, a.port)
    print(f"gate API on http://{HOST}:{httpd.server_port}")
    httpd.serve_forever()
