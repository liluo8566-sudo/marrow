"""ADR-0004 sync guard: every model pinned in the pipeline must prefix-match
some config [transcript].worker_models entry.

If diary moves to opus, or prompt-lint's MODEL is renamed, the headless
signal silently breaks (a worker run gets archived as a real session, or
vice versa). This test fails loudly the moment that drift happens.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from marrow import transcript

_REPO = Path(__file__).resolve().parents[1]
_LINT = Path.home() / ".claude" / "hooks" / "prompt-lint.py"


def _matches(model: str, workers: list[str]) -> bool:
    return any(model.startswith(w) for w in workers)


def _config_default_tiers() -> dict:
    import tomllib
    with (_REPO / "marrow" / "config.default.toml").open("rb") as f:
        return tomllib.load(f).get("tiers", {})


def test_worker_models_loaded_and_nonempty():
    w = transcript.worker_models()
    assert isinstance(w, list) and w
    assert all(isinstance(x, str) and x for x in w)


def test_pipeline_pinned_models_subset_of_worker_models():
    workers = transcript.worker_models()
    tiers = _config_default_tiers()
    # diary.py uses tier "cheap" (map/stitch) and "mid" (write); both are
    # pipeline-pinned worker models and MUST be in the worker set.
    for tier in ("cheap", "mid"):
        model = tiers[tier]
        assert _matches(model, workers), (
            f"config tiers.{tier}={model} not covered by "
            f"worker_models={workers}; ADR-0004 signal would break")


def test_prompt_lint_model_subset_of_worker_models():
    src = _LINT.read_text()
    m = re.search(r'^MODEL\s*=\s*(.+)$', src, re.M)
    assert m, "could not find MODEL pin in prompt-lint.py"
    model = ast.literal_eval(m.group(1).strip())
    assert _matches(model, transcript.worker_models()), (
        f"prompt-lint MODEL={model} not covered by worker_models; "
        f"ADR-0004 signal would break")
