"""Enable ``python -m shiplog`` as an alias for the ``shiplog`` console script.

The installed ``shiplog`` entry point is the primary interface, but agents, CI
jobs, and venvs often reach for ``python -m shiplog`` (no PATH shim required).
This keeps that invocation working and identical to the console script.
"""

from __future__ import annotations

from shiplog.cli import app

if __name__ == "__main__":  # pragma: no cover
    app()
