# Directory-Scan Benchmark: `rglob` vs `os.walk` for `GET /api/docs`

**Date:** 2026-10-16
**Question:** Which directory-scan implementation should ship for the `GET /api/docs`
endpoint — `Path.rglob("*")` with post-hoc hidden filtering (Approach A), or
`os.walk` with in-place pruning of hidden dirs (Approach B)?

Both implementations return the list of non-hidden directories under a vault root
(relative, forward-slash paths), excluding any path whose segment starts with `.`.

---

## Methodology

### Platform

- **OS:** Windows 11 (build 26200, NTFS), vaults on the `C:` drive
- **Python:** 3.11.7 (Anaconda, MSC v.1916 64-bit)
- **Machine:** single developer workstation; no concurrent load controlled for

### Correctness gate (run first)

A small hand-built vault with nested dirs (`a/b/c`), an empty dir, hidden dirs
(`.sync/`, `.obsidian/plugins/`, `hidden/.x/`), and a non-hidden dir `foo/`
containing only `.git/`. **Result: PASS** — both approaches returned the exact
same list: `['a', 'a/b', 'a/b/c', 'empty', 'foo', 'hidden']`. All performance
work below assumes output equivalence.

### Synthetic vaults

Three vaults generated under a temp dir (generation times: **1.17 s / 2.69 s /
8.44 s**):

| Component | Layout |
|---|---|
| Visible tree | 10 top dirs (`d00`–`d09`) × 5 subdirs (`s0`–`s4`) = **50 leaf dirs** |
| `.md` files | content `b"# x\n"` via `write_bytes`, distributed round-robin across leaves: **20 / 100 / 400 per leaf** |
| Hidden tree (constant) | `.obsidian/plugins/pluginNN/{main.js, manifest.json, data/98 files}` × 20 plugins = **exactly 2,000 files**, 42 hidden dirs |

Totals per vault: **3,000 / 7,000 / 22,000 files**, **102 dirs** each
(60 visible + 42 hidden). The `.obsidian` subtree simulates Obsidian
config/plugins and is held constant so the hidden-tree overhead is isolated.

### Timing method

- `time.perf_counter()` around the scan only (no JSON/ETag — shared code).
- **2 warmup runs, then median of 10 timed runs**, per approach, per vault.
- Approaches measured sequentially (A then B) within each vault; a second,
  independent measurement pass with the order swapped (B then A) confirmed
  stability — see Results.

### Entries-visited (IO-work proxy)

Counted in **separate measurement passes** (not part of the timed runs):

- **Approach A:** `sum(1 for _ in vault.rglob("*"))` — every entry rglob
  enumerates, including everything inside hidden dirs/files.
- **Approach B:** `sum(len(dirnames) + len(filenames))` across all visited
  roots, **after** in-place pruning — only what os.walk actually touches.

### Caveats

- **Page cache warm:** all timed runs follow warmups and prior full
  enumerations; cold-cache (first-ever read) costs are not measured. For a
  long-running server this is the realistic steady state.
- **Measurement noise:** Approach A at 20k varied 939–1,110 ms across two
  independent runs (~15%); B at 20k varied 51–68 ms. The A/B ratio is robust
  to this noise (7.5–18× across all runs).
- **Uniform structure:** all three vaults share the same 102-dir skeleton;
  only file counts differ. Real vaults vary in shape (see the filename-length
  finding below, which this design initially masked).
- Single machine, single drive; Windows Defender/indexer state not controlled.

---

## Results

Primary numbers: median of 10 after 2 warmups (first measurement pass).

| Vault size (.md files) | A: `rglob` wall-clock (ms) | B: `os.walk` wall-clock (ms) | A/B time | A: entries visited | B: entries visited | A/B entries |
|---|---|---|---|---|---|---|
| 1,000 (+2,000 hidden) | **136.6** | **8.7** | 15.7× | 3,102 | 1,060 | 2.93× |
| 5,000 (+2,000 hidden) | **363.2** | **48.5** | 7.5× | 7,102 | 5,060 | 1.40× |
| 20,000 (+2,000 hidden) | **938.8** | **50.9** | 18.4× | 22,102 | 20,060 | 1.10× |

Stability check (second independent pass, order swapped): A = 154.5 / 384.8–
394.4 / 1,093.6–1,109.7 ms; B = 9.0–9.4 / 46.4–46.7 / 51–68 ms (20k settles
~50 ms). Same picture, same ratios.

Entry-count sanity check: A enumerates all files + dirs (3,000+102 = 3,102 ✓);
B touches only the 60 visible dirs + files inside them (1,000+60 = 1,060 ✓).

---

## Key findings

### 1. `os.walk` pruning is 5–18× faster in wall-clock, at every size

