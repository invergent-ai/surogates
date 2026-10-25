/**
 * A host's document editor answers its comments' mentions as the chat's own
 * editor does: who a comment can mention, and a comment that mentions one of
 * them, told to the host.
 */
import { describe, expect, it, vi } from "vitest";
import { editorMentions } from "../src";

const AGENT = { id: "agent:a1", name: "Document checker", email: "document-checker@agent.invalid" };

function openEditor(users = [AGENT]) {
  const setUsers = vi.fn();
  const tell = vi.fn();
  const events = editorMentions(users, tell)(() => ({ destroyEditor: vi.fn(), setUsers }));
  return { setUsers, tell, events };
}

describe("editorMentions", () => {
  it("offers who a comment can mention, and nobody to any other list the editor asks for", () => {
    const { setUsers, events } = openEditor();

    events!.onRequestUsers({ data: { c: "mention" } });
    expect(setUsers).toHaveBeenLastCalledWith({ c: "mention", users: [AGENT] });
    events!.onRequestUsers({ data: { c: "info" } });
    expect(setUsers).toHaveBeenLastCalledWith({ c: "info", users: [AGENT] });
    events!.onRequestUsers({ data: { c: "protect" } });
    expect(setUsers).toHaveBeenLastCalledWith({ c: "protect", users: [] });
  });

  it("tells the host of a comment that mentions one of them, by email in any case", () => {
    const { tell, events } = openEditor();

    events!.onRequestSendNotify({
      data: {
        emails: ["Document-Checker@agent.invalid"],
        actionLink: { action: { data: "c7" } },
        message: "+document-checker@agent.invalid check the totals",
      },
    });
    // A notice with no action link: its own id names the thread.
    events!.onRequestSendNotify({
      data: { emails: ["document-checker@agent.invalid"], actionId: "c8", message: "and the dates" },
    });

    expect(tell.mock.calls).toEqual([
      ["c7", "+document-checker@agent.invalid check the totals", ["Document-Checker@agent.invalid"]],
      ["c8", "and the dates", ["document-checker@agent.invalid"]],
    ]);
  });

  it("tells nothing of a comment that mentions no one it offers", () => {
    const { tell, events } = openEditor();

    events!.onRequestSendNotify({ data: { emails: ["ioana@bank.test"], actionId: "c9", message: "+ioana@bank.test" } });
    events!.onRequestSendNotify({});

    expect(tell).not.toHaveBeenCalled();
  });

  it("gives an editor with no one to mention no events", () => {
    expect(openEditor([]).events).toBeNull();
  });

  it("answers nothing once the editor is closed", () => {
    const events = editorMentions([AGENT], vi.fn())(() => null);

    expect(() => events!.onRequestUsers({ data: { c: "mention" } })).not.toThrow();
  });
});
