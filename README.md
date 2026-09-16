# rasc

[中文说明](README.zh-CN.md)

## What rasc is

rasc is a Rust re-implementation of ASC: an APK/DEX analysis tool **built as an experiment,
but usable in practice**. It explores native performance and the ability of coding agents
to refactor code and optimize performance toward clearly defined goals.

Most implementation and iteration are carried out by agents using ASC as a reference,
with occasional human intervention. It is both a working tool and an exercise in
agent-driven development, not a claim of fully autonomous software generation.

## rasc is CLI-only

For testing purposes, rasc stays simple: CLI only, optimized for agent workflows, with
no GUI.

## Performance and trade-offs

Across 11 test scenarios on a 343 MiB APK, rasc achieves a geometric mean speedup of
**8.0×** over ASC. Detailed measurements are included below.

That speed is not free. Optimization has led some of rasc's designs away from ASC;
it is no longer a line-by-line translation. It favors throughput and is willing to spend
more memory for speed: multithreaded reference searches, for example, have a higher peak
memory footprint than ASC. This is a trade-off, not a claim of lower resource use everywhere.

The speedup should first be understood in the context of moving from Python to a native
Rust implementation—not as proof that Rust beats other languages or that agents beat
human developers. Algorithms, parallelism, and memory strategies also affect the result.
An implementation in Zig or C++ might go further.

<details>
<summary>Benchmark results, memory usage, and reproduction</summary>

### Test conditions

- Apple M3 Pro (6 performance + 6 efficiency cores), 36 GiB RAM, macOS 26.5.1.
- ASC: CPython 3.12.14, Androguard 4.1.4. rasc: Rust release build with FatLTO.
- Input: WeChat `com.tencent.mm` base.apk (243 MiB, 16 root DEXes, 227,802 classes).
  Both implementations use 8 workers.
- End-to-end wall time: fresh process per sample, randomized execution order, median of
  at least 3 runs. Output is discarded, but formatting and writing are included.
- Exit status and output are checked before timing; result sets are compared where applicable.

### Execution time

| Scenario | rasc | ASC | Speedup |
|---|---:|---:|---:|
| `findrefs string Authorization` | 43 ms | 214 ms | 5.0× |
| `findrefs string okhttp` | 38 ms | 131 ms | 3.5× |
| `findrefs type Gson` | 38 ms | 121 ms | 3.2× |
| `findrefs method onCreate` | 54 ms | 336 ms | 6.2× |
| `findrefs method onCreate --class androidx --fuzzy-class` | 46 ms | 303 ms | 6.6× |
| `findrefs field INSTANCE` | 48 ms | 310 ms | 6.5× |
| `getclass` early class (`classes.dex`) | 51 ms | 81 ms | 1.6× |
| `getclass` late class (`classes16.dex`) | 31 ms | 50 ms | 1.6× |
| `getclass` missing class | 39 ms | 89 ms | 2.3× |
| `manifest` | 7 ms | 146 ms | 19.8× |
| `classes` | 56 ms | 747 ms | 13.5× |
| **Geometric mean** | | | **4.7×** |

ASC has no CLI command for `manifest` or `classes`; the benchmark calls the underlying
functions used by its GUI. Search semantics also differ: rasc uses literal queries and
instruction-boundary scanning, so arbitrary queries need not produce identical results.

### Memory

Peak RSS on the same APK with 8 workers:

| Scenario | rasc | ASC |
|---|---:|---:|
| `findrefs string Authorization` | 230 MiB | 142 MiB |
| `findrefs field INSTANCE` | 203 MiB | 201 MiB |
| `getclass` early class | 239 MiB | 184 MiB |
| `manifest` | 7 MiB | 15 MiB |
| `classes` | 228 MiB | 15 MiB |

rasc uses more memory for parallel reference searches and the class index, less for
manifest decoding. Reducing workers trades speed for memory.

### Reproduce

Build rasc with `cargo build --release`. Set `RASC_BIN`, `APK`, `REF_ROOT`, and `REF_PY`
to absolute paths; `REF_PY` must point to a Python environment with ASC's dependencies.

```sh
APK=/path/to/app.apk RASC_BIN=/path/to/rasc/target/release/rasc \
  REF_ROOT=/path/to/ASC REF_PY=/path/to/venv/bin/python \
  THREADS=8 python3 bench/compare_vs_reference.py
```

`bench/quality_vs_reference.py` is the per-class quality counterpart: the timing
harness above only checks that both sides produce the same result sets, while this one
samples classes per root DEX, runs `rasc getclass` and ASC's reference decompiler on
each, and reports where the two disagree — missing methods, missing string literals,
empty control-flow bodies, statements per class. Every difference it flags is inspected
against the bytecode before it is called a regression: both decompilers have defects of
their own (the reference truncates non-ASCII class names while decoding them, its
`findrefs` can report a row it never found), so "aligning" to a reference bug would be
a regression.

```sh
APK=/path/to/app.apk RASC_BIN=/path/to/rasc/target/release/rasc \
  REF_ROOT=/path/to/ASC REF_PY=/path/to/venv/bin/python \
  REF_PYTHONPATH=/path/to/androguard python3 bench/quality_vs_reference.py \
  --per-dex 20 --workers 4
```

On a 3,200-class sample across the three archives pinned by the acceptance suite
(WeChat `com.tencent.mm`, `com.android.settings`, and the vivo framework
`services.jar`) the current build flags 499 classes (15.6%): 438 on method-set naming
(R8 lambda names, `$`-prefixed synthetics), 109 with a missing string literal —
mostly Kotlin coroutine state machines, which rasc renders as a commented bytecode
listing instead of guessing — 41 with an empty control-flow body and 21 whose output
is thin; none fall back to a stub, and neither side errors on any class. Per-corpus:
WeChat 213/1,600 flagged (13.31%), Settings 261/1,300 (20.08%), services.jar 25/300
(8.33%).

</details>

## Testing

The repository has one acceptance entry point, described in `AGENTS.md`: tests assert
behaviour end to end, drive the real binary over real archives, and take the DEX
bytecode or the Python reference as ground truth — never a mock, a fake, or `rasc`'s own
output:

```sh
python3 bench/acceptance.py          # every declared scenario, one verdict per line
python3 bench/acceptance.py --list   # what is declared, without running anything
```

`tests/acceptance/scenarios.json` declares the scenarios (corpus, class, command,
expected behaviour, ground truth) before the code they judge is written, and pins each
corpus by SHA-256. A scenario is `pass`, `known-failing` (a declared defect that is the
gate for the next fix), or `not-implemented` (criteria pre-registered before coding); a
corpus that is not on this machine is reported as `blocked`, never silently skipped.

## Build

```sh
cargo build --release
./target/release/rasc --help
```

## Usage

```sh
rasc getclass app.apk com.example.Main                 # one class -> Java-like source
rasc getclass --threads 16 -o Main.java app.apk 'Lcom/example/Main;'
rasc findrefs app.apk string Authorization             # references across every root DEX
rasc findrefs app.apk method onCreate --class com.example.Main
rasc findrefs app.apk field INSTANCE --class example --fuzzy-class
rasc classes app.apk                                   # class index
rasc manifest app.apk                                  # binary AndroidManifest.xml -> XML
```

See `rasc --help` for more usage information, or `rasc <command> --help` for command-specific options.
