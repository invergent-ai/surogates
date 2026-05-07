"""Minimal read-only models for ops DB tables we access at runtime.

These mirror the writer-side schema in surogate-ops but expose only
the columns the KB tools need. They're intentionally lightweight --
no relationships, no event hooks, no validators -- because we just
read rows and shape the output for the LLM.

Schema definitions of record live in surogate-ops; this module is
purely an access pattern.
"""

from __future__ import annotations

from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class OpsBase(DeclarativeBase):
    """Separate Base from the surogates app DB models.

    Two metadata graphs in one process is fine -- they bind to
    different engines and never touch each other.
    """
    pass


class OpsKnowledgeBase(OpsBase):
    """Mirror of surogate-ops ``knowledge_bases`` table.

    Stripped to the columns kb_list_pages / kb_read_page actually
    need: identity (id, name) for routing, hub_ref for fetching
    content, status to surface stale/error states to the LLM.
    """
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(sa.String(36))
    name: Mapped[str] = mapped_column(sa.String(255))
    display_name: Mapped[str] = mapped_column(sa.String(255))
    description: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String(32))
    hub_ref: Mapped[Optional[str]] = mapped_column(sa.String(512))


class OpsKBWikiPage(OpsBase):
    """Mirror of surogate-ops ``kb_wiki_pages`` table.

    The KB compile pipeline writes one row per generated wiki page.
    Tools need path (the address), page_type (so we can format the
    tree by category), title (for human-readable listing), and
    size_bytes (so we can warn the LLM about huge pages).
    """
    __tablename__ = "kb_wiki_pages"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(sa.String(36))
    path: Mapped[str] = mapped_column(sa.String(512))
    page_type: Mapped[str] = mapped_column(sa.String(32))
    title: Mapped[str] = mapped_column(sa.String(512))
    size_bytes: Mapped[int] = mapped_column(sa.Integer)


# M2M: agent_knowledge_bases. Two-column join table; we model it as a
# Core ``Table`` because the only operations we run against it are
# joins and ``WHERE agent_id = :id``, never ORM hydration.
agent_knowledge_bases = sa.Table(
    "agent_knowledge_bases",
    OpsBase.metadata,
    sa.Column("agent_id", sa.String(36), primary_key=True),
    sa.Column("kb_id", sa.String(36), primary_key=True),
)
