"""The "vendor": a fake third-party API we don't own, seen only through its Prometheus
/metrics endpoint. Standard library only.

A mode file switches its behaviour (python lab.py vendor <mode> writes it):
  ok      ~120 ms latency, 0.5 % errors
  slow    ~1.8 s latency
  errors  15 % of calls fail
  down    /metrics answers 503, so Prometheus records up{job="vendor"} = 0
"""

import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("VENDOR_PORT", "9105"))
MODE_FILE = os.environ.get("VENDOR_MODE_FILE", "/var/lib/vendor-sim/mode")
CALLS_PER_SECOND = 20

state = {"requests": 0, "errors": 0, "latency": 0.12}
lock = threading.Lock()


def mode() -> str:
    try:
        with open(MODE_FILE, encoding="utf-8") as f:
            return f.read().strip() or "ok"
    except OSError:
        return "ok"


def traffic() -> None:
    while True:
        m = mode()
        fail = 0.15 if m == "errors" else 0.005
        with lock:
            state["requests"] += CALLS_PER_SECOND
            state["errors"] += sum(random.random() < fail for _ in range(CALLS_PER_SECOND))
            state["latency"] = round((1.8 if m == "slow" else 0.12) * random.uniform(0.8, 1.2), 3)
        time.sleep(1)


class Metrics(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_error(404)
            return
        if mode() == "down":
            self.send_error(503, "vendor down")
            return
        with lock:
            body = (
                "# HELP vendor_api_requests_total Calls made to the vendor API.\n"
                "# TYPE vendor_api_requests_total counter\n"
                f"vendor_api_requests_total {state['requests']}\n"
                "# HELP vendor_api_errors_total Vendor API calls that failed.\n"
                "# TYPE vendor_api_errors_total counter\n"
                f"vendor_api_errors_total {state['errors']}\n"
                "# HELP vendor_api_latency_seconds Latest vendor API response time.\n"
                "# TYPE vendor_api_latency_seconds gauge\n"
                f"vendor_api_latency_seconds {state['latency']}\n"
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    threading.Thread(target=traffic, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Metrics).serve_forever()
