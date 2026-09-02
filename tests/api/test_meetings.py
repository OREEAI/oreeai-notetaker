MEETING_PAYLOAD = {
    "title": "Weekly sync",
    "description": "Team alignment",
    "platform": "google_meet",
    "scheduled_at": "2026-09-10T10:00:00Z",
}


async def test_create_and_get_meeting(client) -> None:
    create_resp = await client.post("/api/v1/meetings", json=MEETING_PAYLOAD)
    assert create_resp.status_code == 201
    meeting = create_resp.json()
    assert meeting["title"] == "Weekly sync"
    assert meeting["status"] == "scheduled"

    get_resp = await client.get(f"/api/v1/meetings/{meeting['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == meeting["id"]


async def test_get_missing_meeting_returns_404(client) -> None:
    response = await client.get("/api/v1/meetings/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


async def test_list_meetings(client) -> None:
    await client.post("/api/v1/meetings", json=MEETING_PAYLOAD)
    await client.post("/api/v1/meetings", json={"title": "Retro", "platform": "zoom"})

    response = await client.get("/api/v1/meetings")
    assert response.status_code == 200
    meetings = response.json()
    assert len(meetings) == 2
    assert "transcript" not in meetings[0]
    assert sorted(m["title"] for m in meetings) == ["Retro", "Weekly sync"]


async def test_update_meeting(client) -> None:
    meeting_id = (await client.post("/api/v1/meetings", json=MEETING_PAYLOAD)).json()["id"]

    update_resp = await client.patch(
        f"/api/v1/meetings/{meeting_id}",
        json={"started_at": "2026-09-10T10:05:00Z", "ended_at": "2026-09-10T11:00:00Z"},
    )
    assert update_resp.status_code == 200

    complete_resp = await client.patch(
        f"/api/v1/meetings/{meeting_id}",
        json={"status": "completed", "summary": "Aligned on roadmap"},
    )
    assert complete_resp.status_code == 200
    assert complete_resp.json()["status"] == "completed"
    assert complete_resp.json()["summary"] == "Aligned on roadmap"


async def test_complete_without_end_time_conflicts(client) -> None:
    meeting_id = (await client.post("/api/v1/meetings", json=MEETING_PAYLOAD)).json()["id"]

    response = await client.patch(f"/api/v1/meetings/{meeting_id}", json={"status": "completed"})
    assert response.status_code == 409


async def test_end_before_start_conflicts(client) -> None:
    meeting_id = (await client.post("/api/v1/meetings", json=MEETING_PAYLOAD)).json()["id"]

    response = await client.patch(
        f"/api/v1/meetings/{meeting_id}",
        json={"started_at": "2026-09-10T11:00:00Z", "ended_at": "2026-09-10T10:00:00Z"},
    )
    assert response.status_code == 409


async def test_delete_meeting(client) -> None:
    meeting_id = (await client.post("/api/v1/meetings", json=MEETING_PAYLOAD)).json()["id"]

    delete_resp = await client.delete(f"/api/v1/meetings/{meeting_id}")
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/meetings/{meeting_id}")
    assert get_resp.status_code == 404


async def test_naive_datetimes_normalized_to_utc(client) -> None:
    response = await client.post(
        "/api/v1/meetings",
        json={"title": "Naive times", "scheduled_at": "2026-09-10T10:00:00"},
    )
    assert response.status_code == 201
    assert response.json()["scheduled_at"] == "2026-09-10T10:00:00Z"


async def test_mixed_naive_and_aware_datetimes_accepted(client) -> None:
    meeting_id = (await client.post("/api/v1/meetings", json=MEETING_PAYLOAD)).json()["id"]

    response = await client.patch(
        f"/api/v1/meetings/{meeting_id}",
        json={"started_at": "2026-09-10T10:00:00", "ended_at": "2026-09-10T11:00:00Z"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["started_at"] == "2026-09-10T10:00:00Z"
    assert body["ended_at"] == "2026-09-10T11:00:00Z"
