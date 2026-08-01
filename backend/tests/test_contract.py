"""Contract tests.

These exist to protect the frontend. Your partner builds against the shapes asserted
here, so a change that breaks one of these tests is a change that breaks their build —
which is exactly the signal we want before it reaches them.

The harness test is the important one: it proves the full event stream a real meeting
produces can be exercised with no network, no Recall key, and no Google account.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store


@pytest.fixture(autouse=True)
def _reset_state():
    store.reset()
    yield
    store.reset()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


# --- Config surface ---------------------------------------------------------------


def test_health_never_raises(client: TestClient) -> None:
    """Health must answer even when a provider is misconfigured.

    A health endpoint that 500s tells you nothing, so a broken runtime reports
    `degraded` with the reason instead of raising.
    """
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded")

    # The harness is the no-credentials path and must always be available.
    harness = next(p for p in body["providers"] if p["name"] == "harness")
    assert harness["configured"] is True

    if body["status"] == "ok":
        names = {p["name"] for p in body["providers"]}
        assert {"recall", "voice", "harness"} <= names


def test_people_seeded_and_crud(client: TestClient) -> None:
    listed = client.get("/api/people").json()
    assert listed["next_cursor"] is None
    assert len(listed["items"]) == 3
    assert {p["display_name"] for p in listed["items"]} == {
        "Sarah Chen",
        "Marcus Webb",
        "Priya Raman",
    }

    created = client.post(
        "/api/people", json={"display_name": "Dana Cole", "role": "CFO"}
    )
    assert created.status_code == 201
    person = created.json()
    assert person["id"].startswith("prs_")
    assert person["aliases"] == []

    patched = client.patch(f"/api/people/{person['id']}", json={"role": "Interim CFO"})
    assert patched.json()["role"] == "Interim CFO"
    # An omitted field must not be blanked.
    assert patched.json()["display_name"] == "Dana Cole"

    assert client.delete(f"/api/people/{person['id']}").status_code == 200
    assert client.get(f"/api/people/{person['id']}").status_code == 404


def test_error_envelope_shape(client: TestClient) -> None:
    """Every error is `{"error": {code, message, detail}}` — one parser on the frontend."""
    body = client.get("/api/people/prs_nope").json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "not_found"
    assert "prs_nope" in body["error"]["message"]


def test_validation_error_uses_envelope(client: TestClient) -> None:
    body = client.post("/api/people", json={}).json()
    assert body["error"]["code"] == "validation_error"
    assert "errors" in body["error"]["detail"]


def test_integration_stub_flags_demo(client: TestClient) -> None:
    items = client.get("/api/integrations").json()["items"]
    assert all(i["is_stub"] is True for i in items), "UI badge depends on is_stub"

    connected = client.post("/api/integrations/notion/connect").json()
    assert connected["status"] == "connected"
    assert connected["account_label"]
    assert connected["connected_at"] is not None

    disconnected = client.delete("/api/integrations/notion").json()
    assert disconnected["status"] == "available"
    assert disconnected["account_label"] is None


def test_settings_patch_is_partial(client: TestClient) -> None:
    """Omitted keys must survive a PATCH.

    Deliberately does not assert the default wake word — that is a product decision that
    changes, and pinning it here would make an intentional change look like a regression.
    """
    original = client.get("/api/settings").json()
    assert original["wake_word"], "a wake word must always be configured"

    patched = client.patch("/api/settings", json={"autonomy": "propose"}).json()
    assert patched["autonomy"] == "propose"
    # Untouched keys survive.
    assert patched["wake_word"] == original["wake_word"]
    assert patched["interjection"] == original["interjection"]
    assert patched["voice"] == original["voice"]


def test_timestamps_are_millisecond_utc(client: TestClient) -> None:
    """The contract promises `...THH:MM:SS.mmmZ`. The frontend parses on that."""
    created_at = client.get("/api/people").json()["items"][0]["created_at"]
    assert created_at.endswith("Z")
    assert len(created_at.split(".")[-1]) == 4  # 'mmm' + 'Z'


# --- Schema anchors ---------------------------------------------------------------


def test_live_event_union_is_in_openapi() -> None:
    """Without this, `LiveEvent` never reaches generated.ts and gets hand-written."""
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "SnapshotEvent",
        "TranscriptFinalEvent",
        "TranscriptPartialEvent",
        "InterjectionProposedEvent",
        "AgentStateChangedEvent",
        "WakeDetectedEvent",
        "ErrorEvent",
    ):
        assert name in schemas, f"{name} missing from OpenAPI"


def test_schema_anchor_endpoints_are_not_callable(client: TestClient) -> None:
    assert client.get("/api/_schema/live-event").status_code == 501
    assert client.get("/api/_schema/client-message").status_code == 501


# --- The harness: the whole point of Milestone 0 ----------------------------------


def test_harness_fixture_is_listed(client: TestClient) -> None:
    items = client.get("/api/dev/fixtures").json()["items"]
    ids = {f["id"] for f in items}
    assert "q3_revenue_review" in ids
    fixture = next(f for f in items if f["id"] == "q3_revenue_review")
    assert fixture["participant_count"] == 4
    assert fixture["duration_seconds"] > 0


def test_harness_start_unknown_fixture_lists_available(client: TestClient) -> None:
    body = client.post(
        "/api/dev/harness/start", json={"fixture_id": "does_not_exist"}
    ).json()
    assert body["error"]["code"] == "bad_request"
    assert "q3_revenue_review" in body["error"]["detail"]["available"]


def test_harness_emits_full_event_stream(client: TestClient) -> None:
    """Drive the fixture fast and assert the frontend gets everything it needs."""
    client.patch("/api/settings", json={"autonomy": "auto_post"})

    meeting = client.post(
        "/api/dev/harness/start",
        json={"fixture_id": "q3_revenue_review", "speed": 50.0},
    ).json()
    assert meeting["source"] == "harness"
    assert meeting["bot_id"] is None
    assert meeting["state"] == "in_call"
    meeting_id = meeting["id"]

    seen: list[str] = []
    finals: list[dict] = []
    interjections: list[dict] = []
    agent_states: list[str] = []

    with client.websocket_connect(f"/api/meetings/{meeting_id}/live") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot", "connect must yield exactly one snapshot"
        assert first["seq"] >= 1
        assert first["data"]["meeting"]["id"] == meeting_id
        # The fixture's joins fire within ~45ms of wall clock at speed 50, so a client
        # that subscribes after starting the meeting may legitimately miss those events.
        # Nothing is lost: the snapshot carries the roster, which is the documented
        # recovery. Track what the snapshot already knew so the assertions below can
        # accept either path, the same way a real frontend has to.
        roster_at_connect = {e["participant_id"] for e in first["data"]["meeting"]["roster"]}

        # The fixture is ~161s of content at 50x, so ~3.2s of wall clock.
        for _ in range(400):
            frame = ws.receive_json()
            kind = frame["type"]
            if kind == "ping":
                continue
            seen.append(kind)
            if kind == "transcript.final":
                finals.append(frame["data"])
            elif kind in ("interjection.proposed", "interjection.updated"):
                interjections.append(frame["data"])
            elif kind == "agent.state_changed":
                agent_states.append(frame["data"]["agent_state"])
            if kind == "meeting.state_changed" and frame["data"]["state"] == "ended":
                break

    kinds = set(seen)
    assert (
        "participant.joined" in kinds or roster_at_connect
    ), "the roster must arrive, by snapshot or by event"
    assert "transcript.partial" in kinds, "frontend live-line rendering needs partials"
    assert "transcript.final" in kinds
    assert "participant.speaking_changed" in kinds
    assert "speech.wake_detected" in kinds, "wake events drive the demo"
    assert "speech.question_captured" in kinds
    assert "speech.answered" in kinds
    assert "interjection.proposed" in kinds

    # Speaker identity resolved against the seeded people.
    by_name = {f["speaker_name"] for f in finals}
    assert "Marcus Webb" in by_name
    assert all(f["is_final"] for f in finals)

    matched = [f for f in finals if f["person_id"]]
    assert matched, "seeded speakers must resolve to Person ids"

    # The unmatched dial-in participant stays unmatched — the UI flags this state.
    roster = client.get(f"/api/meetings/{meeting_id}").json()["roster"]
    guest = next(e for e in roster if e["display_name"] == "Guest (dial-in)")
    assert guest["matched"] is False
    assert guest["person_id"] is None

    # The planted contradiction fired, with citations, under the 500-char chat cap.
    contradiction = next(i for i in interjections if i["kind"] == "contradiction")
    assert contradiction["citations"], "an interjection without evidence is not usable"
    assert len(contradiction["chat_alert"]) <= 500
    assert contradiction["status"] == "posted"  # autonomy=auto_post
    assert contradiction["confidence"] > 0.5

    # Speech mode ran the full state machine.
    assert "listening" in agent_states
    assert "thinking" in agent_states
    assert "speaking" in agent_states

    answer = next(i for i in interjections if i["kind"] == "answer")
    assert answer["spoken"] is True


def test_chat_alert_never_exceeds_meet_limit(client: TestClient) -> None:
    """Google Meet hard-caps chat at 500 chars. Over that, the post silently fails."""
    from app.ingest.harness import _CANNED

    for ref, canned in _CANNED.items():
        assert len(canned["chat_alert"]) <= 500, f"{ref} chat_alert too long"


def test_propose_autonomy_holds_interjections(client: TestClient) -> None:
    client.patch("/api/settings", json={"autonomy": "propose"})
    meeting = client.post(
        "/api/dev/harness/start",
        json={"fixture_id": "q3_revenue_review", "speed": 50.0},
    ).json()
    meeting_id = meeting["id"]

    proposed = None
    with client.websocket_connect(f"/api/meetings/{meeting_id}/live") as ws:
        ws.receive_json()  # snapshot
        for _ in range(400):
            frame = ws.receive_json()
            if frame["type"] == "interjection.proposed":
                proposed = frame["data"]
                break

    assert proposed is not None
    assert proposed["status"] == "proposed", "propose mode must not auto-post"
    assert proposed["posted_at"] is None

    approved = client.post(
        f"/api/interjections/{proposed['id']}/approve",
        json={"edited_chat_alert": "Tightened by the operator."},
    ).json()
    assert approved["status"] == "posted"
    assert approved["chat_alert"] == "Tightened by the operator."
    assert approved["posted_at"] is not None

    # Approving twice is a conflict, not a silent no-op.
    assert (
        client.post(f"/api/interjections/{proposed['id']}/approve", json={}).status_code
        == 400
    )


# --- Operator controls: the stage-insurance path ----------------------------------


def test_mute_blocks_wake_and_ask(client: TestClient) -> None:
    meeting = client.post(
        "/api/dev/harness/start",
        json={"fixture_id": "q3_revenue_review", "speed": 50.0},
    ).json()
    meeting_id = meeting["id"]

    muted = client.post(f"/api/meetings/{meeting_id}/mute", json={"muted": True}).json()
    assert muted["agent_state"] == "muted"

    assert client.post(f"/api/meetings/{meeting_id}/wake").status_code == 400
    assert (
        client.post(
            f"/api/meetings/{meeting_id}/ask", json={"question": "hi", "speak": True}
        ).status_code
        == 400
    )

    unmuted = client.post(
        f"/api/meetings/{meeting_id}/mute", json={"muted": False}
    ).json()
    assert unmuted["agent_state"] == "idle"
    assert client.post(f"/api/meetings/{meeting_id}/wake").status_code == 200


def test_ask_returns_answer_with_citations(client: TestClient) -> None:
    meeting = client.post(
        "/api/dev/harness/start",
        json={"fixture_id": "q3_revenue_review", "speed": 50.0},
    ).json()
    answer = client.post(
        f"/api/meetings/{meeting['id']}/ask",
        json={"question": "What is new-product revenue?", "speak": False},
    ).json()
    assert answer["kind"] == "answer"
    assert answer["citations"]
    assert len(answer["chat_alert"]) <= 500
    assert answer["id"].startswith("itj_")


def test_create_meeting_without_recall_key_fails_gracefully(client: TestClient) -> None:
    """No key must not mean a 500. The frontend builds the failure state from this."""
    body = client.post(
        "/api/meetings", json={"meeting_url": "https://meet.google.com/abc-defg-hij"}
    ).json()
    assert body["state"] == "failed"
    assert body["error"]
    assert "harness" in body["error"].lower()


def test_transcript_endpoint_returns_only_finals(client: TestClient) -> None:
    meeting = client.post(
        "/api/dev/harness/start",
        json={"fixture_id": "q3_revenue_review", "speed": 50.0},
    ).json()
    meeting_id = meeting["id"]

    with client.websocket_connect(f"/api/meetings/{meeting_id}/live") as ws:
        ws.receive_json()
        for _ in range(120):
            if ws.receive_json()["type"] == "transcript.final":
                break

    page = client.get(f"/api/meetings/{meeting_id}/transcript").json()
    assert page["items"]
    assert all(item["is_final"] for item in page["items"])
