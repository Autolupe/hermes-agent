"""Explicit non-code contracts for tests of workspace and lifecycle behavior."""

ARTIFACT_CONTRACT = '```acceptance-contract\ndomain: research\ntarget: artifact-file\ntier1:\n  - cmd: "true"\n    expect_exit: 0\ntier2:\n  - "temporary workspace fixture remains isolated"\ntier3: "The local fixture demonstrates its stated invariant."\n```'

ARTIFACT_DELIVERY = {
    "classification": "no_merge_expected",
    "reason": "Temporary local fixture demonstrates the requested workspace invariant.",
}
