#!/usr/bin/env python3
"""Compare `rasc getclass` decompilation quality against the Python reference.

`bench/compare_vs_reference.py` answers "is rasc fast and does it return the same
result set". This harness answers the other half: "is the Java rasc prints the
same Java the reference prints", per class, on a real corpus.

For every sampled class both implementations decompile it and the script compares:

* exit status and empty output,
* the method names each side renders (a method only the reference renders means
  rasc dropped code),
* string literals (the semantic anchor: a literal the reference has and rasc does
  not is code rasc lost; `@Signature`-style descriptor fragments are ignored),
* empty *control-flow* bodies, the shape the Rust emitter produces when an
  if-converted chain loses a body (empty *method* bodies are normal, e.g. stubs,
  and are ignored),
* stub markers (`{ ... }`, `@droidsaw`) and a statement-count ratio.

Classes are sampled stratified by DEX entry (`--per-dex N`) or taken in full
(`--all`). Per-class records go to JSON so flagged classes can be diffed by hand;
the summary prints counts and the first examples of each signal.

Usage:
    bench/quality_vs_reference.py <apk|jar> [--per-dex 20] [--all]
        [--rasc BIN] [--ref-root DIR] [--ref-python PY] [--ref-pythonpath PATHS]
        [--threads N] [--workers N] [--json PATH] [--report PATH]
        [--fail-on-flagged N]

Environment defaults match the local reference checkout used for benchmarking:
RASC_BIN, REF_ROOT=/tmp/asc-ref, REF_PY=python3.12, REF_PYTHONPATH=/tmp/agcheck.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict

RASC = os.environ.get("RASC_BIN", "/Users/yves/Code/Github/ASC/target/release/rasc")
REF_ROOT = os.environ.get("REF_ROOT", "/tmp/asc-ref")
REF_PY = os.environ.get("REF_PY", "python3.12")
# lxml first: the reference's androguard copy needs a working lxml, and the first
# entry wins when both directories carry one.
REF_PYTHONPATH = os.environ.get("REF_PYTHONPATH", "/tmp/agcheck_lxml:/tmp/agcheck")

# A name followed by an argument list: definitions *and* declarations (an
# interface method has no body) as well as calls. What matters is the set
# difference: a name the reference prints and rasc never mentions at all.
# The name class has to include `-` and `$`: R8 lambda bodies are spelled
# `$r8$lambda$La-Ab` and the leading `$` digits would otherwise split the token,
# leaving a bare suffix that looks like a missing method.
METHOD_RE = re.compile(r"(?<![A-Za-z0-9_$])([A-Za-z0-9_$][\w$\-]*)\s*\(")
KEYWORDS = {"if", "for", "while", "switch", "catch", "else", "do", "try", "synchronized",
            "return", "new", "super", "this", "case", "assert"}
# Annotation names are rendered by the reference as `@Name(...)` and are metadata,
# not code; a decompiled original does not have to print them.
ANNOTATION_NAMES = {
    "Signature", "Throws", "MethodParameters", "AnnotationDefault", "EnclosingMethod",
    "InnerClass", "KotlinMetadata", "Metadata", "DebugMetadata", "SuppressLint", "Override",
    "Deprecated", "TargetApi", "RequiresApi", "SdkSuppress", "RequiresPermission",
    "JvmStatic", "JvmField", "JvmOverloads", "JvmName", "NotNull", "Nullable", "IntRange",
    "InlineOnly", "Keep", "SourceFile", "SourceDebugExtension", "LocalVariableTable",
    "LocalVariableTypeTable", "RuntimeVisibleAnnotations", "RuntimeInvisibleAnnotations",
    "ConstructorParameters", "ColorInt", "Experimental", "ExtensionFunctionType",
}
LAMBDA_PREFIX_RE = re.compile(r"^(?:\w+\$)+lambda\$")
# `@Signature`/`@Throws` bodies are metadata: the reference prints the descriptor
# strings, and a descriptor like `Ljava/lang/Object;->equals(...)` would otherwise
# count as a method rasc "lost" and `"I"` as a literal it dropped.
ANNOTATION_SPAN_RE = re.compile(r"@(\w+)\(")


def strip_annotations(text):
    """Drop `@Name(...)` spans for the annotations the reference renders."""
    out = []
    cursor = 0
    while True:
        match = ANNOTATION_SPAN_RE.search(text, cursor)
        if match is None:
            out.append(text[cursor:])
            return "".join(out)
        if match.group(1) not in ANNOTATION_NAMES:
            out.append(text[cursor:match.end()])
            cursor = match.end()
            continue
        out.append(text[cursor:match.start()])
        depth = 0
        index = match.end() - 1
        while index < len(text):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        cursor = index + 1


CANONICAL_SYNTHETIC_RE = re.compile(r"(?:lambda\$|\$\$Nest\$|\$Nest\$|\$r8\$)")


def synthetic_name(name):
    """True for a member name either tool invented for a synthetic member.

    R8/ART synthesise `$r8$lambda$...` and droidsaw rewrites `-` (illegal in a Java
    identifier) to `_` plus a `$` prefix, so the same lambda is `...$lambda$La-Ab`
    on one side and `$_r8$lambda$La_Ab` on the other. Comparing those names is
    meaningless; whether the *body* survived is still checked in the metrics.
    """
    return bool(CANONICAL_SYNTHETIC_RE.search(name))


def canonical_member(name):
    """Fold the synthetic nest-accessor spellings onto the DEX name.

    The DEX holds `Nest$fgetmBooted`; droidsaw prints `$_$$Nest$fgetmBooted`
    (`-` is not a legal Java identifier) and the reference prints
    `-$$Nest$fgetmBooted`, which the method regex picks up as `Nest$fgetmBooted`.
    All three mean the same member.
    """
    index = name.rfind("$$Nest$")
    if index != -1:
        return name[index + 2:]
    index = name.find("$Nest$")
    if index != -1:
        return name[index + 1:]
    return name
LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
STUB_MARKERS = ("{ ... }", "@droidsaw")
CONTROL_LINE_RE = re.compile(
    r"^\s*(?:\}\s*)?(?:if|while|for|switch|else\s+if)\b.*\{\s*$"
    r"|^\s*(?:\}\s*)?else\s*\{\s*$"
)
STATEMENT_RE = re.compile(r";\s*$", re.M)


def run(cmd, cwd=None, env=None):
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env)
    return proc.returncode, proc.stdout, proc.stderr, (time.perf_counter() - started) * 1000.0


def rasc_classes(path, threads):
    code, out, err, _ = run([RASC, "classes", "--threads", threads, path])
    if code != 0:
        raise SystemExit(f"classes failed: {err.strip()}")
    rows = []
    for line in out.splitlines():
        parts = line.split(" | ")
        if len(parts) >= 2 and parts[1].startswith("L") and parts[1].endswith(";"):
            rows.append((parts[0], parts[1]))
    return rows


def sample(rows, per_dex):
    by_dex = defaultdict(list)
    for dex, descriptor in rows:
        by_dex[dex].append(descriptor)
    picked = []
    for dex in sorted(by_dex):
        descriptors = by_dex[dex]
        step = max(1, len(descriptors) // per_dex)
        picked.extend(descriptors[::step][:per_dex])
    return picked


def empty_control_blocks(text):
    lines = text.splitlines()
    count = 0
    for index, line in enumerate(lines[:-1]):
        if not CONTROL_LINE_RE.match(line):
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor < len(lines) and lines[cursor].strip() == "}":
            count += 1
    return count


def looks_like_descriptor(literal):
    return any(character in literal for character in ";()<>[")


JAVA_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "s": " ", "'": "'", '"': '"', "\\": "\\"}


def java_literal_value(raw):
    """The value of a Java string literal, as written between the quotes.

    The two tools escape the same string differently (`Can\'t` vs `Can't`, `\u0041`
    vs `A`), so compare values, not spellings.
    """
    out = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        nxt = raw[index + 1 : index + 2]
        if nxt == "u":
            digits = raw[index + 2 : index + 6]
            if re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                out.append(chr(int(digits, 16)))
                index += 6
                continue
            out.append(nxt)
            index += 2
            continue
        octal = re.match(r"[0-7]{1,3}", raw[index + 1 :])
        if octal is not None:
            out.append(chr(int(octal.group(0), 8)))
            index += 1 + len(octal.group(0))
            continue
        out.append(JAVA_ESCAPES.get(nxt, nxt))
        index += 2
    return "".join(out)


def metrics(text, class_simple_name=""):
    code = strip_annotations(text)
    methods = set()
    for match in METHOD_RE.finditer(code):
        name = match.group(1)
        if name in KEYWORDS or name in ANNOTATION_NAMES:
            continue
        # Droidsaw renames synthetic lambda bodies (`dsaw$lambda$f$1`) and the
        # nest accessors; both sides mean the same method.
        name = LAMBDA_PREFIX_RE.sub("lambda$", name)
        name = canonical_member(name)
        if class_simple_name and name == class_simple_name:
            continue
        methods.add(name)
    literals = {java_literal_value(m.group(1)) for m in LITERAL_RE.finditer(code)
                if not looks_like_descriptor(m.group(1))}
    return {
        "method_set": methods,
        "literals": literals,
        "lines": len(text.splitlines()),
        "statements": len(STATEMENT_RE.findall(text)),
        "empty_control": empty_control_blocks(text),
        "stub": [marker for marker in STUB_MARKERS if marker in text],
    }


def signals(record):
    """Signals that the class needs a look. Not all of them are defects: a method
    the reference prints and rasc does not can also be a synthetic/lambda name the
    reference invents, and a small statement count can be better code."""
    found = []
    if record.get("missing_methods"):
        found.append("method-set")
    if record.get("missing_literals"):
        found.append("literal")
    if record.get("rasc_empty_control", 0) > 0:
        found.append("empty-control-flow")
    if record.get("rasc_stub"):
        found.append("stub")
    if record.get("ref_statements", 0) >= 8 and record.get("rasc_statements", 0) * 4 < record["ref_statements"]:
        found.append("thin")
    return found


def compare(path, descriptor, threads, ref_env):
    rcode, rout, rerr, rms = run([RASC, "getclass", "--threads", threads, path, descriptor])
    pcode, pout, perr, pms = run(
        [REF_PY, "main.py", "getclass", "--threads", threads, path, descriptor],
        cwd=REF_ROOT,
        env=ref_env,
    )
    record = {
        "class": descriptor,
        "rasc_exit": rcode,
        "ref_exit": pcode,
        "rasc_ms": round(rms),
        "ref_ms": round(pms),
        "rasc_error": (rerr or rout).strip()[:200] if rcode != 0 or not rout.strip() else "",
        "ref_error": (perr or pout).strip()[:200] if pcode != 0 or not pout.strip() else "",
    }
    if rcode == 0 and pcode == 0 and rout.strip() and pout.strip():
        class_simple_name = descriptor[1:-1].rsplit("/", 1)[-1]
        rasc_metrics = metrics(rout, class_simple_name)
        ref_metrics = metrics(pout, class_simple_name)
        # Synthetic members (lambda bodies, nest accessors, R8 stubs) get a freshly
        # invented name on each side; a name that only one side renders is not a
        # dropped method there.
        missing_methods = [n for n in ref_metrics["method_set"] - rasc_metrics["method_set"]
                           if not synthetic_name(n)]
        extra_methods = [n for n in rasc_metrics["method_set"] - ref_metrics["method_set"]
                         if not synthetic_name(n)]
        record.update({
            "missing_methods": sorted(missing_methods),
            "extra_methods": sorted(extra_methods),
            "missing_literals": sorted(ref_metrics["literals"] - rasc_metrics["literals"]),
            "ref_literals": len(ref_metrics["literals"]),
            "rasc_statements": rasc_metrics["statements"],
            "ref_statements": ref_metrics["statements"],
            "rasc_lines": rasc_metrics["lines"],
            "ref_lines": ref_metrics["lines"],
            "rasc_empty_control": rasc_metrics["empty_control"],
            "rasc_stub": rasc_metrics["stub"],
        })
    return record


def build_report(path, records):
    """Markdown summary; the numbers the benchmark and the bug triage both read."""
    flagged = [r for r in records if signals(r)]
    rasc_errors = [r for r in records if r["rasc_error"]]
    ref_errors = [r for r in records if r["ref_error"]]
    literals_total = sum(r.get("ref_literals", 0) for r in records)
    literals_lost = sum(len(r.get("missing_literals", [])) for r in records)

    lines = [
        f"# decompilation quality: rasc vs reference",
        "",
        f"- corpus: `{path}`",
        f"- classes compared: {len(records)}",
        f"- rasc errors: {len(rasc_errors)}, reference errors: {len(ref_errors)}",
        f"- flagged classes: {len(flagged)}"
        + (f" ({100.0 * len(flagged) / len(records):.2f}%)" if records else ""),
        f"- string literals: {literals_total} in the reference, {literals_lost} missing from rasc",
    ]
    if records:
        rasc_ms = [r["rasc_ms"] for r in records if not r["rasc_error"]]
        ref_ms = [r["ref_ms"] for r in records if not r["ref_error"]]
        if rasc_ms and ref_ms:
            lines.append(
                f"- median wall time per class: rasc {statistics.median(rasc_ms):.0f} ms, "
                f"reference {statistics.median(ref_ms):.0f} ms"
            )
    by_signal = defaultdict(int)
    for record in flagged:
        for signal in signals(record):
            by_signal[signal] += 1
    if by_signal:
        lines += ["", "| signal | classes |", "| --- | --- |"]
        lines += [f"| {signal} | {count} |" for signal, count in sorted(by_signal.items(), key=lambda kv: -kv[1])]

    if rasc_errors or ref_errors:
        lines += ["", "## errors", ""]
        for record in (rasc_errors + ref_errors)[:20]:
            if record in rasc_errors:
                lines.append(f"- rasc `{record['class']}`: exit={record['rasc_exit']} {record['rasc_error'][:120]}")
            else:
                lines.append(f"- reference `{record['class']}`: exit={record['ref_exit']} {record['ref_error'][:120]}")
    if flagged:
        lines += ["", "## flagged classes (first 25)", "",
                  "| class | signals | statements rasc/ref | empty | missing literals |",
                  "| --- | --- | --- | --- | --- |"]
        for record in flagged[:25]:
            lines.append(
                f"| `{record['class']}` | {', '.join(signals(record))} | "
                f"{record.get('rasc_statements')}/{record.get('ref_statements')} | "
                f"{record.get('rasc_empty_control')} | {len(record.get('missing_literals', []))} |"
            )
    lines.append("")
    return "\n".join(lines)


def main():
    global RASC
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    parser.add_argument("--per-dex", type=int, default=20, help="classes to sample per DEX entry (default 20)")
    parser.add_argument("--all", action="store_true", help="compare every class instead of a sample")
    parser.add_argument("--rasc", default=RASC)
    parser.add_argument("--ref-root", default=REF_ROOT)
    parser.add_argument("--ref-python", default=REF_PY)
    parser.add_argument("--ref-pythonpath", default=REF_PYTHONPATH)
    parser.add_argument("--threads", default="4")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--json", default="")
    parser.add_argument("--report", default="")
    parser.add_argument("--fail-on-flagged", type=int, default=-1,
                        help="exit 1 when more than N classes are flagged (0 = fail on any)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    RASC = args.rasc
    ref_env = dict(os.environ, PYTHONPATH=args.ref_pythonpath)

    rows = rasc_classes(args.path, args.threads)
    classes = [descriptor for _, descriptor in rows] if args.all else sample(rows, args.per_dex)
    dex_count = len({dex for dex, _ in rows})
    if not args.quiet:
        print(f"{args.path}: {len(rows)} classes over {dex_count} DEX entries; "
              f"comparing {len(classes)}", flush=True)

    records = []
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(compare, args.path, descriptor, args.threads, ref_env)
                   for descriptor in classes]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            records.append(future.result())
            if not args.quiet and index % 25 == 0:
                print(f"  {index}/{len(classes)} ...", flush=True)

    report = build_report(args.path, records)
    json_path = args.json or f"/tmp/quality_{os.path.basename(args.path).replace('.', '_')}.json"
    with open(json_path, "w") as handle:
        json.dump(records, handle, indent=1, sort_keys=True)
    report_path = args.report or f"/tmp/quality_{os.path.basename(args.path).replace('.', '_')}.md"
    with open(report_path, "w") as handle:
        handle.write(report)
    if not args.quiet:
        print(report)
    print(f"records: {json_path}\nreport:  {report_path}")

    flagged = [r for r in records if signals(r)]
    if args.fail_on_flagged >= 0 and len(flagged) > args.fail_on_flagged:
        print(f"quality gate: {len(flagged)} flagged classes exceed --fail-on-flagged {args.fail_on_flagged}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
