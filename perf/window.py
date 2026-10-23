"""Tells bench.py when the measured part of a Locust run began and ended.

Every workload imports this. Locust starts its run timer only after the workload module is
imported, and some workloads spend seconds building request bodies at import. Without these
marks, bench.py would average ServScope's CPU over that idle time too and report it lower
than it really was under load.

bench.py sets PERF_WINDOW_FILE; this module writes "start <unix time>" and "stop <unix time>"
lines to it. Run without bench.py (no variable), it does nothing.
"""

from __future__ import annotations

import os
import time
from typing import Any

from locust import events

PATH = os.environ.get("PERF_WINDOW_FILE")


def _mark(what: str) -> None:
    if PATH:
        with open(PATH, "a") as f:
            f.write(f"{what} {time.time()}\n")


@events.test_start.add_listener
def _started(**_: Any) -> None:
    _mark("start")


@events.test_stop.add_listener
def _stopped(**_: Any) -> None:
    _mark("stop")
