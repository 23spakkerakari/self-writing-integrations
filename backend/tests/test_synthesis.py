"""Synthesis tests. The pipeline is exercised with a fake agent so it runs offline; the
final test calls Claude for real and is skipped without credentials."""
import copy
import json

import pytest

from app.manifest.schema import load_manifest
from app.synthesis.agent import SynthesisResult, _extract_json, synthesize
from app.synthesis.pipeline import synthesize_and_verify
from tests.conftest import has_anthropic_credentials


def test_extract_json_tolerates_code_fences():
    text = "```json\n{\"a\": 1}\n```"
    assert _extract_json(text) == {"a": 1}
    assert _extract_json("Here you go: {\"a\": {\"b\": 2}} done") == {"a": {"b": 2}}


def test_pipeline_repairs_using_verification_feedback(registry, manifest_dict, spec_text):
    """First synthesis emits a manifest whose schema disagrees with the API (mock), the
    pipeline feeds the failure back, and the second attempt returns the good manifest."""
    broken = copy.deepcopy(manifest_dict)
    broken["endpoints"][0]["response_schema"]["properties"]["employees"]["items"]["required"].append("badge_number")
    calls = []

    def fake_synthesize(spec, name_hint, model, max_attempts, client, previous, feedback):
        calls.append({"previous": previous, "feedback": feedback})
        data = broken if previous is None else manifest_dict
        return SynthesisResult(manifest=load_manifest(data), attempts=1, transcript=["{}"])

    result = synthesize_and_verify(registry, spec_text, "bamboohr", synthesize_fn=fake_synthesize, max_rounds=3)
    assert result.rounds == 2
    assert result.report.passed
    assert result.record.status == "verified"
    assert result.record.version == "0.1.0"
    assert "badge_number" in calls[1]["feedback"]
    assert calls[1]["previous"].name == "bamboohr"


def test_pipeline_stores_rejected_when_repairs_run_out(registry, manifest_dict, spec_text):
    broken = copy.deepcopy(manifest_dict)
    broken["endpoints"][0]["response_schema"]["required"].append("missing_top_level")

    def fake_synthesize(spec, name_hint, model, max_attempts, client, previous, feedback):
        return SynthesisResult(manifest=load_manifest(broken), attempts=1, transcript=["{}"])

    result = synthesize_and_verify(registry, spec_text, "bamboohr", synthesize_fn=fake_synthesize, max_rounds=2)
    assert result.rounds == 2
    assert not result.report.passed
    assert result.record.status == "rejected"
    assert result.record.verification is not None


def test_agent_retries_on_invalid_manifest():
    """A stub Anthropic client returns an invalid manifest, then a valid one."""
    from tests.conftest import MANIFEST_PATH
    import yaml

    good = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    bad = copy.deepcopy(good)
    bad["mappings"][0]["fields"][0]["target"] = "employee_number"
    outputs = [json.dumps(bad), json.dumps(good)]

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Response:
        stop_reason = "end_turn"

        def __init__(self, text):
            self.content = [_Block(text)]

    class _Messages:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            return _Response(outputs.pop(0))

    class _Client:
        def __init__(self):
            self.messages = _Messages()

    client = _Client()
    result = synthesize("spec", "bamboohr", client=client, max_attempts=3)
    assert result.attempts == 2
    assert result.manifest.name == "bamboohr"
    second = client.messages.requests[1]["messages"]
    assert second[-1]["role"] == "user" and "employee_number" in second[-1]["content"]


@pytest.mark.skipif(not has_anthropic_credentials(), reason="needs ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN")
def test_real_synthesis_from_bamboohr_spec(registry, spec_text):
    result = synthesize_and_verify(registry, spec_text, "bamboohr", max_rounds=2)
    print(result.report.summary())
    assert result.report.passed, result.report.summary()
    manifest = load_manifest(result.record.manifest)
    assert manifest.auth.type == "basic"
    assert any(m.canonical_object == "Employee" for m in manifest.mappings)
