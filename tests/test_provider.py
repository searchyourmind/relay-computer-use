"""Model boundary tests; mocked replies are not evidence of LLM discovery."""

import hashlib
import json

import httpx
import pytest

from relaycu import provider
from relaycu.models import Control, Observation, Target


CHECKPOINT = {"kind": "role", "role": "heading", "name": "Savings account", "frame": "Member workspace"}
MEMBER = Target(kind="label", name="Member ID", frame="Member workspace")
SEARCH = Target(kind="role", role="button", name="Find member", frame="Member workspace")
PROFILE = Target(kind="role", role="link", name="Open profile", frame="Member workspace")
FILL_CHOICE = "fill Member ID in Member workspace using member_id"
SEARCH_CHOICE = "click Find member in Member workspace"


def observation(*controls):
    return Observation(route="/workspace", headings=["Find a member"], controls=list(controls))


def field(control_id="current-field", target=MEMBER):
    return Control(id=control_id, target=target, actions=["fill"])


def button(control_id="current-button", target=SEARCH):
    return Control(id=control_id, target=target, actions=["click"])


def install_model(monkeypatch, reply):
    """Keep real httpx response/status handling, replacing only network I/O."""
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "test-model", "digest": "test-digest"}]})
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        result = reply(body)
        if isinstance(result, httpx.Response):
            return result
        content = result if isinstance(result, str) else json.dumps(result)
        return httpx.Response(200, json={"message": {"content": content}, "eval_count": 17})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    return requests


def choose(body, predicate, reason="navigate"):
    payload = json.loads(body["messages"][-1]["content"])
    candidate = next(item for item in payload["available_actions"] if predicate(item))
    return {"choice": candidate["choice"], "reason": reason}


async def test_choice_resolves_current_observation_id_not_previous_observation_alias(monkeypatch):
    def reply(body):
        return choose(body, lambda item: item["action"] == "fill", "locate_record")

    install_model(monkeypatch, reply)
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test/")
    first = await planner.decide("Find a member", observation(field("first-nonce")), [], ["member_id"], CHECKPOINT)
    second = await planner.decide("Find a member", observation(field("new-nonce")), [], ["member_id"], CHECKPOINT)
    assert (first.control, second.control) == ("first-nonce", "new-nonce")
    assert first.action == second.action == "fill"
    assert first.input_ref == second.input_ref == "member_id"


async def test_prompt_and_descriptive_choice_are_stable_when_controls_reorder_and_ids_change(monkeypatch):
    menus = []
    model_requests = []

    def reply(body):
        model_requests.append({"messages": body["messages"], "format": body["format"]})
        payload = json.loads(body["messages"][-1]["content"])
        choices = [item["choice"] for item in payload["available_actions"]]
        menus.append(choices)
        assert FILL_CHOICE in body["format"]["properties"]["choice"]["enum"]
        return {"choice": FILL_CHOICE, "reason": "locate_record"}

    install_model(monkeypatch, reply)
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    first = await planner.decide("Find a member", observation(field("field-one"), button("button-one")), [], ["member_id"], CHECKPOINT)
    second = await planner.decide("Find a member", observation(button("button-two"), field("field-two")), [], ["member_id"], CHECKPOINT)
    assert menus == [[FILL_CHOICE, SEARCH_CHOICE], [FILL_CHOICE, SEARCH_CHOICE]]
    assert model_requests[0] == model_requests[1]
    assert (first.control, second.control) == ("field-one", "field-two")
    assert first.action == second.action == "fill"
    assert first.input_ref == second.input_ref == "member_id"


async def test_duplicate_descriptive_choice_fails_before_model_call(monkeypatch):
    requests = install_model(monkeypatch, lambda body: {"choice": FILL_CHOICE, "reason": "locate_record"})
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    with pytest.raises(provider.ModelError, match="Ambiguous observed affordances"):
        await planner.decide("Find a member", observation(field("one"), field("two")), [], ["member_id"], CHECKPOINT)
    assert requests == []
    assert planner.calls == 0


async def test_click_candidate_cannot_be_reinterpreted_as_fill(monkeypatch):
    install_model(monkeypatch, lambda body: choose(body, lambda item: item["label"] == "Open profile"))
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    decision = await planner.decide("Read savings balance", observation(button("profile-link", PROFILE)), [], ["member_id"], CHECKPOINT)
    assert decision.action == "click"
    assert decision.control == "profile-link"
    assert decision.input_ref is None


async def test_parameter_choice_preserves_declared_reference_and_never_supplies_literal_value(monkeypatch):
    install_model(monkeypatch, lambda body: choose(body, lambda item: item["input_ref"] == "alternate_member_id", "locate_record"))
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    decision = await planner.decide("Find a member", observation(field()), [], ["member_id", "alternate_member_id"], CHECKPOINT)
    assert decision.input_ref == "alternate_member_id"
    assert decision.action == "fill"
    assert set(decision.model_dump()) == {"action", "control", "input_ref", "reason"}


