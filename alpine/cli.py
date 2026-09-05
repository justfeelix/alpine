"""Command-line entry points. Thin — each command calls a function that works on its own."""
from __future__ import annotations

import argparse


def cmd_seed(_):
    from .seed import load_seed
    for table, n in load_seed().items():
        print(f"{table:16} {n:>10,} rows")


def cmd_profile(_):
    from .profile import profile
    profile()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alpine",
                                description="Ski-resort pricing & snow analytics pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("seed", help="Load the Kaggle CSVs into raw")
    s.set_defaults(func=cmd_seed)

    pr = sub.add_parser("profile", help="Profile the raw data")
    pr.set_defaults(func=cmd_profile)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
