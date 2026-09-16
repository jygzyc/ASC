#!/bin/bash
# CLI contract checks: the exit-code, payload and diagnostic invariants that the
# benchmark harness relies on, expressed as a script so they travel with the
# project instead of living only in a gitignored session folder.
#
# Usage: bash bench/contracts.sh <path/to/app.apk> [path/to/rasc]
#
# Needs a real APK; unit tests cover everything that can be checked without one.
set -euo pipefail

APK="${1:?usage: contracts.sh <apk> [rasc]}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="${2:-$ROOT/target/release/rasc}"
ABSENT='__RASC_ABSENT_7f32c9__'

[ -f "$APK" ] || { echo "contracts: missing APK $APK" >&2; exit 1; }
[ -x "$BIN" ] || { echo "contracts: missing binary $BIN" >&2; exit 1; }

# The probe class comes from the archive itself: no application-specific class
# name (and no vendor package) is baked into this script.
CLASS="$("$BIN" classes --threads 4 "$APK" | sed -n 1p | awk -F' \\| ' '{print $2}')"
[ -n "$CLASS" ] || { echo "contracts: class index is empty" >&2; exit 1; }
PROBE="${CLASS#L}"; PROBE="${PROBE%;}"; PROBE="${PROBE##*/}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/rasc-contracts.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "contract violated: $1" >&2; exit 1; }
same() { cmp -s "$1" "$2" || fail "$3"; }

# Every query mode returns rows for a query that exists in the APK, and nothing
# for one that does not. The fixed probe names are what application corpora tend
# to contain; a library or synthetic corpus may not carry them, so each mode also
# tries names taken from the archive itself (imports, signature types and members
# of the probe class). What is checked is that the mode returns rows for a query
# the corpus really contains, not that one particular application's names exist.
PROBE_TYPE="${RASC_PROBE_TYPE:-Gson}"
PROBE_METHOD="${RASC_PROBE_METHOD:-onCreate}"
PROBE_FIELD="${RASC_PROBE_FIELD:-INSTANCE}"

# Fallback names come from a class of the archive itself: the first index entries
# whose source decompiles. A stub class (an obfuscated one with no imports and no
# members) gives the fallback nothing to work with, so a few are tried.
"$BIN" classes --threads 4 "$APK" >"$TMP/index"
: >"$TMP/probes.java"
FOUND=0
FIRST_CLASS=""
while IFS= read -r candidate; do
  [ -n "$candidate" ] || continue
  [ "$FOUND" -lt 5 ] || break
  if "$BIN" getclass --threads 4 "$APK" "$candidate" >"$TMP/probe.try" 2>/dev/null \
    && [ -s "$TMP/probe.try" ]; then
    cat "$TMP/probe.try" >>"$TMP/probes.java"
    FIRST_CLASS="${FIRST_CLASS:-$candidate}"
    FOUND=$((FOUND + 1))
  fi
done <<< "$(sed -n '1,8p' "$TMP/index" | awk -F' \\| ' '{print $2}')"

GETCLASS_PROBE="${FIRST_CLASS:-$CLASS}"

IMPORTS=""; QUALIFIED=""; METHODS=""; FIELDS=""; CHAIN_FIELDS=""
if [ "$FOUND" -gt 0 ]; then
  IMPORTS="$(sed -n 's/^import \(.*\);$/\1/p' "$TMP/probes.java" | tr '.' '/' | sort -u | sed -n '1,6p' || true)"
  QUALIFIED="$(grep -oE '[a-z][a-zA-Z0-9_]*(\.[a-zA-Z0-9_$]+)+' "$TMP/probes.java" | tr '.' '/' | sort -u | sed -n '1,6p' || true)"
  # The last segment of a dotted chain is a member name: for `a.b.C.field` that is
  # `field`, which gives the field check a name the corpus really refers to even
  # when no decompiled class declares it.
  CHAIN_FIELDS="$(grep -oE '[a-z][a-zA-Z0-9_]*(\.[a-zA-Z0-9_$]+)+' "$TMP/probes.java" | sed -n 's/.*\.//p' | sort -u | sed -n '1,6p' || true)"
  METHODS="$(grep -oE '[A-Za-z_$][A-Za-z0-9_$]*\(' "$TMP/probes.java" | tr -d '(' \
    | grep -vxE 'if|for|while|switch|catch|super|return|new' | sort -u | sed -n '1,6p' || true)"
  FIELDS="$(sed -n 's/.* \([A-Za-z_$][A-Za-z0-9_$]*\);$/\1/p' "$TMP/probes.java" | sort -u | sed -n '1,6p' || true)"
fi

