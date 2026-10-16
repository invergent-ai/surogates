"""Comments that call the agent: its address, the message a mention becomes, and how the model is told."""

from __future__ import annotations

import re

from surogates.office.mentions import agent_address, mention_message, mention_of, mentionable_agent

MENTION = {"path": "docs/Plan.docx", "thread": "15689582", "kind": "docx", "author": "Ana", "author_id": "user:1",
           "comment": "@WRITER@agent.invalid is Q1 right?"}


def test_the_agent_is_offered_by_an_address_no_mail_can_reach():
    assert agent_address("writer") == "writer@agent.invalid"
    assert agent_address("org/writer 2") == "org-writer-2@agent.invalid"
    # The editor matches a mention by /\B[@+][A-Z0-9._%+-]+@[A-Z0-9._-]+\.[A-Z]+\b/i alone.
    assert re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9._-]+\.[A-Z]+", agent_address("3f2a_9b-c.d"), re.IGNORECASE)
    assert mentionable_agent("writer", "Writer") == {"id": "agent:writer", "name": "Writer", "email": "writer@agent.invalid"}


def test_a_mention_becomes_a_message_naming_the_file_the_person_and_the_agent():
    content, metadata = mention_message(path="docs/Plan.docx", thread="15689582", kind="docx", author="Ana", author_id="user:1",
                                        comment="@WRITER@agent.invalid is Q1 right?", agent_id="writer", agent_name="Writer")
    assert content == "[Comment on Plan.docx] Ana: «@Writer is Q1 right?»"
    assert metadata == MENTION
    assert mention_of({"content": content, "source": "office_mention", "metadata": metadata}) == MENTION
    # A typed message's metadata is anyone's to write: without the route's source it is no mention.
    assert mention_of({"content": content, "metadata": metadata}) is None
    assert mention_of({"source": "office_mention", "metadata": {**metadata, "thread": ""}}) is None
