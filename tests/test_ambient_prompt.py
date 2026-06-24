from surogates.ambient.prompt import build_ambient_prompt


def test_prompt_mentions_tool_and_silence():
    p = build_ambient_prompt(channel_label="#ops", task_changes=[])
    assert "mate_ambient_post" in p
    assert "#ops" in p
    assert "nothing" in p.lower() or "stay silent" in p.lower()
    assert "connected tools" in p.lower()


def test_prompt_includes_task_changes():
    p = build_ambient_prompt(
        channel_label="#ops",
        task_changes=["Task 'deploy' is now done", "Task 'migrate' is blocked"],
    )
    assert "deploy" in p
    assert "blocked" in p