probe_mode() { # <mode> <outfile> <candidate>...
  local mode="$1" out="$2"; shift 2
  local candidate
  for candidate in "$@"; do
    [ -n "$candidate" ] || continue
    # `--` so that an obfuscated name that starts with a hyphen (Dalvik allows
    # them: `-$$Lambda$...`) is read as the query and not as a flag.
    "$BIN" findrefs --threads 4 "$APK" "$mode" -- "$candidate" >"$out" \
      || fail "$mode query failed for '$candidate'"
    [ -s "$out" ] && return 0
  done
  fail "$mode query returned no rows for any probe name"
}

# The unquoted expansions are the candidate lists built above; the trailing
# entries are types and members every application archive carries.
# shellcheck disable=SC2086
probe_mode type "$TMP/type" "$PROBE_TYPE" $IMPORTS $QUALIFIED java/lang/String android/content/Intent android/view/View
# shellcheck disable=SC2086
probe_mode method "$TMP/method" "$PROBE_METHOD" $METHODS onDraw toString equals
# shellcheck disable=SC2086
probe_mode field "$TMP/field" "$PROBE_FIELD" $CHAIN_FIELDS $FIELDS mContext mService TAG
"$BIN" findrefs --threads 4 "$APK" string -- "$ABSENT" >"$TMP/absent"
[ ! -s "$TMP/absent" ] || fail "absent query returned rows"

# A descriptor written with dots instead of slashes resolves to the same class
# (the reference implementation normalizes that too), which also exercises the
# descriptor matching in the scoped decompile parse. Any row of any mode names a
# packaged class, so the check does not depend on a field reference existing.
# `sed -n 1p` rather than `head -1`: this script runs with pipefail, and head
# closing the pipe early turns into a SIGPIPE failure.
PACKAGED="$(cat "$TMP/type" "$TMP/method" "$TMP/field" 2>/dev/null \
  | sed -n 's/^[^|]*| \([^ ]*\)->.*/\1/p' | grep / | sed -n 1p)"
[ -n "$PACKAGED" ] || fail "no packaged class found for the dotted-descriptor check"
DOTTED="$(printf '%s' "$PACKAGED" | tr / .)"
"$BIN" getclass --threads 4 "$APK" "$DOTTED" >"$TMP/dotted.java"
"$BIN" getclass --threads 4 "$APK" "$PACKAGED" >"$TMP/slashed.java"
same "$TMP/dotted.java" "$TMP/slashed.java" "dotted descriptor decompiled differently"

# The class index still contains a class known to be defined, and the manifest
# decodes to XML with the usual root and namespace. The filter is a value of an
# option, so an obfuscated probe name that starts with a hyphen needs the
# `--filter=<value>` spelling (clap's leading-dash tip only covers positionals).
"$BIN" classes --threads 4 "--filter=$PROBE" "$APK" >"$TMP/classes"
grep -qF "$CLASS" "$TMP/classes" || fail "class index lost the probe class"
# The XML checks need an AndroidManifest.xml; a bare-dex corpus (a framework
# classes.dex, a synthetic fixture) has none and `manifest` reports that as an
# error, so the section is skipped with a note instead of failing the run.
HAVE_MANIFEST=0
if "$BIN" manifest "$APK" >"$TMP/manifest.xml" 2>"$TMP/manifest.stderr"; then
  HAVE_MANIFEST=1
  grep -q '<manifest ' "$TMP/manifest.xml" || fail "manifest has no root element"
  grep -q 'xmlns:android=' "$TMP/manifest.xml" || fail "manifest lost its namespace"
  head -1 "$TMP/manifest.xml" | grep -qx '<?xml version="1.0" encoding="utf-8"?>' \
    || fail "manifest lost its XML declaration"
  # Indentation is four spaces per level, at whatever depths this corpus nests:
  # an element may be top level or sit inside another one. (This used to require
  # a `<uses-sdk>` line and a `<queries><package/>` element, which applications
  # without a `<queries>` section do not have.)
  awk '/^ +</ { match($0, /^ */); if (RLENGTH % 4 != 0 || RLENGTH > prev + 4) exit 1; prev = RLENGTH }' \
    "$TMP/manifest.xml" || fail "manifest indentation is not four spaces per level"
  grep -q '^    <' "$TMP/manifest.xml" || fail "manifest has no top-level elements"
  grep -q '^        <' "$TMP/manifest.xml" || fail "manifest deeper indentation is wrong"
  grep -q ' />$' "$TMP/manifest.xml" || fail "manifest no longer writes self-closing tags as ' />'"
  grep -q '[^ ]/>' "$TMP/manifest.xml" && fail "manifest wrote a self-closing tag without the space"
  [ "$(tail -1 "$TMP/manifest.xml")" = "</manifest>" ] || fail "manifest does not end with its root close tag"
  grep -q 'ResourceValueType::' "$TMP/manifest.xml" && fail "manifest leaks decoder placeholder values"
