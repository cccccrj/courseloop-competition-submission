from backend.core.generation_contract import production_task_limits


def test_production_limits_read_dotenv_without_backend_dependencies(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CL_LLM_SOLVE_MAX_TOKENS=1280\n"
        "CL_LLM_GROUNDED_QA_MAX_TOKENS='640'\n",
        encoding="utf-8",
    )

    limits = production_task_limits(env_file)

    assert limits["solve"] == 1280
    assert limits["grounded_qa"] == 640
    assert limits["diagnose"] == 768


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CL_LLM_PRACTICE_MAX_TOKENS=1200\n", encoding="utf-8")
    monkeypatch.setenv("CL_LLM_PRACTICE_MAX_TOKENS", "1400")

    assert production_task_limits(env_file)["practice"] == 1400
