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


def cmd_weather(args):
    from .weather import DEFAULT_END, DEFAULT_START, load_weather
    n = load_weather(start=args.start or DEFAULT_START, end=args.end or DEFAULT_END)
    print(f"\nraw.weather      {n:>10,} rows")


def cmd_model(args):
    from .model import run
    run(save_to=args.out)


def cmd_publish(args):
    from pathlib import Path
    from .publish import publish
    publish(out=Path(args.out))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alpine",
                                description="Ski-resort pricing & snow analytics pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("seed", help="Load the Kaggle CSVs into raw")
    s.set_defaults(func=cmd_seed)

    pr = sub.add_parser("profile", help="Profile the raw data")
    pr.set_defaults(func=cmd_profile)

    w = sub.add_parser("weather", help="Fetch daily weather from Open-Meteo")
    w.add_argument("--start", default=None, help="YYYY-MM-DD (default 2022-01-01)")
    w.add_argument("--end", default=None, help="YYYY-MM-DD (default 2022-12-31)")
    w.set_defaults(func=cmd_weather)

    m = sub.add_parser("model", help="Baselines, models, and the snow ablation")
    m.add_argument("--out", default="models/metrics.json")
    m.set_defaults(func=cmd_model)

    pub = sub.add_parser("publish", help="Export the marts to site/data.json")
    pub.add_argument("--out", default="site/data.json")
    pub.set_defaults(func=cmd_publish)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
