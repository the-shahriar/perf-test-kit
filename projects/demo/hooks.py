"""Domain hooks for the demo orders API. Every function is optional; see docs/HOOKS.md."""

from __future__ import annotations

import random

STATUSES = ["Open", "In Progress", "Shipped", "Delivered", "Cancelled"]
FIRST = ["Ahmed", "Fatima", "Omar", "Layla", "Yousef", "Mariam", "Khalid", "Noura", "Hassan", "Sara"]
LAST = ["Haddad", "Mansour", "Khoury", "Nasser", "Saleh", "Rahman", "Aziz", "Hamdan", "Qasim", "Najjar"]


def order(n: int, seed: int) -> dict:
    """Record n always gets the same values, so every run seeds identical data."""
    r = random.Random(seed * 1_000_003 + n)
    return {"ref": f"DEMO-{n:07d}", "customer": f"{r.choice(FIRST)} {r.choice(LAST)}",
            "status": r.choice(STATUSES), "amount": round(r.uniform(5, 25000), 2), "quantity": r.randint(1, 500)}


def record_count(ctx, dataset):
    return ctx.admin().get("/orders/count")["count"]


def seed_to(ctx, target, dataset):
    api = ctx.admin()
    start = record_count(ctx, dataset)
    n, batches = start, 0
    while n < target:
        size = min(1000, target - n)
        api.post("/orders/bulk", {"items": [order(n + i + 1, ctx.seed) for i in range(size)]})
        n += size
        batches += 1
    return {"start_count": start, "added": n - start, "batches": batches}


def record_ids(ctx, dataset, limit):
    return [o["id"] for o in ctx.admin().get("/orders", params={"limit": limit, "fields": "id"})["items"]]


def after_run(ctx, dataset):
    """Remove what the run created (ref K6-*) so the next run starts at the same size."""
    return ctx.admin().post("/orders/purge", {"ref_prefix": "K6-"})["deleted"]


def cleanup(ctx):
    api = ctx.admin()
    return {"deleted": api.post("/orders/purge", {"ref_prefix": "K6-"})["deleted"]
            + api.post("/orders/purge", {"ref_prefix": "DEMO-"})["deleted"]}


def describe(ctx):
    return {"dataset": "orders held in memory by demo_api.py", "record_shape": list(order(1, ctx.seed))}
