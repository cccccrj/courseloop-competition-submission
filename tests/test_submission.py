"""交付层回归：不访问真实模型、不使用生产库。"""
import json
import pytest
from fastapi.testclient import TestClient
from backend.core.config import settings
from backend.verification.numeric_checker import check_numeric
from scripts.submission_prepare import QUESTIONS


@pytest.mark.parametrize("question", QUESTIONS)
def test_original_demo_choices_have_machine_checkable_gold(question):
    course, knowledge, stem, options, gold = question
    canonical = {"kind": "choice", "value": gold}
    assert len(options) == 4 and course in {"ds-algo", "co", "os", "net"}
    assert check_numeric(gold, canonical).passed is True
    for option in "ABCD":
        assert check_numeric(option, canonical).passed is (option == gold)


def test_submission_server_does_not_turn_unknown_api_into_spa(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    from scripts.submission_serve import app
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/unknown-endpoint").status_code == 404
        assert c.get("/api/docs").status_code == 404
        assert c.get("/random-missing-file.txt").status_code == 404
