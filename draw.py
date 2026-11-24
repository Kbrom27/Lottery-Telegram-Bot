#!/usr/bin/env python3
"""Verifiable draw (commit-reveal). Standard library only, so anyone can re-run it.

  1. BEFORE ticket sales close, commit to a secret seed and publish the commitment hash:
         python draw.py commit
     Keep draw_seed.txt private. Publish the printed commitment (channel post, pinned message).

  2. AFTER sales close, export the ticket list from the bot (/export) and publish
     the public list (/export public). Then choose a public value that nobody could know when
     you committed (e.g. the hash of a specific blockchain block that did not exist yet, or a value
     announced live by an independent party) and run:
         python draw.py draw --tickets tickets_full.csv --entropy "<public value>" --winners 3

  3. Publish the seed. Anyone can check  sha256(seed) == commitment  and re-run the draw with
     the public list (the `holder` column) to get the same winners.

Winner i is picked from the remaining tickets using SHA-256(seed|entropy|i|j) with rejection
sampling (no modulo bias). With --one-prize-per-person, all other tickets of a winner are removed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import secrets
import sys


def commitment(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def rand_below(seed: str, entropy: str, counter: int, n: int) -> int:
    limit = (2**256 // n) * n  # reject digests in the biased tail
    j = 0
    while True:
        d = int.from_bytes(hashlib.sha256(f"{seed}|{entropy}|{counter}|{j}".encode()).digest(), "big")
        if d < limit:
            return d % n
        j += 1


def pick_winners(seed, entropy, tickets, k, one_per_person=False):
    """tickets: list of (number, holder). Returns list of (number, holder)."""
    pool = sorted(tickets)
    winners = []
    for i in range(k):
        if not pool:
            break
        number, holder = pool.pop(rand_below(seed, entropy, i, len(pool)))
        winners.append((number, holder))
        if one_per_person:
            pool = [t for t in pool if t[1] != holder]
    return winners


def load_tickets(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    holder_col = "holder" if rows and "holder" in rows[0] else "telegram_id"
    return [(int(r["number"]), r.get(holder_col, "")) for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("commit")
    c.add_argument("--seed-file", default="draw_seed.txt")
    d = sub.add_parser("draw")
    d.add_argument("--seed-file", default="draw_seed.txt")
    d.add_argument("--tickets", required=True, help="CSV with a `number` column (+ `holder` or `telegram_id`)")
    d.add_argument("--entropy", required=True, help="public value unknowable at commit time")
    d.add_argument("--winners", type=int, default=1)
    d.add_argument("--one-prize-per-person", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "commit":
        if os.path.exists(a.seed_file):
            sys.exit(f"{a.seed_file} exists - refusing to overwrite (that would change your commitment)")
        seed = secrets.token_hex(32)
        fd = os.open(a.seed_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(seed)
        print(f"Seed saved to {a.seed_file} (keep it SECRET until the draw).")
        print(f"PUBLISH this commitment now:\n{commitment(seed)}")
        return

    seed = open(a.seed_file).read().strip()
    tickets = load_tickets(a.tickets)
    winners = pick_winners(seed, a.entropy, tickets, a.winners, a.one_prize_per_person)
    print(f"seed        : {seed}")
    print(f"commitment  : {commitment(seed)}")
    print(f"entropy     : {a.entropy}")
    print(f"tickets     : {len(tickets)}")
    for rank, (number, holder) in enumerate(winners, 1):
        print(f"winner #{rank}: ticket {number}  (holder {holder})")


if __name__ == "__main__":
    main()
