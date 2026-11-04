import argparse
import logging

from aiohttp import web

from .config import load_config
from .server import make_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="tracker", description="Art-Net light tracker")
    ap.add_argument("--config", default="config/fixtures.json")
    ap.add_argument("--web-host", default="127.0.0.1")
    ap.add_argument("--web-port", type=int, default=8080)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    web.run_app(make_app(load_config(args.config)), host=args.web_host, port=args.web_port)


if __name__ == "__main__":
    main()
