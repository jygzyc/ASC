#!/usr/bin/env python3
"""Acceptance scenarios: behaviour, end to end, over real archives.

Reads `bench/scenarios.py` (see AGENTS.md for the policy it enforces),
verifies every corpus by SHA-256, then runs each scenario against the real `rasc`
binary. Nothing is mocked or stubbed: every check is a subprocess invocation over a
real APK/JAR, and the ground truth is either the DEX bytecode or the Python
reference implementation.

Usage:
    bench/acceptance.py                     # run everything declared
    bench/acceptance.py --list              # what is declared, run nothing
    bench/acceptance.py --only guards       # substring filter on the scenario id
    bench/acceptance.py --skip-quality      # skip the corpus-wide quality gates
    bench/acceptance.py --allow-blocked     # tolerate corpora that are not on this machine

Exit status: 1 when a `pass` scenario fails or a corpus is missing (unless
--allow-blocked); `known-failing` scenarios are expected to fail until the bug they
describe is fixed, and `not-implemented` scenarios carry pre-registered criteria.

Environment: RASC_BIN (default target/release/rasc), REF_ROOT (default /tmp/asc-ref),
REF_PY (default python3.12), REF_PYTHONPATH (default /tmp/agcheck_lxml:/tmp/agcheck).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios as spec_module

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RASC = os.environ.get("RASC_BIN", os.path.join(REPO, "target", "release", "rasc"))
REF_ROOT = os.environ.get("REF_ROOT", "/tmp/asc-ref")
REF_PY = os.environ.get("REF_PY", "python3.12")
REF_PYTHONPATH = os.environ.get("REF_PYTHONPATH", "/tmp/agcheck_lxml:/tmp/agcheck")

OK, FAIL, KNOWN, UNEXPECTED, BLOCKED, SKIPPED = "ok", "FAIL", "known-failing", "unexpectedly-passing", "blocked", "skipped"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_rasc(args, timeout=600):
    started = time.time()
    proc = subprocess.run([RASC, *args], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr, (time.time() - started) * 1000.0


def check_regexes(stdout, require, forbid, problems):
    for pattern in require or []:
        if not re.search(pattern, stdout, re.MULTILINE):
            problems.append(f"missing /{pattern}/")
    for pattern in forbid or []:
        if re.search(pattern, stdout, re.MULTILINE):
            problems.append(f"forbidden /{pattern}/ matched")


def check_expected(record, expect, problems):
    """`expect` maps a signal count to its ceiling (`missing_literals: 0`), plus
    `allowed_signals` for signals that are documented artefacts."""
    counts = {
        "missing_literals": len(record.get("missing_literals") or []),
        "missing_methods": len(record.get("missing_methods") or []),
        "empty_control": record.get("rasc_empty_control", 0),
        "stub": len(record.get("rasc_stub") or []),
    }
    for key, ceiling in (expect or {}).items():
        if key == "allowed_signals":
            continue
        if key == "max_empty_control":
            key = "empty_control"
        if key == "flagged":
            continue
        if key not in counts:
            problems.append(f"unknown expectation key {key!r}")
            continue
        if counts[key] > ceiling:
            problems.append(f"{key}={counts[key]} > {ceiling}")
    if "allowed_signals" in (expect or {}):
        import quality_vs_reference as qv

        tolerated = set(expect["allowed_signals"])
        left = [s for s in qv.signals(record) if s not in tolerated]
        if left:
            problems.append(f"unexpected signals {left}")


def scenario_class_parity(scenario, corpus_path, threads):
    import quality_vs_reference as qv

    qv.RASC = RASC
    ref_env = dict(os.environ, PYTHONPATH=REF_PYTHONPATH)
    record = qv.compare(corpus_path, scenario["class"], threads, ref_env)
    problems = []
    if record.get("rasc_exit") != 0 or not record.get("rasc_error") == "":
        problems.append(f"rasc failed: {record.get('rasc_error')!r}")
    if record.get("ref_exit") != 0:
        problems.append(f"reference failed: {record.get('ref_error')!r}")
    check_expected(record, scenario.get("expect"), problems)
    code, stdout, stderr, _ = run_rasc(["getclass", "--threads", threads, corpus_path, scenario["class"]])
    if code != 0:
        problems.append(f"getclass exit {code}: {stderr.strip()[:120]}")
    check_regexes(stdout, scenario.get("require_stdout_regex"), scenario.get("forbid_stdout_regex"), problems)
    detail = " ".join(problems)
    return (not problems), detail, {
        "signals": [s for s in qv.signals(record)],
        "missing_literals": len(record.get("missing_literals") or []),
        "missing_methods": len(record.get("missing_methods") or []),
        "empty_control": record.get("rasc_empty_control"),
        "rasc_ms": record.get("rasc_ms"),
        "ref_ms": record.get("ref_ms"),
    }


def scenario_class_shape(scenario, corpus_path, threads):
    code, stdout, stderr, ms = run_rasc(["getclass", "--threads", threads, corpus_path, scenario["class"]])
    problems = []
    if code != 0:
        problems.append(f"exit {code}: {stderr.strip()[:120]}")
    payload = stdout.encode()
    if "max_stdout_bytes" in scenario and len(payload) > scenario["max_stdout_bytes"]:
        problems.append(f"{len(payload)} bytes > {scenario['max_stdout_bytes']}")
    if "max_ms" in scenario and ms > scenario["max_ms"]:
        problems.append(f"{ms:.0f} ms > {scenario['max_ms']}")
    check_regexes(stdout, scenario.get("require_stdout_regex"), scenario.get("forbid_stdout_regex"), problems)
    return (not problems), " ".join(problems), {
        "stdout_bytes": len(payload),
        "ms": round(ms),
        "signals": [],
    }


def scenario_contracts(scenario, corpus_path, threads):
    proc = subprocess.run(["bash", os.path.join(HERE, "contracts.sh"), corpus_path, RASC],
                          capture_output=True, text=True, cwd=REPO)
    output = (proc.stdout + proc.stderr).strip().splitlines()
    tail = output[-1] if output else ""
    return proc.returncode == 0, f"exit {proc.returncode}: {tail[:160]}", {"detail": tail[:200]}


def scenario_corpus_quality(scenario, corpus_path, args):
    import quality_vs_reference as qv

    json_path = f"/tmp/acceptance_{scenario['id']}.json"
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "quality_vs_reference.py"), corpus_path,
         "--rasc", RASC,
         "--per-dex", str(scenario["per_dex"]), "--threads", args.threads, "--workers", str(args.workers),
         "--json", json_path, "--report", f"/tmp/acceptance_{scenario['id']}.md", "--quiet"],
        capture_output=True, text=True, cwd=REPO, env=dict(os.environ, PYTHONPATH=REF_PYTHONPATH))
    if proc.returncode != 0:
        return False, f"harness exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:160]}", {}
    records = json.load(open(json_path))
    flagged = [r for r in records if qv.signals(r)]
    errors = [r for r in records if r.get("rasc_error") or r.get("ref_error")]
    problems = []
    if errors:
        problems.append(f"{len(errors)} class(es) errored")
    if len(flagged) > scenario["max_flagged"]:
        problems.append(f"flagged {len(flagged)} > {scenario['max_flagged']}")
    return (not problems), " ".join(problems), {
        "classes": len(records), "flagged": len(flagged), "errors": len(errors),
        "flag_rate": round(100.0 * len(flagged) / max(len(records), 1), 2),
    }


def scenario_no_mocks(scenario, _corpus_path, _args):
    hits = []
    for pattern in scenario["scan"]:
        for path in sorted(glob.glob(os.path.join(REPO, pattern), recursive=True)):
            if "/vendor/" in path or "/target/" in path:
                continue
            # The declarations module names the needles as data, like this
            # runner does; scanning it would flag the policy itself.
            if os.path.basename(path) == "scenarios.py":
                continue
            with open(path, "r", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    for identifier in scenario["forbidden_identifiers"]:
                        if identifier in line:
                            hits.append(f"{os.path.relpath(path, REPO)}:{number}: {identifier}")
    detail = "; ".join(hits[:5])
    return (not hits), detail, {"hits": len(hits)}


def main():
    global RASC
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rasc", default=RASC)
    parser.add_argument("--only", default="", help="substring filter on the scenario id")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--skip-quality", action="store_true", help="skip corpus-wide quality gates")
    parser.add_argument("--allow-blocked", action="store_true", help="do not fail on missing corpora")
    parser.add_argument("--strict", action="store_true", help="also fail when a known-failing scenario passes")
    parser.add_argument("--threads", default="8")
    # Default 8: the benchmark condition both implementations are measured at
    # (README). At 4 workers the reference getclass pool is the bottleneck on the
    # larger corpora and the quality scenarios can exceed their scenario timeout.
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--json", default="/tmp/acceptance.json")
    args = parser.parse_args()

    RASC = args.rasc
    corpora = spec_module.CORPORA
    scenarios = [s for s in spec_module.SCENARIOS if args.only in s["id"]]

    if args.list:
        for scenario in scenarios:
            print(f"{scenario['status']:>15}  {scenario['id']:<32} {scenario['title']}")
        print(f"\n{len(scenarios)} scenario(s), {len(corpora)} corpus entries")
        return 0

    if not os.path.exists(RASC):
        print(f"rasc not found at {RASC}; build it first (cargo build --release)", file=sys.stderr)
        return 2

    resolved, blocked = {}, []
    for name, corpus in corpora.items():
        path = corpus["path"]
        if not os.path.isabs(path):
            # Corpus paths are repo-relative so the suite runs from any working
            # directory; the JSON records the layout, this resolves it.
            path = os.path.join(REPO, path)
        if not os.path.exists(path):
            blocked.append(f"{name}: missing {path}")
            resolved[name] = None
            continue
        digest = sha256(path)
        if digest != corpus["sha256"]:
            blocked.append(f"{name}: sha256 {digest[:16]} does not match the pinned {corpus['sha256'][:16]}")
            resolved[name] = None
            continue
        resolved[name] = path

    results = []
    for scenario in scenarios:
        kind = scenario["kind"]
        status = scenario["status"]
        label = f"{scenario['id']}"
        if kind == "not-implemented":
            results.append((label, SKIPPED, f"{len(scenario.get('acceptance_criteria', []))} criteria pre-registered", {}))
            continue
        corpus_path = resolved.get(scenario.get("corpus", ""))
        if scenario.get("corpus") and corpus_path is None:
            results.append((label, BLOCKED, f"corpus {scenario['corpus']!r} unavailable or hash mismatch", {}))
            continue
        if kind == "corpus-quality" and args.skip_quality:
            results.append((label, SKIPPED, "quality gate skipped (--skip-quality)", {}))
            continue
        started = time.time()
        try:
            if kind == "class-parity":
                passed, detail, extra = scenario_class_parity(scenario, corpus_path, args.threads)
            elif kind == "class-shape":
                passed, detail, extra = scenario_class_shape(scenario, corpus_path, args.threads)
            elif kind == "contracts":
                passed, detail, extra = scenario_contracts(scenario, corpus_path, args.threads)
            elif kind == "corpus-quality":
                passed, detail, extra = scenario_corpus_quality(scenario, corpus_path, args)
            elif kind == "policy-no-mocks":
                passed, detail, extra = scenario_no_mocks(scenario, corpus_path, args)
            else:
                passed, detail, extra = False, f"unknown kind {kind!r}", {}
        except Exception as error:  # noqa: BLE001 - a harness crash is a scenario failure, not a mock
            passed, detail, extra = False, f"{type(error).__name__}: {error}", {}
        extra["seconds"] = round(time.time() - started, 2)
        if status == "pass":
            verdict = OK if passed else FAIL
        elif status == "known-failing":
            verdict = KNOWN if not passed else UNEXPECTED
        else:
            verdict = SKIPPED
        results.append((label, verdict, detail if not passed else (detail or "ok"), extra))

    width = max((len(r[0]) for r in results), default=10)
    print(f"acceptance: {len(results)} scenario(s), rasc={os.path.relpath(RASC, REPO)}\n")
    for label, verdict, detail, extra in results:
        numbers = ""
        if verdict == OK and extra:
            keys = ("signals", "flagged", "classes", "flag_rate", "stdout_bytes", "ms", "hits")
            shown = [f"{k}={extra[k]}" for k in keys if k in extra and (extra[k] or extra[k] == 0)]
            numbers = "  " + " ".join(str(v) for v in shown)
        print(f"  {verdict:>20}  {label:<{width}}  {detail[:110]}{numbers}")

    failures = [r for r in results if r[1] == FAIL]
    unexpected = [r for r in results if r[1] == UNEXPECTED]
    known = [r for r in results if r[1] == KNOWN]
    blocked_runs = [r for r in results if r[1] == BLOCKED]
    print(f"\nok={sum(1 for r in results if r[1] == OK)} known-failing={len(known)} "
          f"fail={len(failures)} blocked={len(blocked_runs)} skipped={sum(1 for r in results if r[1] == SKIPPED)}")
    if blocked:
        print("corpora unavailable: " + "; ".join(blocked))
    with open(args.json, "w") as handle:
        json.dump({"scenarios": [{"id": r[0], "verdict": r[1], "detail": r[2], **r[3]} for r in results]},
                  handle, indent=1)
    print(f"json: {args.json}")

    code = 0
    if failures:
        code = 1
    if blocked_runs and not args.allow_blocked:
        code = 1
    if args.strict and unexpected:
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
