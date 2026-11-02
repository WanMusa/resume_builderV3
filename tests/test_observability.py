import json
import logging

from app.observability import emit_event


def test_emit_event_writes_json_with_safe_operational_fields(caplog):
    caplog.set_level(logging.INFO, logger="resume_builder.events")

    emit_event("stage.completed", job_id="job-123", stage="generate", duration_ms=42.5)

    record = next(record for record in caplog.records if record.name == "resume_builder.events")
    payload = json.loads(record.getMessage())
    assert payload["event"] == "stage.completed"
    assert payload["service"] == "resume-builder-v3"
    assert payload["job_id"] == "job-123"
    assert payload["stage"] == "generate"
    assert payload["duration_ms"] == 42.5
    assert payload["level"] == "INFO"
    assert payload["timestamp"].endswith("+00:00")