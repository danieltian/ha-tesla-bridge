"""Entry point: ``python3 -m tesla_bridge``."""

from __future__ import annotations

import asyncio
import logging
import sys

from .bridge import Bridge, run_forever
from .options import Options, OptionsError
from .state import StateStore


def main() -> int:
    try:
        options = Options.load()
    except OptionsError as err:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger(__name__).error("Invalid option: %s", err)
        return 1
    logging.basicConfig(
        level=getattr(logging, options.log_level.upper()),
        format="%(asctime)s %(levelname)s (%(name)s) %(message)s",
        stream=sys.stdout,
    )
    # aiohttp's access logs and zmq debug noise are not useful here.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    state = StateStore()
    state.load()
    asyncio.run(run_forever(Bridge(options, state)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
