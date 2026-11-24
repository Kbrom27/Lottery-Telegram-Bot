import csv
import subprocess
import sys
from collections import Counter

import draw


def test_deterministic_and_verifiable():
    tickets = [(n, f"h{n % 7}") for n in range(1, 101)]
    a = draw.pick_winners("seed", "entropy", tickets, 5)
    b = draw.pick_winners("seed", "entropy", list(reversed(tickets)), 5)  # input order must not matter
    assert a == b and len(set(a)) == 5
    assert a != draw.pick_winners("seed2", "entropy", tickets, 5)
    import hashlib

    assert draw.commitment("seed") == hashlib.sha256(b"seed").hexdigest()


def test_one_prize_per_person():
    tickets = [(n, "A" if n <= 50 else f"B{n}") for n in range(1, 101)]
    for i in range(30):
        w = draw.pick_winners(f"s{i}", "e", tickets, 10, one_per_person=True)
        holders = [h for _, h in w]
        assert len(holders) == len(set(holders))


def test_roughly_uniform():
    tickets = [(n, str(n)) for n in range(1, 11)]
    hits = Counter(draw.pick_winners(f"seed{i}", "e", tickets, 1)[0][0] for i in range(5000))
    assert set(hits) == set(range(1, 11))
    assert all(400 < c < 600 for c in hits.values()), hits  # expected 500 each


def test_cli_commit_then_draw(tmp_path):
    seed_file = tmp_path / "seed.txt"
    csv_file = tmp_path / "t.csv"
    with open(csv_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["number", "holder"])
        for n in range(1, 21):
            w.writerow([n, f"h{n}"])
    run = lambda *a: subprocess.run([sys.executable, "draw.py", *a], capture_output=True, text=True)  # noqa: E731
    c = run("commit", "--seed-file", str(seed_file))
    assert c.returncode == 0 and len(c.stdout.strip().splitlines()[-1]) == 64
    assert run("commit", "--seed-file", str(seed_file)).returncode != 0  # never overwrite a commitment
    d = run("draw", "--seed-file", str(seed_file), "--tickets", str(csv_file), "--entropy", "x", "--winners", "2")
    assert d.returncode == 0 and "winner #1" in d.stdout and "winner #2" in d.stdout
