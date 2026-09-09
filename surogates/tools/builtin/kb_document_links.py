"""Bounded navigation hints for source-grounded document relationships."""

LABELS = {
    "references": "References",
    "depends_on": "Must be used with",
    "amends": "Amends",
    "supersedes": "Replaces",
}


def format_document_links(result, *, full=False):
    status = result.get("status", "unavailable")
    if status in ("stale", "unavailable", "not_indexed"):
        message = {
            "stale": "The source publication changed. Read it again before following document links.",
            "unavailable": "Document link extraction was unavailable for this publication.",
            "not_indexed": "Document links have not been indexed for this source.",
        }[status]
        return message if full or status == "stale" else ""
    links = result.get("links", [])[:16]
    if not links and status != "partial":
        return "No explicit document links were extracted." if full else ""
    lines = ["Document links (extracted from this source):"]
    if status == "partial":
        lines.append("Extraction was partial; additional references may exist.")
    lines.append(
        "Relationships may be conditional. Read the supporting quote, surrounding source context and target source before applying them."
    )
    visible = links if full else links[:4]
    for link in visible:
        target = link["target"][:256]
        edition = link.get("target_edition")
        resolution = link.get("resolution", "unavailable")
        lines.append(
            f"- {LABELS.get(link['relation'], 'References')}: {target}"
            + (f" (edition {edition[:256]})" if edition else "")
            + f"; resolution={resolution}"
        )
        if resolution == "matched" and len(link.get("documents", [])) == 1:
            doc = link["documents"][0]
            lines.append(
                f"  Read with kb_read_page: kb_id={doc['kb_id']}, path={doc['path']}"
            )
        elif resolution == "not_found":
            lines.append(
                "  No exact target is currently indexed in this KB; do not substitute another document."
            )
        elif resolution == "needs_review":
            lines.append(
                ("  The extracted edition matches a document identifier; verify how the source uses it. "
                 if link.get("resolution_basis") == "identifier_in_edition"
                 else "  Possible title matches to inspect. ")
                + "No target or edition has been selected. "
                "Read the candidate source and verify its identity, edition and the supporting reference before using it."
            )
            for doc in link.get("documents", [])[: 5 if full else 2]:
                lines.append(
                    f"  Candidate {doc['filename'][:256]} (identity={doc.get('identity_status', 'unknown')}): "
                    f"kb_read_page kb_id={doc['kb_id']}, path={doc['path']}"
                )
            if link.get("truncated"):
                lines.append(
                    "  Candidate list is incomplete; a unique target cannot be established from it."
                )
        elif resolution != "matched":
            lines.append(
                "  No target was selected. Resolve the identity/edition uncertainty before applying it."
            )
        if full:
            evidence = link["evidence"]
            lines.append(
                f"  Source page={evidence.get('page')}; characters={evidence['start']}-{evidence['end']}"
            )
            # The extractor bounds the original quotation. Do not trim a
            # trailing applicability condition when rendering the evidence.
            lines.append("  Supporting quote (source text): " + evidence["quote"])
    if not full:
        lines.append(
            "Read this source with context='links' for the supporting quotations and remaining links (omit passage_id/pages/offset)."
        )
    elif result.get("truncated"):
        lines.append("Additional extracted links were omitted from this response.")
    return "\n".join(lines)
