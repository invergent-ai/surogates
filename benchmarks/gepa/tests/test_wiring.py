"""GEPA is driven through its real API, offline.

The released package has already diverged from its own documentation once
here (the blog's ``OptimizeAnythingConfig`` / ``engine=`` switch never
shipped). A version bump that moves ``max_metric_calls``, renames a config
block, or changes the batch-evaluator contract must fail in this suite
rather than three hours into a paid run.

Nothing here touches the network: the reflection LM and the evaluator are
both stubs.
"""
from gepa import TimeoutStopCondition
from gepa.optimize_anything import (
    EngineConfig, GEPAConfig, ReflectionConfig, optimize_anything,
)

SEED = "# Execution discipline\nthe seed body\n"
BETTER = "# Execution discipline\nthe improved body\n"


def stub_reflection_lm(prompt):
    """GEPA reads the new candidate out of the fenced block."""
    return f"Here is a better version:\n```\n{BETTER}```"


def stub_batch_evaluator(pairs):
    """Only the improved body solves the tasks."""
    return [
        (1.0 if candidate.strip() == BETTER.strip() else 0.0,
         {"task_id": example["task_id"], "solved": candidate.strip() == BETTER.strip()})
        for candidate, example in pairs
    ]


def config(run_dir, budget):
    return GEPAConfig(
        engine=EngineConfig(
            run_dir=str(run_dir), seed=0, max_metric_calls=budget,
            parallel=False, max_workers=1, display_progress_bar=False,
        ),
        reflection=ReflectionConfig(
            reflection_lm=stub_reflection_lm, reflection_minibatch_size=2,
        ),
        stop_callbacks=[TimeoutStopCondition(60)],
    )


def test_batch_evaluator_drives_a_search_to_the_better_candidate(tmp_path):
    train = [{"task_id": f"t{i}", "role": "train"} for i in range(4)]
    val = [{"task_id": f"v{i}", "role": "val"} for i in range(4)]

    result = optimize_anything(
        seed_candidate=SEED,
        batch_evaluator=stub_batch_evaluator,
        dataset=train,
        valset=val,
        objective="Make the body better.",
        background="A prompt fragment.",
        config=config(tmp_path / "gepa", budget=8 * len(val)),
    )

    best = result.best_candidate
    if isinstance(best, dict):
        best = next(iter(best.values()))
    assert best.strip() == BETTER.strip()
    # The attributes optimize.py reads off the result. val_aggregate_scores
    # is the per-candidate val score; index 0 is always the seed, which is
    # what the reported "seed -> best" delta is built from.
    assert result.num_candidates > 1
    assert result.total_metric_calls > 0
    assert result.val_aggregate_scores[0] == 0.0
    assert result.val_aggregate_scores[result.best_idx] == 1.0
    assert len(result.val_subscores[result.best_idx]) == len(val)


def test_pairs_arrive_as_candidate_example_tuples(tmp_path):
    seen = []

    def recording_evaluator(pairs):
        seen.append(pairs)
        return stub_batch_evaluator(pairs)

    optimize_anything(
        seed_candidate=SEED,
        batch_evaluator=recording_evaluator,
        dataset=[{"task_id": "t0", "role": "train"}],
        valset=[{"task_id": "v0", "role": "val"}],
        objective="Make the body better.",
        config=config(tmp_path / "gepa", budget=4),
    )

    assert seen, "batch_evaluator was never called"
    candidate, example = seen[0][0]
    assert isinstance(candidate, str), "str seed must arrive unwrapped"
    assert example["task_id"] in {"t0", "v0"}
