# Spec Document Reviewer Prompt Template

Use this template when dispatching a spec document reviewer sub-agent via `delegate_task`.

**Purpose:** Verify the spec is complete, consistent, and ready for implementation planning.

**Dispatch after:** Spec document is written to `.surogate/specs/`.

```
delegate_task(
  goal="Review spec document",
  context="""
    You are a spec document reviewer. Verify this spec is complete and ready for planning.

    **Spec to review:** <SPEC_FILE_PATH>

    ## What to Check

    | Category     | What to Look For                                                  |
    |--------------|-------------------------------------------------------------------|
    | Completeness | TODOs, placeholders, "TBD", incomplete sections                   |
    | Consistency  | Internal contradictions, conflicting requirements                 |
    | Clarity      | Requirements ambiguous enough to cause building the wrong thing   |
    | Scope        | Focused enough for a single plan — not covering many subsystems   |
    | YAGNI        | Unrequested features, over-engineering                            |

    ## Calibration

    Only flag issues that would cause real problems during implementation planning.
    A missing section, a contradiction, or a requirement so ambiguous it could be
    interpreted two ways — those are issues. Minor wording, stylistic preferences,
    and "sections less detailed than others" are not.

    Approve unless there are serious gaps that would lead to a flawed plan.

    ## Output Format

    ## Spec Review

    **Status:** Approved | Issues Found

    **Issues (if any):**
    - [Section X]: [specific issue] — [why it matters for planning]

    **Recommendations (advisory, do not block approval):**
    - [suggestions for improvement]
  """,
)
```

**Reviewer returns:** Status, Issues (if any), Recommendations.
