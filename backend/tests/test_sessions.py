"""Session management and post-meeting review.

The load-bearing test here is `test_session_survives_restart`. A dashboard whose job is
reviewing finished meetings is broken if a restart empties it, so persistence is a
product requirement, not an optimization.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import archive
from app.main import app
from app.store import store


@pytest.fixture(autouse=True)
def _isolated_archive(tmp_path, monkeypatch):
    """Point the archive at a temp dir so tests never touch the real data/ directory."""
    monkeypatch.setattr(archive, "SESSIONS_DIR", tmp_path / "sessions")
    archive._dirty.clear()
    archive._finalized.clear()
    store.reset()
    yield
    store.reset()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def _run_fixture(client: TestClient, speed: float = 50.0) -> str:
    """Play the fixture to completion and return the meeting id."""
    meeting = client.post(
        "/api/dev/harness/start",
        json={"fixture_id": "q3_revenue_review", "speed": speed},
    ).json()
    meeting_id = meeting["id"]
    with client.websocket_connect(f"/api/meetings/{meeting_id}/live") as ws:
        ws.receive_json()  # snapshot
        for _ in range(600):
            frame = ws.receive_json()
            if frame["type"] == "meeting.state_changed" and frame["data"]["state"] == "ended":
                break
    return meeting_id


# --- Session records --------------------------------------------------------------


def test_session_logs_url_time_and_participants(client: TestClient) -> None:
    """The three things a session record has to capture."""
    meeting_id = _run_fixture(client)
    meeting = client.get(f"/api/meetings/{meeting_id}").json()

    assert meeting["started_at"] is not None
    assert meeting["ended_at"] is not None
    assert meeting["title"]
    # Harness meetings have no URL; a Recall meeting carries the one it was sent to.
    assert "meeting_url" in meeting

    names = {entry["display_name"] for entry in meeting["roster"]}
    assert {"Priya Raman", "Sarah Chen", "Marcus Webb"} <= names
    assert any(e["is_host"] for e in meeting["roster"])
    # The dial-in guest stays unresolved, and the record says so.
    assert any(not e["matched"] for e in meeting["roster"])


def test_multiple_sessions_are_listed_newest_first(client: TestClient) -> None:
    first = _run_fixture(client)
    second = _run_fixture(client)

    listed = client.get("/api/meetings").json()["items"]
    ids = [m["id"] for m in listed]
    assert first in ids and second in ids
    # ULIDs sort chronologically, so newest-first is a plain reverse id sort.
    assert ids.index(second) < ids.index(first)


def test_session_stats_are_populated(client: TestClient) -> None:
    meeting_id = _run_fixture(client)
    stats = client.get(f"/api/meetings/{meeting_id}/bundle").json()["meeting"]["stats"]

    assert stats["utterance_count"] > 15
    assert stats["participant_count"] == 4
    assert stats["duration_seconds"] > 0
    assert stats["interjection_count"] >= 1
    assert stats["source_document_count"] >= 1


# --- Persistence ------------------------------------------------------------------


def test_session_survives_restart(client: TestClient) -> None:
    """The whole premise of the review dashboard.

    Run a session, flush it, wipe memory as a process restart would, reload from disk,
    and confirm the transcript and interjections come back.
    """
    meeting_id = _run_fixture(client)

    before = client.get(f"/api/meetings/{meeting_id}/bundle").json()
    assert before["transcript"], "fixture should have produced a transcript"

    assert archive.save(meeting_id) is True

    # Simulate a restart: memory gone, disk intact.
    store.meetings.clear()
    store.segments.clear()
    store.interjections.clear()
    assert client.get(f"/api/meetings/{meeting_id}").status_code == 404

    assert archive.load_all() >= 1

    after = client.get(f"/api/meetings/{meeting_id}/bundle").json()
    assert len(after["transcript"]) == len(before["transcript"])
    assert len(after["interjections"]) == len(before["interjections"])
    assert after["meeting"]["roster"] == before["meeting"]["roster"]
    assert after["meeting"]["started_at"] == before["meeting"]["started_at"]


def test_restart_closes_a_session_that_was_live(client: TestClient) -> None:
    """A meeting cannot still be in_call after the process that ran it died."""
    meeting = client.post(
        "/api/dev/harness/start",
        json={"fixture_id": "q3_revenue_review", "speed": 50.0},
    ).json()
    meeting_id = meeting["id"]
    assert meeting["state"] == "in_call"

    archive.save(meeting_id)
    store.meetings.clear()
    store.segments.clear()
    store.interjections.clear()
    archive.load_all()

    restored = client.get(f"/api/meetings/{meeting_id}").json()
    assert restored["state"] == "ended"
    assert "restart" in (restored["error"] or "").lower()


def test_corrupt_archive_does_not_block_the_rest(client: TestClient) -> None:
    meeting_id = _run_fixture(client)
    archive.save(meeting_id)

    (archive.SESSIONS_DIR / "mtg_garbage.json").write_text("{not json", encoding="utf-8")

    store.meetings.clear()
    store.segments.clear()
    store.interjections.clear()
    assert archive.load_all() == 1, "the valid session should still load"
    assert client.get(f"/api/meetings/{meeting_id}").status_code == 200


# --- Sources: the audit surface ---------------------------------------------------


def test_sources_aggregate_citations_by_document(client: TestClient) -> None:
    meeting_id = _run_fixture(client)
    sources = client.get(f"/api/meetings/{meeting_id}/sources").json()["items"]

    assert sources, "the fixture cites documents"
    assert any(s["filename"] == "Q3-board-deck.pdf" for s in sources)

    for source in sources:
        # Quotes are deduped by passage, so citations >= distinct quotes.
        assert source["citation_count"] >= len(source["quotes"])
        assert source["interjection_ids"]
        for quote in source["quotes"]:
            assert quote["quote"], "a citation with no quote is not evidence"
            assert 0.0 <= quote["relevance"] <= 1.0

    # Most-cited first — the order a human auditing the session wants.
    counts = [s["citation_count"] for s in sources]
    assert counts == sorted(counts, reverse=True)


def test_source_quotes_trace_back_to_real_interjections(client: TestClient) -> None:
    meeting_id = _run_fixture(client)
    bundle = client.get(f"/api/meetings/{meeting_id}/bundle").json()

    known = {i["id"] for i in bundle["interjections"]}
    for source in bundle["sources"]:
        for quote in source["quotes"]:
            assert quote["interjection_ids"], "a passage with no claim behind it is orphaned"
            for claim_id in quote["interjection_ids"]:
                assert claim_id in known, "a source must cite a real claim"


# --- Review surface ---------------------------------------------------------------


def test_bundle_returns_everything_in_one_call(client: TestClient) -> None:
    meeting_id = _run_fixture(client)
    bundle = client.get(f"/api/meetings/{meeting_id}/bundle").json()

    assert set(bundle) == {"meeting", "transcript", "interjections", "sources"}
    assert bundle["meeting"]["id"] == meeting_id
    assert all(segment["is_final"] for segment in bundle["transcript"])


def test_transcript_search_by_text_and_speaker(client: TestClient) -> None:
    meeting_id = _run_fixture(client)

    everything = client.get(f"/api/meetings/{meeting_id}/search").json()["items"]
    assert everything

    hits = client.get(f"/api/meetings/{meeting_id}/search", params={"q": "revenue"}).json()
    assert hits["items"]
    assert all("revenue" in s["text"].casefold() for s in hits["items"])
    assert len(hits["items"]) < len(everything)

    speaker = next(s for s in everything if s["person_id"])
    scoped = client.get(
        f"/api/meetings/{meeting_id}/search", params={"person_id": speaker["person_id"]}
    ).json()
    assert scoped["items"]
    assert {s["person_id"] for s in scoped["items"]} == {speaker["person_id"]}

    assert client.get(
        f"/api/meetings/{meeting_id}/search", params={"q": "zzzznotpresent"}
    ).json()["items"] == []


def test_single_interjection_is_deep_linkable(client: TestClient) -> None:
    meeting_id = _run_fixture(client)
    interjections = client.get(f"/api/meetings/{meeting_id}/interjections").json()["items"]
    target = interjections[0]

    fetched = client.get(
        f"/api/meetings/{meeting_id}/interjections/{target['id']}"
    ).json()
    assert fetched["id"] == target["id"]

    assert (
        client.get(f"/api/meetings/{meeting_id}/interjections/itj_nope").status_code == 404
    )


def test_delete_removes_session_and_archive(client: TestClient) -> None:
    meeting_id = _run_fixture(client)
    archive.save(meeting_id)
    assert (archive.SESSIONS_DIR / f"{meeting_id}.json").exists()

    assert client.delete(f"/api/meetings/{meeting_id}").json() == {"ok": True}
    assert client.get(f"/api/meetings/{meeting_id}").status_code == 404
    assert not (archive.SESSIONS_DIR / f"{meeting_id}.json").exists()

    # Gone from disk means gone after a reload too.
    store.meetings.clear()
    archive.load_all()
    assert client.get(f"/api/meetings/{meeting_id}").status_code == 404


def test_review_endpoints_404_on_unknown_session(client: TestClient) -> None:
    for path in ("bundle", "sources", "search"):
        assert client.get(f"/api/meetings/mtg_nope/{path}").status_code == 404


def test_repeated_passage_is_one_quote_with_two_backlinks(client: TestClient) -> None:
    """The same deck line cited by two claims is one piece of evidence, not two.

    Rendering the identical quote twice on the audit screen reads as a bug.
    """
    meeting_id = _run_fixture(client)
    sources = client.get(f"/api/meetings/{meeting_id}/sources").json()["items"]

    for source in sources:
        texts = [q["quote"] for q in source["quotes"]]
        assert len(texts) == len(set(texts)), f"{source['filename']} repeats a passage"

    shared = [q for s in sources for q in s["quotes"] if len(q["interjection_ids"]) > 1]
    assert shared, "the fixture has a passage cited by more than one claim"
