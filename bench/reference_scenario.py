"""Scenario driver that exercises the original Python ASC through its real code paths.

The original project has no CLI for the manifest and class-index features (they are
GUI-only), so this driver calls the same functions the GUI calls, in a fresh process,
so they can be timed against the equivalent `rasc` subcommands.

Usage:
    REF_ROOT=/path/to/reference python reference_scenario.py manifest <apk>
    REF_ROOT=/path/to/reference python reference_scenario.py classes  <apk> [workers]

The reference tree may use either the current droidasc/ package or the older src/
layout; the driver resolves it automatically.
"""

import importlib
import os
import sys
import time

ROOT = os.environ["REF_ROOT"]
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def reference_package() -> str:
    """The reference renamed its source package from src/ to droidasc/ when it was
    packaged; resolve whichever layout the checkout uses so the benchmark works
    against both the pre-rename and the current tree."""
    for name in ("droidasc", "src"):
        if os.path.isdir(os.path.join(ROOT, name)):
            return name
    raise SystemExit(f"no reference package under {ROOT} (expected droidasc/ or src/)")


def main() -> int:
    mode, apk = sys.argv[1], sys.argv[2]
    package = reference_package()
    started = time.perf_counter()

    if mode == "manifest":
        manifest_handler = importlib.import_module(f"{package}.asc_client.manifest_handler")

        sys.stdout.write(manifest_handler.get_manifest_xml(apk, pretty=True))
    elif mode == "classes":
        runtime = importlib.import_module(f"{package}.asc_client.gui.runtime")

        store = runtime.GuiDexStore(apk, max_workers=int(sys.argv[3]) if len(sys.argv) > 3 else 8)
        store.load()
        sys.stdout.write("\n".join(store.class_names))
        sys.stdout.write("\n")
    else:
        raise SystemExit(f"unknown mode {mode!r}")

    sys.stdout.flush()
    sys.stderr.write(f"[scenario] {mode} inner={(time.perf_counter() - started) * 1000:.0f}ms\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
