"""Desktop application for the schema comparer.

A presentation layer over the library, shipped as an optional extra. It maps configuration to
widgets, calls the orchestration entry points and renders a report; it never compares, normalises
or introspects anything itself.
"""

#: Slint's Python binding requires 3.12. The CLI still supports 3.11, so the GUI checks at
#: runtime rather than raising the whole project's floor.
MINIMUM_PYTHON = (3, 12)

__all__ = ["MINIMUM_PYTHON"]
