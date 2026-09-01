"""`python -m textcast` runs the build worker.

There is no command line beyond this. Adding an article, choosing a voice,
summarising and building are all done in the app, which is the only interface
worth keeping consistent.
"""

from __future__ import annotations

from .jobs import main

if __name__ == "__main__":
    raise SystemExit(main())
