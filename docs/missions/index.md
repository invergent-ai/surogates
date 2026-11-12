# Missions: orchestrated, rubric-judged objectives

A **mission** is a long-running objective with a written rubric. The coordinator starts agents with the `agent` tool, and an LLM judge keeps grading the work against the rubric: it sends the coordinator back for another round while the rubric is not met, and ends the mission when it is.

Use a mission when "done" is criterion-driven and the user wants to set the bar up front (`accuracy >= 0.8`, `coverage >= 95%`, `all critical TODOs resolved`). Without one, the coordinator's own judgement decides when the work is finished.

## Starting a mission

Open a chat with your agent and send:

```
/mission Train a small classifier on the customer-feedback dataset. Generate training data if needed; iterate until the eval metric is met.

Rubric:
A verifier agent must run the eval suite and report the accuracy it measured. Satisfied when accuracy >= 0.85.
```

That is the whole API. The agent becomes the mission's coordinator, the rubric is kept on the session, and a kickoff message tells the agent to break the work up.

The slash command's other forms:

| Form | Effect |
|---|---|
| `/mission` or `/mission status` | Show the current mission's status, iteration, latest verdict |
| `/mission pause [reason]` | Pause the evaluator loop. Agents that run keep running. |
| `/mission resume` | Resume a paused mission |
| `/mission cancel [reason]` | Cancel without touching the agents: they finish whatever they are on |
| `/mission cancel --cascade [reason]` | Cancel and stop every running agent of the mission, with the agents it started |
| `/mission accept` | Apply a proposed rubric refinement and resume (see [Rubric refinement](#rubric-refinement)) |
| `/mission reject [reason]` | Decline the refinement; the mission ends with the verdict that was held back |

A chat has at most one `/mission` or `/goal` at a time: they share an evaluator loop.

## The mission's agents

Every agent the coordinator starts with `agent` while the mission is active is one of the mission's agents: it carries the mission's id in its `agent_dispatch` config. The agents those agents start are not counted on their own, but their tokens count towards the mission's budget through them. An agent the coordinator started before the mission, or after it ended, is not the mission's.

## Rubric refinement

When the evaluator is about to end a mission `blocked` or `failed` because the **rubric itself** is unreachable as written (it names an artifact the work has shown cannot exist, contradicts itself, or asks for something that was never part of the description), it may propose a replacement rubric instead of ending the mission. The mission pauses with `paused_reason=awaiting_refinement`, the coordinator relays the proposal, and the mission waits. No judge calls run while it waits.

Three parties hold one power each, so a *pivot* stays distinguishable from *drift*:

| Party | Holds | Cannot |
|---|---|---|
| Judge | authorship of the proposed rubric | apply it |
| Coordinator | relay of the proposal to the user | author or apply it |
| User | authority to apply | author it |

`/mission accept` commits the rubric recorded in the `mission.refinement_proposed` event. It takes no rubric argument, so nothing the coordinator writes can change what is committed.

The mission's `description` is never amended (it is the standing intent), and `iteration` is not reset, so a pivot does not refill the allowance (`/mission budget` does that). A mission accepts at most **two** amendments; past that, `blocked` is `blocked`.

## Mission states

| Status | Meaning |
|---|---|
| `active` | The judge grades new evidence as it arrives |
| `paused` | Evaluator suspended; agents continue. `/mission resume` reactivates. A `paused_reason` of `awaiting_refinement` means it waits on `/mission accept` or `/mission reject` instead. |
| `satisfied` | Terminal: rubric met |
| `blocked` | Terminal: the judge says the rubric needs external input the agent has not asked for |
| `failed` | Terminal: the judge says the rubric cannot be reached from where things stand |
| `cancelled` | Terminal: the user cancelled |
| `max_iterations_reached` | Terminal: hit the 20-iteration cap without a `satisfied` verdict |

## When the judge runs

The judge does **not** run after every agent response: that would cost a call to grade unchanged work. It runs only when:

1. One of the mission's agents ended (completed, failed or was stopped) since the last verdict. There is new evidence to grade. **This is the normal path.**
2. The coordinator writes `[[mission-complete]]` on its own line: an explicit "look now" from the agent.

Either way it waits for the work to settle: while any of the mission's agents runs, the judge holds off and no iteration is spent. A 30-second rate limit per mission keeps a burst of finishing agents from triggering a flurry of judge calls.

The judge sees the completed mission agents (each with its description, its agent type and its report) and the ones still running. A `satisfied` verdict while the mission has agents and none of them completed is turned into `needs_revision`: a rubric is met by work that was done, not by the coordinator's word. A mission that started no agents is exempt: there is nothing to check the verdict against.

## How to design a rubric the judge can answer

The judge cannot grade prose claims. It needs evidence in a report. The pattern that works:

1. The coordinator starts the agents that do the work (research, training, generation, fixing).
2. Once they have finished, it starts a **verifier agent** whose job is to measure what the rubric names and report it plainly ("accuracy: 0.87 on the 500-item eval set").
3. When the verifier ends, the judge reads its report and grades against it.

So write rubrics in terms of something a verifier can measure and report:

- ✅ "Satisfied when a verifier reports accuracy >= 0.85"
- ✅ "Satisfied when a verifier reports coverage of at least 95%"
- ✅ "Satisfied when a verifier reports no failing tests"
- ❌ "Satisfied when the agent says it's done" (the judge ignores claims without a report behind them)
- ❌ "Satisfied when the code is clean" (nothing to measure)

## What happens when the judge says "needs revision"

The judge picks one of four verdicts: `satisfied`, `needs_revision`, `blocked`, `failed`. All but `needs_revision` end the mission. `needs_revision` means "keep going":

- The mission's iteration counter goes up.
- The judge's feedback reaches the coordinator as a continuation message ("here is what is missing").
- The coordinator wakes, reads the feedback and decides what to do next, usually starting agents to correct the work.
- After 20 iterations without a `satisfied` verdict, the mission ends as `max_iterations_reached`.

If the coordinator decides the rubric cannot be met (contradictory criteria, missing data), it says so in its answer and writes `[[mission-complete]]`; the judge reads that with the agents' reports and returns `failed` or `blocked`.

## Pausing, resuming, cancelling

- **Pause** stops the evaluator from grading new evidence. Agents that run keep running and reporting; the judge just does not react. Useful when you want to look at the work without it changing under you.
- **Resume** turns the evaluator back on and wakes the coordinator, so any pending continuation is read.
- **Cancel** ends the mission. Without `--cascade`, running agents finish (their reports are no longer graded). With `--cascade`, every running agent of the mission is stopped, with the agents it started: use it when the whole direction is wrong.

If the judge fails to return parseable output three times in a row, the mission pauses itself with the reason `evaluator parse failure` so you can look into it; a single recoverable failure pauses nothing. A verdict with no explanation counts as a failure, so a judge cannot pass a mission without saying why.

## Where to watch a mission run

Each agent shows missions in two places:

- A **Missions** panel in the sidebar, listing every active or paused mission. Click a row to open the dashboard.
- A **mission dashboard** that shows the rubric, the current iteration, the latest verdict, and the mission's agents with their status and links into each agent's session. Active missions refresh every 5 seconds; finished missions stop.

The dashboard is the place to watch a mission in flight; the chat is the place to talk to the coordinator about it.

## See also

- [Sub-Agents](../sub-agents/index.md): the `agent` tool and the agent types it starts.
- [Tools](../tools/index.md): the agent family's parameters.
- [Slash commands](../commands/index.md): `/mission` and `/goal`.
