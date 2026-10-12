"""Review fixes in carto_edge.pipeline.templates (M1 review, package A2): template state that
any input can be persisted, secrets never become template constants, and repetition counts
records, not deliveries (spec 2.3 invariants 2 and 8, ADR 0027)."""

from __future__ import annotations

import json
from pathlib import Path

from carto_edge.pipeline.templates import TemplateStore, compute_template_id

LONE_SURROGATE = chr(0xD800)


def test_a_lone_surrogate_never_breaks_template_persistence(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path, flush_interval_seconds=0)
    store.mine("sys_a", f"payment {LONE_SURROGATE} received")
    for _ in range(3):
        store.mine("sys_a", "an ordinary message here")
    store.flush()
    raw = (tmp_path / "sys_a.json").read_bytes()
    assert raw.isascii()
    json.loads(raw)
    reloaded = TemplateStore(tmp_path)
    assert reloaded.cluster_count("sys_a") == 2


def test_a_template_with_a_lone_surrogate_has_an_id() -> None:
    store = TemplateStore()
    for _ in range(3):
        text, _params = store.mine("sys_a", f"payment {LONE_SURROGATE} received")
    assert text == f"payment {LONE_SURROGATE} received"
    assert compute_template_id("sys_a", text).startswith("tpl_")
