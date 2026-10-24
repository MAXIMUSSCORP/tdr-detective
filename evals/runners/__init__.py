"""Eval runners: one ``evals.schema.Runner`` per configuration.

* ``raw_dump``: a single Messages API call over the scenario's raw files (the baseline).
* ``skill_tools``: the Agent SDK with the triage skill and the pc-diag MCP tools.

Both build the model's input from ``EvalCase.public()`` only. Run them with
``python -m evals.run``.
"""