The gap is far outside noise (tight per-run distributions, reproducible across
two independent passes and both measurement orders). At realistic personal-vault
scale (5k–20k files + a 2,000-file `.obsidian`), B takes **~50–70 ms** where A
takes **~0.4–1.1 s**. For an API endpoint that runs on every request, that is
the difference between a fast response and a perceptibly slow one.

### 2. Why the wall-clock gap exceeds the IO-work ratio

- **A pays ~45–52 µs of Python overhead per entry** (Path object construction +
  `is_dir()` stat + `relative_to`/parts check), so its time scales linearly
  with *total* entries: 136.6 ms / 3,102 = 44 µs; 363.2 / 7,102 = 51 µs;
  938.8 / 22,102 = 42 µs.
- **B's per-entry Python cost is ~zero**: files are never touched in Python at
  all (only dir names, for the 60 visible dirs). Its time is dominated by
  kernel directory enumeration, which is nearly flat across these vault sizes
  (same 62 visited dirs; 8.7 → 48.5 → 50.9 ms as files go 1k → 5k → 20k).
- The IO-work ratio (A/B entries) is **2.93× / 1.40× / 1.10×**: the constant
  2,000-file hidden tree is ~66% of A's work at 1k scale but only ~10% at 20k.
  Pruning's *relative* IO advantage shrinks as the vault grows — but its
  wall-clock advantage does not, because A's per-entry Python cost keeps
  scaling.

### 3. A reproducible Windows/NTFS quirk: filename length (8.3 short names)

Control experiments (fresh vaults, identical structure, one factor varied)
found that B's absolute time depends strongly on note filename length:

| Naming (100 files/leaf, 5k scale) | B wall-clock | Per-leaf `scandir` |
|---|---|---|
| `note0000.md` (8-char stem, fits 8.3) | **8.1 ms** | 0.08 ms |
| `note00000.md` (9-char stem, no 8.3) | **40.2 ms** | 0.66 ms |

`dir /x` shows the mechanism: `note00000.md` carries an 8.3 short name
(`NOTE00~1.MD`) while `note0000.md` does not. Files whose names don't fit 8.3
get an extra short-name index entry, and the kernel reads/filters those during
enumeration — an 8× per-directory cost. **This is a kernel-level cost that hits
both approaches equally** (A on the same control vaults: 202.6 ms vs 270.1 ms),
so it does not change the A/B comparison — but it means B's *absolute* time
depends on vault naming. Human-titled Obsidian notes ("Meeting Notes 2026-10.md")
almost always exceed 8.3, so the ~40–50 ms figure (not the 8 ms one) is the
realistic expectation for B at 5k scale.

### 4. What pruning actually saves in IO

Descending into the hidden tree costs A: 2,000 file entries + 42 dir entries
per scan, every request. Measured cost of that descent (unpruned vs pruned
`os.walk` on the same vault): only **~4–6 ms** — the kernel enumeration of
2,000 tiny files is cheap. The expensive part of A is not the hidden tree
itself; it is the per-entry Python overhead across *all* 22k entries.

---

## Recommendation

**Ship Approach B (`os.walk` with in-place pruning).**

1. **5–18× faster** at every tested size, with the difference far beyond
   measurement noise. At 20k files: ~51 ms vs ~939–1,110 ms.
2. **Strictly less IO work** (1.1–2.9× fewer entries touched), and it never
   descends into hidden trees — future-proof against larger `.git`/`.obsidian`
   growth, which would only widen A's gap.
3. **Output-identical** to the specified `rglob` approach (correctness gate
   passed on nested/empty/hidden cases), so this is a pure performance swap.
4. The one caveat — B's absolute time depends on filename length via the NTFS
   8.3 short-name path — affects any directory enumeration on Windows (including
   `rglob`), is bounded (~50 ms at 20k scale in our tests), and does not change
   the relative verdict.

If the endpoint is ever called at very high frequency, consider caching the
result (the existing ETag/JSON layer) — but between these two implementations,
there is no contest at personal-vault scale.

---

## Appendix: control experiments

All under the temp bench dir (since deleted); scripts were throwaway.

| Experiment | Result |
|---|---|
| Gate vault (nested/empty/hidden/`.git`) | A == B exactly: `['a','a/b','a/b/c','empty','foo','hidden']` |
| Fresh 5k vault, no `.obsidian` | B = 7.8 ms (vs 44–48 ms for the original 5k vault) |
| Fresh 5k vault + `.obsidian` (2,000 files) | B = 7.9 ms → hidden tree's *presence* is not the cost; naming is |
| Copy of original `vault_5000` to fresh location | B = 45.1 ms → slowness is intrinsic to content, not on-disk state |
| 8-char vs 9-char stem names (100/leaf) | B = 8.1 ms vs 40.2 ms; per-leaf `scandir` 0.08 vs 0.66 ms; `dir /x` shows short name only for 9-char stem |
| A (`rglob`) on same 8/9-char control vaults | 202.6 ms vs 270.1 ms → kernel cost hits both approaches |
