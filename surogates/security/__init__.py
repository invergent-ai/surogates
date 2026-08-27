"""Security policy data shipped with the harness.

Holds ``prompt_injection.yaml``, the detection rules loaded by
:func:`surogates.session.attachment_ingest.get_injection_detector`.
Without it the detector falls back to sample rules that flag a
markdown code fence as a delimiter attack.
"""
