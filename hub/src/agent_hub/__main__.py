"""Run the hub: `agent-hub --host 0.0.0.0 --port 8790`."""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-hub")
    parser.add_argument("--host", default=os.getenv("AGENT_HUB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENT_HUB_PORT", "8790")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
