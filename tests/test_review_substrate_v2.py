import json

from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        body = {
            "verdict": "PASS",
            "findings": [],
            "summary": f"reviewed by {kwargs['model']}",
        }
        return {"content": json.dumps(body)}, {"prompt_tokens": 10, "completion_tokens": 5}


class FencedArrayLLM:
    def chat(self, **kwargs):
        body = (
            "Here is the review:\n"
            "```json\n"
            "[{\"verdict\":\"FAIL\",\"severity\":\"critical\",\"item\":\"x\",\"evidence\":\"e\",\"recommendation\":\"r\"}]\n"
            "```"
        )
        return {"content": body}, {"prompt_tokens": 10, "completion_tokens": 5}


def test_review_substrate_treats_duplicate_models_as_independent_slots(tmp_path):
    llm = FakeLLM()
    slots = [
        ReviewSlot(slot_id="triad_a", model="same/model", effort="high"),
        ReviewSlot(slot_id="triad_b", model="same/model", effort="high"),
    ]
    result = run_review_request(
        ReviewRequest(surface="task_acceptance", goal="verify final claim", subject="done", task_id="task-1"),
        slots=slots,
        drive_root=tmp_path,
        llm=llm,
    )

    assert result.aggregate_signal == "PASS"
    assert [actor["slot_id"] for actor in result.actors] == ["triad_a", "triad_b"]
    assert [call["model"] for call in llm.calls] == ["same/model", "same/model"]
    for actor in result.actors:
        assert actor["prompt_ref"]["manifest_ref"]["path"]
        assert actor["response_ref"]["manifest_ref"]["path"]


def test_review_substrate_reports_no_slots_as_degraded(tmp_path):
    result = run_review_request(
        ReviewRequest(surface="plan", goal="review plan", task_id="task-1"),
        slots=[],
        drive_root=tmp_path,
        llm=FakeLLM(),
    )

    assert result.aggregate_signal == "DEGRADED"
    assert result.degraded is True
    assert "no_review_slots" in result.degraded_reasons


def test_review_substrate_emits_usage_when_context_supplied(tmp_path):
    class Ctx:
        task_id = "task-usage"
        pending_events = []

    ctx = Ctx()
    result = run_review_request(
        ReviewRequest(surface="task_acceptance", goal="review claim", task_id="task-usage"),
        slots=[ReviewSlot(slot_id="slot_a", model="same/model")],
        drive_root=tmp_path,
        llm=FakeLLM(),
        usage_ctx=ctx,
    )

    assert result.aggregate_signal == "PASS"
    usage_events = [event for event in ctx.pending_events if event.get("type") == "llm_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["task_id"] == "task-usage"
    assert usage_events[0]["source"] == "review_substrate:task_acceptance"
    assert usage_events[0]["slot_id"] == "slot_a"


def test_review_substrate_parses_fenced_json_array_findings(tmp_path):
    result = run_review_request(
        ReviewRequest(surface="scope", goal="review diff", task_id="task-json-array"),
        slots=[ReviewSlot(slot_id="slot_a", model="same/model")],
        drive_root=tmp_path,
        llm=FencedArrayLLM(),
    )

    assert result.aggregate_signal == "FAIL"
    assert result.parsed_findings[0]["item"] == "x"
    assert result.actors[0]["parsed"][0]["verdict"] == "FAIL"
