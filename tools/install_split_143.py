"""Install/restore a pinned 142/150 -> 143 plan in the running RAM loader.

Uses the bounded sector transport with a separate, exact-image plan validator.
No reset, loader upload or write outside the reconstructed plan is exposed.
"""
import argparse
from pathlib import Path

try:
    from .prepare_split_143 import load_plan
    from .install_142 import run_plan
except ImportError:
    from prepare_split_143 import load_plan
    from install_142 import run_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    plan, original, target = load_plan(args.plan)
    run_plan(args.plan, plan, original, target, execute=args.execute, restore=args.restore)


if __name__ == "__main__":
    main()
