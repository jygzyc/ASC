#!/usr/bin/env python3
"""Disassemble one method straight from a jar/apk's DEX, as ground truth for the
decompiler comparison. Uses the same androguard copy as the Python reference and
the same stub the reference installs so androguard loads without lxml.

Usage: disas.py <archive> <descriptor> <method-name>

Environment: REF_PYTHONPATH (default /tmp/agcheck) holds the androguard copy
that the reference implementation itself runs against.
"""
from __future__ import annotations

import os
import sys
import zipfile

for entry in reversed([p for p in os.environ.get("REF_PYTHONPATH", "/tmp/agcheck").split(":") if p]):
    sys.path.insert(0, entry)


class _DummyClass:
    pass


class _DummyModule:
    """Same stub the Python reference installs so androguard can load without lxml."""

    __path__ = []

    def __init__(self):
        self.APK = _DummyClass

    def __getattr__(self, name):
        return _DummyModule()

    def __iter__(self):
        return iter([])

    def __call__(self, *args, **kwargs):
        return _DummyModule()


sys.modules["androguard.core.apk"] = _DummyModule()
sys.modules["networkx"] = _DummyModule()
sys.modules["pygments"] = _DummyModule()
sys.modules["lxml"] = _DummyModule()

from androguard.core.dex import DEX  # noqa: E402


def main() -> int:
    archive, target_class, target_method = sys.argv[1], sys.argv[2], sys.argv[3]
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if n.startswith("classes") and n.endswith(".dex")]
        for name in sorted(names):
            data = zf.read(name)
            dex = DEX(data)
            for cls in dex.get_classes():
                if cls.get_name() != target_class:
                    continue
                print(f"# {name}: {target_class} (access {cls.get_access_flags_string()})")
                for method in cls.get_methods():
                    if method.get_name() != target_method:
                        continue
                    code = method.get_code()
                    if code is None:
                        print(f"# {method.get_name()}{method.get_descriptor()} - no code")
                        continue
                    print(f"# {method.get_access_flags_string()} {method.get_name()}{method.get_descriptor()}")
                    print(f"# registers={code.get_registers_size()} ins={code.get_ins_size()} outs={code.get_outs_size()}")
                    for ins in code.get_bc().get_instructions():
                        print(f"  {ins.get_name():24s} {ins.get_output()}")
                    return 0
    print(f"# not found: {target_class}.{target_method}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
