"""TripFlow workflow tests.

Tests cover agent routing, policy outcomes, and checkpoint recovery.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from checkpoint_store import CheckpointStore
import webapp
from webapp import app, sessions


client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sessions.clear()
    monkeypatch.setattr(webapp, "checkpoint_store", CheckpointStore(tmp_path))


def test_health_endpoint() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def create_session() -> str:
    response = client.post("/api/session")
    response.raise_for_status()
    return response.json()["session_id"]


def test_approval_gate_saves_checkpoint() -> None:
    session_id = create_session()

    response = client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "message": "Book a trip to Tokyo next month with budget 1800 for 2 travelers",
        },
    )
    payload = response.json()

    assert payload["status"] == "awaiting_approval"
    assert payload["checkpoint_ready"] is True
    assert "approval" in payload["message"].lower()
    assert webapp.checkpoint_store.checkpoint_path(session_id).exists()


def test_denied_approval_stops_booking() -> None:
    session_id = create_session()
    client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "message": "Book a trip to Tokyo next month with budget 1800 for 2 travelers",
        },
    )

    response = client.post(
        "/api/approval",
        json={"session_id": session_id, "approve": False},
    )
    payload = response.json()

    assert payload["status"] == "blocked"
    assert payload["booking_reference"] is None


def test_approval_then_resume_completes_booking() -> None:
    session_id = create_session()
    client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "message": "Book a trip to Tokyo next month with budget 1800 for 2 travelers",
        },
    )

    approval_response = client.post(
        "/api/approval",
        json={"session_id": session_id, "approve": True},
    )
    payload = approval_response.json()

    assert payload["status"] == "completed"
    assert payload["booking_reference"].startswith("TRIP-")
    assert any(event["message"] == "Resume continuing with previously granted approval." for event in payload["trace"])


def test_over_hard_limit_blocks_immediately() -> None:
    session_id = create_session()

    response = client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "message": "Book a trip to Tokyo in December with budget 5000 for 4 travelers",
        },
    )
    payload = response.json()

    assert payload["status"] == "blocked"
    assert payload["booking_reference"] is None
    assert "hard limit" in payload["message"].lower()


def test_low_risk_request_completes_automatically() -> None:
    session_id = create_session()

    response = client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "message": "Book a trip to Oslo next month with budget 700 for 1 traveler",
        },
    )
    payload = response.json()

    assert payload["status"] == "completed"
    assert payload["booking_reference"].startswith("TRIP-")


def test_unstructured_message_is_traced_for_debugging() -> None:
    session_id = create_session()

    response = client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "message": "hello there",
        },
    )
    payload = response.json()

    assert payload["status"] == "collecting_intent"
    assert any(event["message"] == "No structured fields were extracted from the latest message." for event in payload["trace"])