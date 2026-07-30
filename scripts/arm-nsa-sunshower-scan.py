#!/usr/bin/env python3
"""Run the frozen low-sun scan at ARM NSA C1, excluding frozen conditions."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")


def main():
    path = ROOT / "scripts" / "arm-bnf-sunshower-scan.py"
    spec = importlib.util.spec_from_file_location("cross_site", path)
    scan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scan)
    scan.OUT = ROOT / "validation" / "arm-nsa"
    scan.ASI = "nsaasiskyimageC1.a1"
    scan.SIRS = "nsasirsC1.b1"
    scan.MET = "nsametC1.b1"
    scan.LAT, scan.LON = 71.323, -156.615
    scan.surface.LAT, scan.surface.LON = scan.LAT, scan.LON
    scan.main()


if __name__ == "__main__":
    main()
