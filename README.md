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

Across 11 scenarios on the 565 MiB WeChat `base.apk`, rasc achieves a geometric-mean
speedup of **4.8×** over ASC, with equal result sets on every comparable scenario.
Smaller archives widen the gap (`classes` is 31× on the 9.8 MiB Settings APK), while
`getclass` stays around 1.2-2.7× because both sides parse the whole archive first.
Detailed measurements are included below.

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

- Apple M4 Pro, 48 GiB RAM, macOS 26.6.2.
- ASC: CPython 3.12.12, Androguard 4.1.4. rasc: Rust release build with FatLTO.
- Inputs — not shipped with the repo, pinned by SHA-256 in
  `tests/acceptance/scenarios.json`, benchmarked from `/tmp/rasc_corpus/`:
  WeChat `com.tencent.mm` base.apk (565 MiB, 16 root DEXes, 227,802 classes),
  Android `com.android.settings` base.apk (9.8 MiB, 13 DEXes, 57,535 classes), and
  vivo V2324A `framework.jar` (49.5 MiB, 6 DEXes, 37,815 classes).
- Both implementations use 8 workers. End-to-end wall time: fresh process per sample,
  randomized execution order, median of at least 3 runs on the WeChat APK, median of 3
  on the smaller archives. Output is discarded, but formatting and writing are included.
- Exit status and output are checked before timing; result sets are compared where applicable.

### Execution time

WeChat `base.apk` (565 MiB):

| Scenario | rasc | ASC | Speedup |
|---|---:|---:|---:|
| `findrefs` string `Authorization` | 42 ms | 218 ms | 5.2× |
| `findrefs` string `okhttp` | 45 ms | 157 ms | 3.5× |
| `findrefs` type `Lcom/google/gson/Gson;` | 46 ms | 163 ms | 3.6× |
| `findrefs` method `onCreate` | 57 ms | 447 ms | 7.8× |
| `findrefs` method `onCreate` + `--class androidx --fuzzy-class` | 54 ms | 402 ms | 7.5× |
| `findrefs` field `INSTANCE` | 52 ms | 363 ms | 7.0× |
| `getclass` first class in the index | 48 ms | 60 ms | 1.2× |
| `getclass` last class in the index | 47 ms | 62 ms | 1.3× |
| `getclass` missing class | 49 ms | 87 ms | 1.8× |
| `manifest` | 7 ms | 158 ms | 24× |
| `classes` | 63 ms | 1,539 ms | 24× |
| **Geometric mean** | | | **4.8×** |

Result-set parity holds on every scenario above (0 rows missing / 0 extra; `getclass`
output is byte-identical). `getclass` speedup by class position in the index:

| Class position | rasc | ASC | Speedup |
|---|---:|---:|---:|
| first (`classes7.dex`) | 48 ms | 60 ms | 1.2× |
| ~1/4 (`classes4.dex`) | 46 ms | 61 ms | 1.3× |
| middle (`classes16.dex`) | 43 ms | 75 ms | 1.7× |
| ~3/4 (`classes15.dex`) | 48 ms | 129 ms | 2.7× |
| last (`classes13.dex`) | 47 ms | 62 ms | 1.3× |

rasc is flat (~43-49 ms — a full-archive parse dominates); ASC grows with the distance
into the archive, except where a late class sits in a DEX it parses cheaply.

Settings `base.apk` (9.8 MiB) and `framework.jar` (49.5 MiB), median of 3:

