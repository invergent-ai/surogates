# The guest's way in to a folder's history (surogates/sandbox/local_history.py): the agent runs
# this as root with one JSON request on stdin, on a python that reads nothing of any user's
# (python3 -I), so the tree beside it is the only code it can import.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from surogates.sandbox.local_history import main  # noqa: E402

sys.exit(main())
