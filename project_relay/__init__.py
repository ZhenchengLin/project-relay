from __future__ import annotations

from importlib.metadata import (
    PackageNotFoundError,
    version,
)


try:
    __version__ = version(
        "project-relay"
    )

except PackageNotFoundError:
    # Source-tree fallback before installation; keep in sync with pyproject.toml.
    __version__ = "2.2.1"
