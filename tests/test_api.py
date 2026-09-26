from datetime import UTC, datetime
from threading import Event, current_thread
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import APP_VERSION, create_app
from app.automation import CycleResult
from app.config import Settings
from app.monitoring import finish_cycle, start_cycle
from app.persistence.database import open_database

NOW = datetime(2026, 9, 21, 8, tzinfo=UTC)


def api_settings(tmp_path):
    return Settings(
        database_file=tmp_path / "classroom.db",
        cycle_lock_file=tmp_path / "automation-cycle.lock",
        token_file=tmp_path / "token.json",
    )


def test_health_is_cheap_and_versioned(tmp_path):
    settings = api_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": APP_VERSION}
    assert not settings.database_file.exists()


def test_status_has_stable_empty_shape(tmp_path):
    settings = api_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/status")
    assert response.status_code == 200
    assert response.json() == {
        "status": "online",
        "agent_running": False,
        "cycle_running": False,
        "mode": "unknown",
        "interval_hours": 8,
        "last_cycle": None,
        "next_cycle": None,
        "eligible": 0,
        "answered": 0,
        "generated": 0,
        "errors": 0,
        "duration_seconds": None,
        "version": APP_VERSION,
        "turn_in_enabled": False,
        "last_cycle_result": None,
    }


def test_history_reads_persisted_cycles_and_enforces_limit(tmp_path):
    settings = api_settings(tmp_path)
    engine = open_database(settings.database_file)
    cycle_id = start_cycle(engine, "manual", NOW)
    finish_cycle(
        engine,
        cycle_id,
        NOW,
        2.5,
        CycleResult("SUCCESS", eligible=[1], solved=[1], generated=[1]),
    )
    engine.dispose()

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/history?limit=1")
        activities = client.get("/api/v1/activities?limit=1")
        invalid = client.get("/api/v1/history?limit=101")
    assert response.status_code == 200
    assert response.json()[0] == {
        "started_at": NOW.isoformat(),
        "finished_at": NOW.isoformat(),
        "mode": "manual",
        "status": "success",
        "eligible": 1,
        "answered": 1,
        "generated": 1,
        "errors": 0,
        "duration_seconds": 2.5,
    }
    assert activities.status_code == 200
    assert activities.json() == [
        {
            "course": "Disciplina desconhecida",
            "title": "Atividade #1",
            "due_at": None,
            "status": "generated",
            "pdf_generated": False,
            "draft_attached": False,
        }
    ]
    assert invalid.status_code == 422


def test_manual_run_returns_immediately_off_thread_and_rejects_concurrency(tmp_path):
    settings = api_settings(tmp_path)
    started = Event()
    release = Event()
    finished = Event()
    worker_names = []

    def blocked_cycle(*args, acquired_lock, **kwargs):
        worker_names.append(current_thread().name)
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        return CycleResult("SUCCESS")

    with (
        patch("app.api.run_single_cycle", side_effect=blocked_cycle),
        TestClient(create_app(settings)) as client,
    ):
        first = client.post("/api/v1/run")
        assert started.wait(timeout=2)
        assert first.status_code == 202
        assert not finished.is_set()
        assert worker_names == ["classroom-agent-manual-cycle"]
        second = client.post("/api/v1/run")
        release.set()

    assert first.json()["status"] == "accepted"
    assert second.status_code == 409
    assert second.json()["detail"] == "A cycle is already running"
    assert second.json()["error"]["code"] == "cycle_running"


def test_thread_start_failure_releases_shared_lock(tmp_path):
    from app.cycle_lock import CycleLock

    settings = api_settings(tmp_path)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        with patch("app.api.Thread", side_effect=RuntimeError("cannot start")):
            assert client.post("/api/v1/run").status_code == 500
    lock = CycleLock(settings.cycle_lock_file)
    assert lock.acquire()
    lock.release()


def test_run_respects_external_process_lock(tmp_path):
    from app.cycle_lock import CycleLock

    settings = api_settings(tmp_path)
    with CycleLock(settings.cycle_lock_file):
        with patch("app.api.run_single_cycle") as run, TestClient(create_app(settings)) as client:
            assert client.post("/api/v1/run").status_code == 409
            run.assert_not_called()
