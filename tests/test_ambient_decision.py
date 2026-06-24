from surogates.ambient.decision import AmbientPostDecision, gate_decision


def _d(**kw):
    base = dict(action="post", target_thread="t1", confidence=0.9, message="hi", rationale="r")
    base.update(kw)
    return AmbientPostDecision(**base)


def test_gate_allows_confident_post():
    assert gate_decision(_d(), confidence_threshold=0.7, limiter_ok=True) is True


def test_gate_blocks_low_confidence():
    assert gate_decision(_d(confidence=0.5), confidence_threshold=0.7, limiter_ok=True) is False


def test_gate_blocks_action_none():
    assert gate_decision(_d(action="none"), confidence_threshold=0.7, limiter_ok=True) is False


def test_gate_blocks_empty_message():
    assert gate_decision(_d(message="  "), confidence_threshold=0.7, limiter_ok=True) is False


def test_gate_blocks_when_limiter_denies():
    assert gate_decision(_d(), confidence_threshold=0.7, limiter_ok=False) is False
