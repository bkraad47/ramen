import argparse
import sys
from pathlib import Path

from . import __version__
from .log import log
from .rpc import Server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ramen_runtime")
    ap.add_argument("--bucket", required=True, help="group bucket dir containing mcp/")
    ap.add_argument("--load", action="store_true", help="run runtime.load before serving")
    a = ap.parse_args(argv)
    log("info", "runtime start", version=__version__, bucket=a.bucket)
    Server(Path(a.bucket), load_on_start=a.load).serve(sys.stdin, sys.stdout)
    log("info", "runtime exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
