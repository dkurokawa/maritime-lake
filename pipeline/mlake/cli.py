from __future__ import annotations

import argparse
import sys

from . import build, check, ingest, od


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mlake", description="maritime-lake pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="fetch PortWatch sources into bronze")
    pi.add_argument("--full", action="store_true", help="ignore existing silver and reload everything")
    sub.add_parser("build", help="bronze -> silver -> gold")
    sub.add_parser("od", help="searoute OD matrix for the container time map (cached; needs `build` first)")
    sub.add_parser("check", help="data quality checks (non-zero exit on failure)")
    pa = sub.add_parser("all", help="ingest + build + od + check")
    pa.add_argument("--full", action="store_true")
    args = p.parse_args(argv)

    if args.cmd in ("ingest", "all"):
        ingest.run(full=args.full)
    if args.cmd in ("build", "all"):
        build.run()
    if args.cmd in ("od", "all"):
        od.run()
    if args.cmd in ("check", "all"):
        return check.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