else
  echo "contracts: no AndroidManifest.xml in this corpus, skipping the XML rendering checks" >&2
fi

# Determinism is part of the contract: identical bytes across worker counts and
# across repeated runs (the entry scheduler flattens results in central-directory
# order, so which worker took which entry cannot show up in the output).
for threads in 1 4 8; do
  "$BIN" findrefs --threads "$threads" "$APK" field INSTANCE >"$TMP/field.$threads"
  "$BIN" classes --threads "$threads" "$APK" >"$TMP/classes.$threads"
done
same "$TMP/field.1" "$TMP/field.4" "findrefs output depends on --threads"
same "$TMP/field.1" "$TMP/field.8" "findrefs output depends on --threads"
same "$TMP/classes.1" "$TMP/classes.4" "classes output depends on --threads"
same "$TMP/classes.1" "$TMP/classes.8" "classes output depends on --threads"
"$BIN" findrefs --threads 8 "$APK" field INSTANCE >"$TMP/field.rerun"
"$BIN" classes --threads 8 "$APK" >"$TMP/classes.rerun"
same "$TMP/field.8" "$TMP/field.rerun" "repeated findrefs run differs"
same "$TMP/classes.8" "$TMP/classes.rerun" "repeated classes run differs"
"$BIN" getclass --threads 1 "$APK" "$GETCLASS_PROBE" >"$TMP/class.t1"
"$BIN" getclass --threads 8 "$APK" "$GETCLASS_PROBE" >"$TMP/class.t8"
same "$TMP/class.t1" "$TMP/class.t8" "getclass output depends on --threads"

# A consumer that closes stdout early (the usual `| head -1`) must end quietly:
# no panic, no abort, and either success or SIGPIPE. The CLI documents this as
# normal filter behaviour.
PIPE_CODE="$(set +o pipefail; "$BIN" classes --threads 4 "$APK" 2>"$TMP/pipe.stderr" | head -1 >/dev/null; printf '%s' "${PIPESTATUS[0]}")"
case "$PIPE_CODE" in
  0 | 141) ;;
  *) fail "classes on a closed pipe exited $PIPE_CODE" ;;
esac
grep -q 'panicked' "$TMP/pipe.stderr" && fail "classes panicked on a closed pipe"

# An -o file receives exactly the bytes stdout received, for every subcommand.
"$BIN" findrefs --threads 4 "$APK" string Authorization -o "$TMP/findrefs.file" >"$TMP/findrefs.stdout"
same "$TMP/findrefs.file" "$TMP/findrefs.stdout" "findrefs -o differs from stdout"
"$BIN" classes --threads 4 "--filter=$PROBE" -o "$TMP/classes.file" "$APK" >"$TMP/classes.stdout"
same "$TMP/classes.file" "$TMP/classes.stdout" "classes -o differs from stdout"
if [ "$HAVE_MANIFEST" = 1 ]; then
  "$BIN" manifest -o "$TMP/manifest.file" "$APK" >"$TMP/manifest.stdout"
  same "$TMP/manifest.file" "$TMP/manifest.stdout" "manifest -o differs from stdout"
fi
"$BIN" getclass --threads 4 -o "$TMP/class.file" "$APK" "$GETCLASS_PROBE" >"$TMP/class.stdout"
same "$TMP/class.file" "$TMP/class.stdout" "getclass -o differs from stdout"

# --debug diagnostics go to stderr and leave stdout byte-identical.
"$BIN" findrefs --debug --threads 4 "$APK" string Authorization >"$TMP/debug.stdout" 2>"$TMP/debug.stderr"
same "$TMP/debug.stdout" "$TMP/findrefs.stdout" "findrefs --debug changed stdout"
grep -q '^\[DEBUG\]' "$TMP/debug.stderr" || fail "findrefs --debug printed no diagnostics on stderr"
"$BIN" getclass --debug --threads 4 "$APK" "$GETCLASS_PROBE" >"$TMP/debug-class.stdout" 2>"$TMP/debug-class.stderr"
same "$TMP/debug-class.stdout" "$TMP/class.stdout" "getclass --debug changed stdout"
grep -q '^\[DEBUG\]' "$TMP/debug-class.stderr" || fail "getclass --debug printed no diagnostics on stderr"

# A missing class is an error with a message and exit status 1, not a panic.
if "$BIN" getclass --threads 4 "$APK" com.example.NoSuchClass7f32c9 >"$TMP/missing.stdout" 2>"$TMP/missing.stderr"; then
  fail "getclass succeeded for a class that does not exist"
fi
grep -q 'not found' "$TMP/missing.stderr" || fail "missing class did not report 'not found'"

echo "contracts ok"
