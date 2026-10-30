"""apply_change lock-hold benchmark (arch-review L3, quantified 2026-10-30).

Question: how long does ``apply_change`` (server/vault/orchestration/file_ops.py)
hold the global ``_db_lock``, and how much of that is disk I/O inside the
critical section, at vault scales 300 / 1k / 3k / 10k?

Companion doc: ``2026-10-30-apply-change-lock-hold-benchmark.md`` (this dir).

Run:  ``python 2026-10-30-apply-change-lock-hold-benchmark.py`` (~5 min, 10k build included)
Notes:
- DeprecationWarning on ``server.vault`` import = pre-existing shim, not a bug.
- Results are machine-dependent; the RELATIONS (created flat, deleted/moved
  linear in file count, rglob-dominated) are the point.
"""
import hashlib
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # repo root
sys.path.insert(0, str(REPO))

import server.vault.db.schema as schema
from server.vault.db.schema import init_db
from server.vault.orchestration import file_ops
from server.vault.orchestration.indexer import rebuild_segments_all, graph_rebuild_all
from server.vault.orchestration.refs import update_references_batch
from server.vault.fs.link_parser import _clear_wikilink_cache
import server.vault.fs.link_parser as lp

WORK = Path(tempfile.gettempdir()) / "l3bench"

# ── recording lock: patch the name file_ops bound at import time (file_ops.py:19) ──
class RecordingLock:
    def __init__(self, inner):
        self._inner = inner
        self._t0 = 0.0
        self.holds = []
    def acquire(self, blocking=True, timeout=-1):
        self._t0 = time.perf_counter()
        return self._inner.acquire(blocking, timeout)
    def release(self):
        self.holds.append((time.perf_counter() - self._t0) * 1000)
        self._inner.release()
    def __enter__(self):
        self.acquire()
        return self
    def __exit__(self, *a):
        self.release()

rec_lock = RecordingLock(schema._db_lock)
file_ops._db_lock = rec_lock

def build_vault(vault: Path, n: int, seed=42) -> list[str]:
    for d1 in ("a", "b", "c"):
        for d2 in ("x", "y", "z"):
            (vault / d1 / d2).mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    rels = []
    for i in range(n):
        rel = f"{rng.choice(['a','b','c'])}/{rng.choice(['x','y','z'])}/f{i:05d}.md"
        rels.append(rel)
    for i, rel in enumerate(rels):
        links = "\n".join(f"[[f{rng.randrange(n):05d}]]" for _ in range(4))
        body = f"# doc {i}\n\nSome text about file {i}. " * 40 + links + "\n\n#tagA #tagB\n"
        (vault / rel).write_text(body)
    return rels

def pctl(xs, q):
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)

def med(xs):
    return statistics.median(xs)

def timed_calls(fn, iters=20):
    """Run fn() iters times; return (lock_holds_ms, total_ms)."""
    holds, totals = [], []
    for _ in range(iters):
        rec_lock.holds = []
        t0 = time.perf_counter()
        fn()
        totals.append((time.perf_counter() - t0) * 1000)
        holds.extend(rec_lock.holds)
    return holds, totals

def component_ms(fn, iters=10):
    xs = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1000)
    return med(xs)

