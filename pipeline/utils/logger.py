import logging
import sys
from pathlib import Path


def get_logger(name: str) -> logging.Logger:
    """
    Returns a logger with consistent formatting for the pipeline.
    Configures only the root 'pipeline' logger to avoid duplicate output
    when child loggers propagate to it.
    """
    # Configure the root pipeline logger once
    root_logger = logging.getLogger("pipeline")
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        root_logger.propagate = False

    child = logging.getLogger(name)
    child.setLevel(logging.DEBUG)   # let root filter
    return child
