"""Mission layer — orchestrated, rubric-judged goals.

A Mission is a long-running, durable, multi-worker objective attached to
a chat (coordinator) session. The mission's rubric is graded by an LLM
judge fired on two triggers:

1. A mission-linked task transitions to a terminal state (``done``,
   ``failed``, ``cancelled``).
2. The coordinator's no-tool-call response carries the explicit
   ``[[mission-complete]]`` marker on its own line.

This package contains the Pydantic domain model, async CRUD store,
``/mission`` slash-command parser + handlers, and the evaluator.
"""
