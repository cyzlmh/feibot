from feibot.session.manager import SessionManager


def test_session_manager_rotate_keeps_old_archive_and_switches_active_session(tmp_path) -> None:
    manager = SessionManager(tmp_path / "sessions")

    first = manager.get_or_create("feishu:chat1")
    first.add_message("user", "hello")
    manager.save(first)

    first_id = first.session_id
    first_path = tmp_path / "sessions" / (first.storage_path or "")
    assert first_path.exists()
    assert first_path.parent.relative_to(tmp_path / "sessions").parts == (
        first.created_at.strftime("%Y"),
        first.created_at.strftime("%m"),
        first.created_at.strftime("%d"),
    )

    second = manager.rotate("feishu:chat1")
    assert second.session_id != first_id
    assert second.messages == []

    second.add_message("user", "new turn")
    manager.save(second)
    second_path = tmp_path / "sessions" / (second.storage_path or "")
    assert second_path.exists()

    manager.invalidate("feishu:chat1")
    loaded = manager.get_or_create("feishu:chat1")
    assert loaded.session_id == second.session_id
    assert [m["content"] for m in loaded.messages] == ["new turn"]


def test_session_manager_save_appends_new_records(tmp_path) -> None:
    manager = SessionManager(tmp_path / "sessions")

    session = manager.get_or_create("cli:direct")
    session.add_message("user", "one")
    manager.save(session)
    path = tmp_path / "sessions" / (session.storage_path or "")
    first_line_count = len(path.read_text(encoding="utf-8").splitlines())

    session.add_message("assistant", "two")
    manager.save(session)
    second_line_count = len(path.read_text(encoding="utf-8").splitlines())

    assert second_line_count > first_line_count


def test_legacy_session_id_is_stable_after_reload(tmp_path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    legacy = sessions_dir / "feishu_chat1.jsonl"
    legacy.write_text(
        '{"_type":"metadata","created_at":"2026-03-01T10:00:00","updated_at":"2026-03-01T10:00:00","metadata":{},"last_consolidated":0}\n'
        '{"role":"user","content":"hello","timestamp":"2026-03-01T10:00:00"}\n',
        encoding="utf-8",
    )

    manager = SessionManager(sessions_dir)
    first = manager.get_or_create("feishu:chat1")
    session_id = first.session_id

    manager.invalidate("feishu:chat1")
    second = manager.get_or_create("feishu:chat1")

    assert session_id == second.session_id
    assert session_id.endswith("_legacy")
