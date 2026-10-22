"""Steady demo traffic: the shop's customers.

Sends checkouts to the gateway at a fixed rate, about 1 in 20 with a card that is declined
(a normal business answer, not an error). Without traffic there is nothing for metrics and
alerts to look at, and silence would look like an outage.

The rate stays fixed even when the shop slows down: at most MAX_IN_FLIGHT checkouts wait at
once, and a tick that finds them all busy is skipped and counted, rather than piling up an
ever-growing queue that would make the demo app look worse than the fault does.

    python scripts/traffic.py --gateway http://localhost:8080 --rps 2
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from collections import Counter

import httpx

MAX_IN_FLIGHT = 20
REPORT_EVERY_S = 30
CARTS = [
    [{"sku": "tea", "qty": 1}],
    [{"sku": "tea", "qty": 2}, {"sku": "mug", "qty": 1}],
    [{"sku": "beans", "qty": 1}],
    [{"sku": "mug", "qty": 1}, {"sku": "beans", "qty": 1}],
]


def checkout_body(rng: random.Random) -> dict[str, object]:
    body: dict[str, object] = {"items": rng.choice(CARTS)}
    if rng.random() < 0.05:
        body["card_token"] = "tok_demo_declined"  # noqa: S105  # a demo constant
    return body


async def run(gateway: str, rps: float, seconds: float | None) -> Counter[str]:
    rng = random.Random()  # noqa: S311  # traffic mix, not security
    outcomes: Counter[str] = Counter()
    slots = asyncio.Semaphore(MAX_IN_FLIGHT)
    tasks: set[asyncio.Task[None]] = set()

    async def one(client: httpx.AsyncClient) -> None:
        try:
            r = await client.post("/checkout", json=checkout_body(rng))
            outcomes[str(r.status_code)] += 1
        except httpx.HTTPError as exc:
            outcomes[type(exc).__name__] += 1
        finally:
            slots.release()

    async with httpx.AsyncClient(base_url=gateway, timeout=15) as client:
        start = last_report = time.monotonic()
        tick = 0
        while seconds is None or time.monotonic() - start < seconds:
            tick += 1
            if slots.locked():
                outcomes["skipped (too many waiting)"] += 1
            else:
                await slots.acquire()
                task = asyncio.create_task(one(client))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            # Sleep to the next tick of a fixed schedule, so slow ticks don't drift the rate.
            await asyncio.sleep(max(0.0, start + tick / rps - time.monotonic()))
            if time.monotonic() - last_report >= REPORT_EVERY_S:
                last_report = time.monotonic()
                print("traffic:", dict(sorted(outcomes.items())), flush=True)
        await asyncio.gather(*tasks)
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway", default="http://localhost:8080")
    parser.add_argument("--rps", type=float, default=2.0)
    parser.add_argument("--seconds", type=float, default=None, help="default: run until stopped")
    args = parser.parse_args()
    if not 0 < args.rps <= 50:
        parser.error("--rps must be between 0 and 50")
    outcomes = asyncio.run(run(args.gateway, args.rps, args.seconds))
    print("traffic done:", dict(sorted(outcomes.items())))


if __name__ == "__main__":
    main()
