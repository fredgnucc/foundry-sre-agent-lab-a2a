"""Resource providers: register what the lab needs on the subscription, before any other layer.
Registration is a subscription setting (free, shared with other workloads), so `down` leaves it alone."""

from __future__ import annotations

import time

from labkit import arm
from labkit.config import Config, Names

API = "2021-04-01"


def _state(c: arm.Client, ns: str) -> str:
    p = arm.get(c, f"/subscriptions/{c.subscription}/providers/{ns}", API)
    return (p or {}).get("registrationState", "NotFound")


def up(c: arm.Client, cfg: Config, n: Names, timeout: int = 900) -> None:
    print("== providers")
    pending = [ns for ns in cfg.providers if _state(c, ns) != "Registered"]
    if not pending:
        print(f"  all {len(cfg.providers)} registered")
        return
    for ns in pending:
        arm.call(c, "POST", f"/subscriptions/{c.subscription}/providers/{ns}/register", API)
    if c.dry_run:
        return
    end = time.time() + timeout
    while pending:
        if time.time() > end:
            raise arm.ArmError(f"providers still registering after {timeout}s: {', '.join(pending)}")
        time.sleep(10)
        pending = [ns for ns in pending if _state(c, ns) != "Registered"]
        print(f"  registering: {', '.join(pending) or 'done'}")
