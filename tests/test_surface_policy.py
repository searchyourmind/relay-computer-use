import pytest

from relaycu.models import Step, Target
from relaycu.surface import Policy, Surface, SurfaceError


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:4311/", "http://localhost:4311/", "http://127.0.0.1:43110/",
    "http://127.0.0.1:4311.evil.example/", "http://127.0.0.1:4311@evil.example/",
    "http://user:secret@127.0.0.1:4311/", "http://127.0.0.1:4311/api/accounts",
    "http://127.0.0.1:4311/workspace/profile/", "http://127.0.0.1:4311/%77orkspace",
    "file:///tmp/member.txt", "data:text/html,secret", "javascript:alert(1)",
])
def test_policy_is_exact_not_prefix_match(url):
    assert not Policy().permits_url(url)


def test_query_not_used_to_expand_allowlisted_paths():
    policy = Policy()
    assert policy.permits_url(policy.origin + "/?scenario=session")
    assert policy.permits_url(policy.origin + "/workspace/profile")
    assert not policy.permits_url(policy.origin + "/unexpected?next=/workspace")


@pytest.mark.parametrize("origin", ["http://127.0.0.1:4311/", "http://user@localhost:4311", "file:///tmp", "http://localhost:80"])
def test_policy_origin_must_be_canonical(origin):
    with pytest.raises(ValueError):
        Policy(origin=origin)


@pytest.mark.asyncio
async def test_forged_artifact_cannot_expand_actions_or_frames():
    surface = Surface()
    for target in [
        Target(kind="role", role="button", name="Transfer funds", frame="Member workspace"),
        Target(kind="text", name="Open profile", frame="Member workspace"),
        Target(kind="role", role="button", name="Open profile", frame="Other frame"),
    ]:
        with pytest.raises(SurfaceError) as caught:
            await surface.act(Step(action="click", target=target), {})
        assert caught.value.code == "policy_blocked"
    with pytest.raises(SurfaceError) as caught:
        await surface.act(Step(action="fill", target=Target(kind="label", name="Amount"), input_ref="secret"), {"secret": "sensitive-123"})
    assert caught.value.code == "policy_blocked"
    assert "sensitive-123" not in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_start_does_not_launch_browser():
    surface = Surface()
    with pytest.raises(SurfaceError, match="outside the trusted"):
        await surface.start("https://example.com/")
    assert surface.page is None
