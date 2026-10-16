"""Rank the files in a git repository by security-review risk."""

__version__ = "0.1.0"

import logging

from .errors import HeatmapError
from .pipeline import ScanOptions, ScanResult, scan
from .scoring import FileScore

# Library callers opt in to progress messages; the CLI attaches a real handler.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = ["FileScore", "HeatmapError", "ScanOptions", "ScanResult", "scan", "__version__"]
