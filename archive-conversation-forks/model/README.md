# Empirical model + fuzz harness for the Step 2 set algebra

This directory is the **empirical side** of the verification. It contains executable Python
re-implementations of the `../SKILL.md` Step 2 set algebra and property-based / adversarial fuzz
harnesses that check the safety properties hold across random and hand-built stores.

It **complements** the Lean proof in `../proofs/`, it does not replace it:

- `../proofs/` (Lean) certifies the **abstract rules**: given the set-algebra definitions, the safety
  properties hold for *all* stores and *all* operator judge policies, machine-checked.
- this directory (Python) is a second, *independent* re-implementation of the same algebra, fuzzed
  against ~millions of concrete stores. It is the cross-check that the abstract rules the proof
  reasons about actually behave as claimed on real inputs, and it is how the round-5 / round-6 bugs
  were originally found.

## What it does and does not establish

These models are faithful Python re-implementations of the Step 2 set algebra: the post-parse fields
(`fingerprints`, `fingerprints_prose`, `lref`, `owned`, boundaries, `live`) are generated directly, then the exact
pipeline is run (`seed` -> `locked_closure` -> per-tree judge -> `KEPT` -> re-close -> `loadbearing`
-> C5 demotion -> C6 deferred archive -> recall pass -> marker loop), and the safety properties are
checked on the result. The operator `judge`, the recall `SONNET_CONFIRM`, and the marker
substantive-read are modelled as **adversarial oracles** (they may return any decision), so the fuzz
tests "for all judge policies" the same way the Lean theorems quantify over them.

What this **establishes**: the set algebra, as specified, upholds the safety properties across a very
large random/adversarial sample (and on the specific structural traps SKILL.md cites).

What this **cannot** establish: that the *real skill code* (the actual Python an operator runs)
matches this model. The model regenerates the post-parse fields directly; it does NOT parse JSONL,
drive the real mutable union-find, compute the real `canonical()` key, or execute the live registry
read. That "does the real code match this model" gap is exactly the model-to-implementation boundary
the Lean proof also leaves open (see `../proofs/README.md`). Model + fuzz + proof together are
stronger than any one alone, but none of them inspects the shipping code's I/O.

All scripts are **stdlib-only `python3`** (`random`, `collections`, `itertools`, `os`, `sys`); no
third-party packages, no network, synthetic/abstract stores only (files are `F0`, `C`, `FORK`, ...;
uuids `u0`; lpus `L0`, `P` - no real session UUIDs or paths).

## Files

Two independent models (cross-checking each other):

- `step2_model.py` - the independent full-pipeline model + a random-store fuzz
  harness with the four safety-property checks. The most complete model.
- `targeted.py` - an adversarial harness on top of `step2_model.py` that hand-builds the structural
  traps SKILL.md cites (the C4 re-close orphan scenario, multiple sources per phantom, cross-file
  chains seeded only from a kept-unique fork) and biases the judge oracle toward archiving sources,
  to stress the no-orphan property.
- `trace.py` - a model of the **pre-fix** (round-5) algebra. Used to reproduce the original bugs.
- `trace_patched.py` - a model of the **post-fix** algebra. Same shape as `trace.py`, with the C4
  re-close / C5 demotion / marker fixes, so the two side by side show a bug reproduce then disappear.

Curated finding-probes (each documents one round-5 / round-6 finding; small standalone traces):

- `finding_f1_phantom_orphan.py` - the main bug: a kept-unique fork needs a phantom whose sole
  source is content-redundant, and the pre-fix closure never locks that source (it is reached only
  from canonicals/live, not kept-unique forks), so the source is archivable and the fork orphaned.
- `finding_f1_fix_confirmed.py` - isolates the cause and confirms the re-close over the full kept set
  fixes it (the source becomes locked, hence kept).
- `finding_f2_halt_swallow.py` - the HALT-swallow bug: a `try/except` swallowed the stop-and-ask when
  a live session could not be resolved to a file, silently dropping it from the live set.
- `finding_marker_no_hole.py` - a 20k-store fuzz confirming the marker partition has no reachable
  `~0-residue AND NOT load-bearing` cell (the branch deleted from the decision tree is unreachable).

## How to run

Each script runs standalone with stock `python3` from this directory:

```
python3 step2_model.py [seed] [trials]      # default seed 12345, 200000 trials
python3 targeted.py [seed] [trials]         # default seed 5, 50000 trials
python3 finding_marker_no_hole.py           # fixed 20000-store sweep
python3 finding_f1_phantom_orphan.py        # single documented trace
python3 finding_f1_fix_confirmed.py
python3 finding_f2_halt_swallow.py
python3 trace.py                            # imported as a module by the finding probes
python3 trace_patched.py
```

`step2_model.py` and `targeted.py` take optional `seed` and `trials` arguments; the defaults run the
large sweeps. The finding probes are deterministic single traces (or a fixed fuzz seed).

## Reading the fuzz output

`step2_model.py` checks four core properties, tagged `[i]` (no-orphan), `[ii]` (recall content
no-loss), `[iii]` (live never archived), `[iv]` / `[iv-hole]` (marker exhaustive, no deleted-cell
reached). On a clean run these report **zero** violations.

It ALSO reports a `[c6-content]` count, and that count is normally **nonzero - this is expected and
not a bug.** The C6 path archives forks the operator judged worthless; those are NOT required to be
0-unique (unlike the recall-0 path), so a C6-archived fork may carry a message found in no kept file.
`step2_model.py` deliberately tags and counts that as `c6-content` (a labelled observation, not a
core violation) precisely to keep it visible rather than hidden. It is the empirical counterpart of the
content-safety scope caveat in `../proofs/README.md`: machine-checked content-safety covers the
0-unique recall path; the judged C6 / nonzero-recall paths rest on the operator read, by design. A
run is "clean" when the core tags `[i] [ii] [iii] [iv] [iv-hole]` are all zero; `[c6-content]` being
nonzero is the model correctly surfacing the judged path, not a failure.