async def test_completed_fill_survives_nonce_change_but_does_not_mark_another_frame(monkeypatch):
    different_frame = MEMBER.model_copy(update={"frame": "Other workspace"})
    history = [{"action": "fill", "target": MEMBER.model_dump(), "input_ref": "member_id"}]

    def reply(body):
        payload = json.loads(body["messages"][-1]["content"])
        fields = [item for item in payload["available_actions"] if item["action"] == "fill"]
        assert [item["already_filled"] for item in fields] == [True, False]
        return choose(body, lambda item: item["action"] == "click")

    install_model(monkeypatch, reply)
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    decision = await planner.decide("Find a member", observation(field("new-nonce"), field("other", different_frame), button()), history, ["member_id"], CHECKPOINT)
    assert decision.action == "click"


async def test_prompt_projects_history_without_input_values_or_previous_control_ids(monkeypatch):
    history = [{"action": "fill", "target": MEMBER.model_dump(), "input_ref": "member_id",
                "value": "PRIVATE-MEMBER-VALUE", "control": "obsolete-nonce",
                "outputs": {"balance": "PRIVATE-BALANCE"}}]

    def reply(body):
        serialized = json.dumps(body)
        assert "PRIVATE-MEMBER-VALUE" not in serialized
        assert "PRIVATE-BALANCE" not in serialized
        assert "obsolete-nonce" not in serialized
        assert "current-field" not in serialized
        assert "current-button" not in serialized
        assert "member_id" in serialized
        return choose(body, lambda item: item["action"] == "click")

    install_model(monkeypatch, reply)
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    await planner.decide("Find the supplied member", observation(field(), button()), history, ["member_id"], CHECKPOINT)


@pytest.mark.parametrize("content", [
    "not JSON PRIVATE-REPLY",
    "[]",
    "null",
    '{"choice":"missing-control","reason":"navigate"}',
    json.dumps({"choice": FILL_CHOICE, "reason": "navigate", "literal": "PRIVATE-REPLY"}),
    json.dumps({"choice": FILL_CHOICE}),
    json.dumps({"choice": FILL_CHOICE, "reason": "PRIVATE-REPLY"}),
    json.dumps({"choice": [FILL_CHOICE], "reason": "navigate"}),
])
async def test_malformed_or_invented_choice_is_rejected_without_exposing_reply(monkeypatch, content):
    install_model(monkeypatch, lambda body: content)
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    with pytest.raises(provider.ModelError) as error:
        await planner.decide("Find a member", observation(field()), [], ["member_id"], CHECKPOINT)
    assert str(error.value) == "Model unavailable or returned an invalid decision"
    assert "PRIVATE-REPLY" not in str(error.value)
    assert planner.calls == 1
    assert planner.last_metrics == {}


async def test_transport_failure_is_sanitized_and_counted_as_attempt(monkeypatch):
    install_model(monkeypatch, lambda body: httpx.Response(503, text="PRIVATE-PROVIDER-ERROR"))
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    with pytest.raises(provider.ModelError) as error:
        await planner.decide("Find a member", observation(field()), [], ["member_id"], CHECKPOINT)
    assert "PRIVATE-PROVIDER-ERROR" not in str(error.value)
    assert "model.test" not in str(error.value)
    assert planner.calls == 1


async def test_metrics_count_real_transport_calls_and_hash_response_without_persisting_it(monkeypatch):
    contents = []

    def reply(body):
        content = json.dumps(choose(body, lambda item: item["action"] == "click"))
        contents.append(content)
        return content

    requests = install_model(monkeypatch, reply)
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    for _ in range(2):
        await planner.decide("PRIVATE-GOAL", observation(button()), [], ["member_id"], CHECKPOINT)
    assert planner.calls == 2
    assert [request.url.path for request in requests] == ["/api/tags", "/api/chat", "/api/chat"]
    assert planner.last_metrics["digest"] == "test-digest"
    assert planner.last_metrics["output_tokens"] == 17
    assert planner.last_metrics["response_sha256"] == hashlib.sha256(contents[-1].encode()).hexdigest()
    assert "PRIVATE-GOAL" not in json.dumps(planner.last_metrics)
    assert contents[-1] not in json.dumps(planner.last_metrics)


async def test_empty_observation_can_escalate_without_inventing_a_control(monkeypatch):
    install_model(monkeypatch, lambda body: {"choice": "escalate", "reason": "blocked"})
    planner = provider.OllamaPlanner(model="test-model", host="http://model.test")
    decision = await planner.decide("Find a member", observation(), [], ["member_id"], CHECKPOINT)
    assert decision.action == "escalate"
    assert decision.control is None
    assert decision.input_ref is None
