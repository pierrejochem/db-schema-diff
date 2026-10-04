"""Support ``python -m db_schema_comparer``.

``main()`` *returns* the exit code (click runs with ``standalone_mode`` off so the exception
hierarchy can be translated), so the return value is the exit status. Discarding it here made
``python -m`` print "Drift found" and exit 0 — a gate that fails open, which is the one failure
this tool exists to prevent.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
