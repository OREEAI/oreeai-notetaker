import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        stream=sys.stdout,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        force=True,
    )
