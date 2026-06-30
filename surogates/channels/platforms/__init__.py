"""Built-in channel platform strategies.

Importing this package imports every built-in platform module for its
registration side effect: each module calls ``registry.register(...)`` at
import time, so ``import surogates.channels.platforms`` is all the channel
runner needs to populate the registry before it asks for the enabled
platforms.  Add a new built-in by importing its module here.
"""

from __future__ import annotations

from surogates.channels.platforms import slack as slack  # noqa: F401
from surogates.channels.platforms import telegram as telegram  # noqa: F401