def main():
    IT = 20
    rows = []
    for n in (300, 1000, 3000, 10000):
        work = WORK / f"v{n}"
        if work.exists():
            shutil.rmtree(work)
        vault = work / "vault"
        t0 = time.time()
        rels = build_vault(vault, n)
        conn = init_db(work / "index.db")
        # steady state: segments + graph populated
        rebuild_segments_all(conn, vault)
        # warm the wikilink filename cache like a running server would have
        _clear_wikilink_cache()
        lp._wikilink_cache = {Path(r).name: [r] for r in rels}
        graph_rebuild_all(conn, vault)
        build_s = time.time() - t0
        print(f"== scale {n}  (build+index {build_s:.1f}s) ==", flush=True)
        (vault / "tmp").mkdir(exist_ok=True)
        (vault / "moved").mkdir(exist_ok=True)

        def make_case(kind, idx):
            if kind == "created_h":
                rel = f"tmp/cn{idx}.md"
                (vault / rel).write_text(f"# bench {n}-{idx}\n\nbody text here.\n\n[[f{idx % n:05d}]]\n")
                h = hashlib.sha256((vault / rel).read_bytes()).hexdigest()
                return lambda: file_ops.apply_change(conn, vault, rel, "created",
                                                     source="bench", new_hash=h)
            if kind == "created_nh":
                rel = f"tmp/cnh{idx}.md"
                (vault / rel).write_text(f"# bench nh {n}-{idx}\n\nbody text here.\n\n[[f{idx % n:05d}]]\n")
                return lambda: file_ops.apply_change(conn, vault, rel, "created", source="bench")
            if kind == "deleted":
                rel = rels[(idx * 37) % n]
                return lambda: file_ops.apply_change(conn, vault, rel, "deleted", source="bench")
            if kind == "moved":
                src = rels[(idx * 53) % n]
                dst = f"moved/g{idx:05d}.md"  # different stem -> referrer rewrite path
                (vault / dst).parent.mkdir(parents=True, exist_ok=True)
                os.rename(vault / src, vault / dst)
                h = hashlib.sha256((vault / dst).read_bytes()).hexdigest()
                return lambda: file_ops.apply_change(conn, vault, dst, "moved",
                                                     source="bench", from_path=src, new_hash=h)

        for kind in ("created_h", "created_nh", "deleted", "moved"):
            fns = [make_case(kind, i) for i in range(IT)]
            holds, totals = zip(*(timed_calls(f) for f in fns))
            holds, totals = sum(holds, []), sum(totals, [])
            line = (f"  n={n:6d} {kind:11s}  lock-hold med {med(holds):8.2f}ms  p95 {pctl(holds,.95):8.2f}ms  max {max(holds):8.2f}ms   call med {med(totals):8.2f}ms")
            print(line, flush=True)
            rows.append((n, kind, med(holds), pctl(holds, .95), max(holds), med(totals)))
        print(flush=True)

    # ── component breakdown at 3000 and 10000 ──
    for n in (3000, 10000):
        work = WORK / f"v{n}"
        vault = work / "vault"
        conn = init_db(work / "index.db")
        rels = [str(p.relative_to(vault)) for p in vault.rglob("f*.md") if p.is_file()]
        hub = rels[100]
        print(f"== breakdown n={n} ==", flush=True)
        name = Path(hub).name
        from server.vault.orchestration.indexer import graph_extract_file, index_segments
        print(f"  rglob(1 filename)        {component_ms(lambda: list(vault.rglob(name))):8.2f} ms", flush=True)
        blob = (vault / hub).read_bytes()
        print(f"  read+sha256(1 file)      {component_ms(lambda: hashlib.sha256(blob).hexdigest()):8.2f} ms", flush=True)
        print(f"  index_segments(1)        {component_ms(lambda: index_segments(conn, vault, hub)):8.2f} ms", flush=True)
        print(f"  graph_extract(warm)      {component_ms(lambda: graph_extract_file(conn, vault, hub)):8.2f} ms", flush=True)
        def _cold_extract():
            _clear_wikilink_cache()
            graph_extract_file(conn, vault, hub)
        cold = component_ms(_cold_extract)
        lp._wikilink_cache = {Path(r).name: [r] for r in rels}
        print(f"  graph_extract(cold cache){cold:8.2f} ms   (cold = 4 fresh filenames -> 4 rglobs)", flush=True)
        referrer = None
        stem = Path(hub).stem
        for r in rels:
            if f"[[{stem}]]" in (vault / r).read_text():
                referrer = r
                break
        if referrer:
            rp = vault / referrer
            orig = rp.read_text()
            moved = f"moved/g-hub-{n}.md"  # different stem -> short-name rewrite fires
            (vault / moved).parent.mkdir(parents=True, exist_ok=True)
            xs = []
            for _ in range(10):
                os.rename(vault / hub, vault / moved)
                t0 = time.perf_counter()
                update_references_batch(conn, vault, [(hub, moved)])
                xs.append((time.perf_counter() - t0) * 1000)
                os.rename(vault / moved, vault / hub)
            (vault / referrer).write_text(orig)
            print(f"  update_refs(1 move,~4 referrers){med(xs):8.2f} ms/iter", flush=True)
        print(flush=True)

    print("== SUMMARY (lock-hold ms) ==")
    print(f"{'scale':>7} {'action':>12} {'med':>9} {'p95':>9} {'max':>9} {'call-med':>9}")
    for (n, kind, m, p, mx, c) in rows:
        print(f"{n:>7} {kind:>12} {m:>9.2f} {p:>9.2f} {mx:>9.2f} {c:>9.2f}")

if __name__ == "__main__":
    main()
