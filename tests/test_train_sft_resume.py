from scripts.train_sft import _periodic_checkpoint_due, _resolve_resume


def test_auto_resume_starts_cleanly_without_checkpoint(tmp_path):
    assert _resolve_resume("auto", str(tmp_path / "missing")) is None
    incomplete = tmp_path / "checkpoint-10"
    incomplete.mkdir()
    assert _resolve_resume("auto", str(tmp_path)) is None


def test_auto_resume_uses_latest_complete_checkpoint(tmp_path):
    for step in (10, 30, 20):
        checkpoint = tmp_path / f"checkpoint-{step}"
        checkpoint.mkdir()
        (checkpoint / "trainer_state.json").write_text("{}")
    junk = tmp_path / "checkpoint-latest"
    junk.mkdir()
    (junk / "trainer_state.json").write_text("{}")

    assert _resolve_resume("auto", str(tmp_path)) == str(tmp_path / "checkpoint-30")


def test_explicit_resume_path_is_preserved(tmp_path):
    explicit = str(tmp_path / "manual")
    assert _resolve_resume(explicit, str(tmp_path)) == explicit
    assert _resolve_resume(None, str(tmp_path)) is None


def test_periodic_recovery_checkpoint_schedule():
    assert not _periodic_checkpoint_due(0, 250)
    assert not _periodic_checkpoint_due(249, 250)
    assert _periodic_checkpoint_due(250, 250)
    assert _periodic_checkpoint_due(500, 250)
    assert not _periodic_checkpoint_due(500, 0)
