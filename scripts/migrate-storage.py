#!/usr/bin/env python3
"""Copy legacy Docker volumes into data/; native restore is start-native migrate."""

import argparse
from native.config import ROOT
from native.bootstrap import ensure_python
from native.migration import check_legacy, migrate_storage
from native.processes import RuntimeLock

if __name__ == "__main__":
    ensure_python()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    with RuntimeLock(ROOT / "data/native/runner.lock"):
        if args.check:
            check_legacy(ROOT)
        else:
            migrate_storage(ROOT)
