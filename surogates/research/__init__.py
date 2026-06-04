"""Deep-research support: evidence bank and living-outline logic.

Two small, IO-free modules — :mod:`surogates.research.memory_bank` and
:mod:`surogates.research.outline` — back the ``research_memory`` /
``research_outline`` builtin tools.  Keeping the data logic separate
from the tool handlers means the tools become thin file-IO wrappers
that are easy to test, and the logic is reusable from any future
consumer (e.g. an eval harness scoring citation accuracy).
"""
