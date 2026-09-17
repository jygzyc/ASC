# AGENTS.md

Working agreements for this repository (the Rust `rasc` port of ASC and its DEX
decompiler).

## Testing policy — binding

1. **Test behaviour, not functions.** A test asserts what the tool *does*: a command's
   exit status, its output, the bytes it writes to a file, the time/space it takes. Tests
   that call a private helper to check an intermediate value are not acceptance tests;
   do not add them.
2. **No mocks, no fakes, no stubs.** Nothing is monkeypatched, intercepted, faked or
   injected. Every test drives the real binary (`target/release/rasc`) as a subprocess
   over real files on disk, or calls the library's public API with real bytes.
3. **Real data only.** Corpora are real Android archives (APKs / framework JARs from
   actual devices). Hand-written or synthesised DEX is not acceptable input for an
   acceptance scenario; if a shape needs pinning, capture it from a real archive and
   record the provenance + SHA-256.
4. **End to end.** The chain under test is command line → archive → decompiler →
   stdout/file. No test stops halfway and asserts an internal stage.
5. **Behaviour before code.** A change starts by stating the behaviour it changes
   (corpus, class, command, expected output, independent ground truth), captured from
   a real archive. Only then is the implementation touched. A fix is finished when that
   behaviour is demonstrated on the real archive and no other behaviour regresses.
6. **Ground truth must be independent of the thing under test.** Two are allowed:
   the DEX bytecode itself (disassembly, e.g. `bench/disas.py`), or the Python reference
   implementation in the ASC checkout. `rasc`'s own output is never the reference —
   but the reference is also not automatically right: it has bugs of its own (see
   "Do not align with these" below), so a bytecode check wins where the two disagree.

## Running the suites

```sh
cargo test --release                 # unit/contract tests inside the crates (fast, hermetic)
bash bench/contracts.sh <apk> target/release/rasc    # CLI contract checks, corpus-agnostic
python3 bench/quality_vs_reference.py <apk|jar> --per-dex 20   # per-class quality vs reference
python3 bench/compare_vs_reference.py                # throughput / result-set parity
```

Benchmark and quality corpora are not part of the repository: place the three
archives (WeChat, Android Settings, vivo `framework.jar`) under `/tmp/rasc_corpus/`.
Every `bench/` harness takes the archive path directly.

## Repository layout

- `src/` — `rasc` CLI: `apk.rs` (archive access), `dex/` (container, filters, MUTF-8,
  prefix index), `manifest.rs` (AXML → text), `query.rs` (query parsing), `cli.rs`,
  `main.rs`. New analysis commands live next to these (`access.rs`, `summary.rs`,
  `aidl.rs`, `receivers.rs` on the `wip/summary-aidl-receivers` branch).
- `vendor/droidsaw-dex/` — the decompiler, a vendored copy of
  `github.com/droidsaw/droidsaw-dex` 2.0.0 with our patches (see `PATCHES.md` in that
  directory for what and why; `UPSTREAM.md` for the fixes proposed upstream). Fixes to
  the decompiler are developed in the fork `jygzyc/droidsaw-dex` (branch `rasc`, clone at
  `/tmp/dsd/work`) and synced here, never the other way round.
- `bench/` — measurement and verification harnesses (`contracts.sh`,
  `quality_vs_reference.py`, `compare_vs_reference.py`, `disas.py`).
- `tests/self_contained.rs` — the hermetic test that production code carries no
  Python/JVM/subprocess dependency.

## Do not align with these (reference-side bugs)

- `tinydex` truncates MUTF-8 class names that contain multi-byte characters, so the
  reference's `classes` list is missing names and prints some without the trailing `;`.
  `rasc` matches androguard here; the reference is wrong.
- `droidasc/asc_core/handlers/asc_handler.py` can report a `findrefs` row it never found.
- The reference's `getclass` loses constructor invocations too (`new X;` + `v7(...)`), so
  constructor pairing is checked against the bytecode, not against it.

## Committing

- Decompiler fixes: commit in the fork first, then sync `vendor/droidsaw-dex`, updating
  `PATCHES.md` (what changed, why, measured effect) and `UPSTREAM.md` when the fix is
  one upstream should take.
- CLI/bench changes: commit on `rust`. Keep `bench/quality_vs_reference.py` baselines in
  `README.md` current when the numbers move.
