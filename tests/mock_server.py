"""Serve mock_arena.py on 127.0.0.1:<port> with compressed phases and optional faults.
SYNTHETIC, for crash/restart testing only: its pool and scoring are made up.

    python tests/mock_server.py --port 18701 --recon 40 --market 45 --closing 12 [--faults]

--faults: /ledger has no phase key, `claimed` is the string "false"/"true", and ~3% of
metered responses are malformed JSON (sent before the request is executed, so not charged).
"""
import argparse, os, random, sys, threading, time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mock_arena as M  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--wait", type=float, default=3)
    ap.add_argument("--recon", type=float, default=40)
    ap.add_argument("--market", type=float, default=45)
    ap.add_argument("--closing", type=float, default=12)
    ap.add_argument("--faults", action="store_true")
    a = ap.parse_args()

    M.PHASES = [("closed", a.wait), ("recon", a.recon), ("market", a.market),
                ("closing", a.closing), ("closed_final", 10 ** 9)]
    M.T0 = time.time()
    M.make_pool()

    class Faulty(M.H):
        def send(self, code, obj):
            if isinstance(obj, dict) and self.path.startswith("/ledger"):
                obj = {k: v for k, v in obj.items() if k != "phase"}
            super().send(code, obj)

        def public(self, cid, full):
            p = super().public(cid, full)
            if "claimed" in p:
                p["claimed"] = str(p["claimed"]).lower()
            return p

        def route(self, method):
            if not self.path.startswith(("/ledger", "/requisitions")) and random.random() < 0.03:
                body = b'{"results": [ {"candidate_id": '
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().route(method)

    handler = Faulty if a.faults else M.H
    threading.Thread(target=M.rivals, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), handler)
    print(f"mock listening on 127.0.0.1:{a.port} faults={a.faults}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