| Scenario | Settings rasc | Settings ASC | Speedup | framework rasc | framework ASC | Speedup |
|---|---:|---:|---:|---:|---:|---:|
| `findrefs` string | 14 ms | 364 ms | 26× | 194 ms | 321 ms | 1.7× |
| `findrefs` type | 12 ms | 353 ms | 29× | 183 ms | 297 ms | 1.6× |
| `findrefs` method | 15 ms | 429 ms | 29× | 285 ms | 634 ms | 2.2× |
| `getclass` early class | 12 ms | 31 ms | 2.6× | 11 ms | 29 ms | 2.6× |
| `getclass` late class | 11 ms | 27 ms | 2.5× | 11 ms | 28 ms | 2.5× |
| `getclass` missing class | 12 ms | 29 ms | 2.4× | 12 ms | 29 ms | 2.4× |
| `manifest` | 5 ms | 124 ms | 25× | 4 ms | 14 ms | 3.5× |
| `classes` | 6 ms | 187 ms | 31× | 16 ms | 176 ms | 11× |

(Settings string `wifi`, type `Landroid/net/wifi/WifiManager;`, method `onCreate`;
framework string `android.app.ActivityManager`, type `Landroid/app/ActivityManagerService;`,
method `onCreate`. framework.jar contains no AndroidManifest.xml; its manifest row uses
the fast-fail path on both sides.)

ASC has no CLI command for `manifest` or `classes`; the benchmark calls the underlying
functions used by its GUI. Search semantics also differ: rasc uses literal queries and
instruction-boundary scanning, so arbitrary queries need not produce identical results.

### Memory

Peak RSS on the WeChat APK with 8 workers:

| Scenario | rasc | ASC |
|---|---:|---:|
| `findrefs` string `Authorization` | 321 MiB | 139 MiB |
| `findrefs` field `INSTANCE` | 309 MiB | 224 MiB |
| `getclass` first class in the index | 318 MiB | 191 MiB |
| `manifest` | 12 MiB | 15 MiB |
| `classes` | 16 GiB | 15 MiB |

rasc uses more memory for parallel reference searches and the class index, less for
manifest decoding. The `classes` row is the known outlier: with 8 worker threads the
batch flush holds every rendered line in memory before writing (16 GiB peak, ~2.1 GiB
single-threaded); bounding it is open tuning work. Reducing workers trades speed for memory.

### Reproduce

Build rasc with `cargo build --release`. Place the three pinned archives under
`/tmp/rasc_corpus/` (paths and SHA-256 in `tests/acceptance/scenarios.json`), set
`RASC_BIN`, `APK`, `REF_ROOT`, and `REF_PY` to absolute paths; `REF_PY` must point to
a Python environment with ASC's dependencies.

```sh
APK=/tmp/rasc_corpus/com.tencent.mm/base.apk RASC_BIN=$PWD/target/release/rasc \
  REF_ROOT=/tmp/asc-ref REF_PY=python3.12 \
  REF_PYTHONPATH=/tmp/agcheck_lxml:/tmp/agcheck THREADS=8 \
  python3.12 bench/compare_vs_reference.py
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
RASC_BIN=$PWD/target/release/rasc REF_ROOT=/tmp/asc-ref REF_PY=python3.12 \
  REF_PYTHONPATH=/tmp/agcheck_lxml:/tmp/agcheck \
  python3.12 bench/quality_vs_reference.py /tmp/rasc_corpus/framework.jar \
  --per-dex 1200 --threads 8 --workers 8
```

On a 10,100-class sample across the three pinned archives the current build flags 829
classes (8.21%): 680 on method-set naming (R8 lambda names, `$`-prefixed synthetics),
211 with a missing string literal — mostly Kotlin coroutine state machines, which rasc
renders as a commented bytecode listing instead of guessing — 81 with an empty
control-flow body and 50 whose output is thin (a class can carry several signals, so
the per-signal counts add up to more than 829); none fall back to a stub, and neither
side errors on any class. Per-corpus: WeChat 213/1,600 flagged (13.31%), Settings
261/1,300 (20.08%), framework.jar 355/7,200 (4.93%).

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
corpus by SHA-256. The corpora themselves are not part of the repository; on this
machine they live under `/tmp/rasc_corpus/` (WeChat, Android Settings, vivo
`framework.jar`). A scenario is `pass`, `known-failing` (a declared defect that is the
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
