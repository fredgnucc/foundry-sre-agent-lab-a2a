"""Demo web app for the SRE Agent lab.

FAULT_MODE (app setting) simulates an unmanaged config change:
  none   - healthy
  errors - / and /api/orders return HTTP 500
  slow   - / and /api/orders take ~5 s
/health is always healthy so App Service keeps the instance in rotation.
"""
import logging
import os
import random
import time

from flask import Flask, jsonify

if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="demo")

log = logging.getLogger("demo")
log.setLevel(logging.INFO)
app = Flask(__name__)
FAULT_MODE = os.environ.get("FAULT_MODE", "none").lower()


def apply_fault():
    if FAULT_MODE == "errors":
        log.error("Order backend unavailable (FAULT_MODE=errors)")
        return jsonify(error="order backend unavailable"), 500
    if FAULT_MODE == "slow":
        log.warning("Slow dependency (FAULT_MODE=slow)")
        time.sleep(5)
    return None


@app.get("/")
def index():
    return apply_fault() or jsonify(app="sre-webapp-demo", fault_mode=FAULT_MODE)


@app.get("/api/orders")
def orders():
    return apply_fault() or jsonify(orders=[{"id": i, "total": round(random.uniform(5, 500), 2)} for i in range(5)])


@app.get("/health")
def health():
    return jsonify(status="ok")
