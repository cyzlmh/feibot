from feibot.session.channel_log import ChannelLogStore, LogEntry
from feibot.session.manager import Session, SessionManager


def test_sync_users_to_session_respects_after_timestamp(tmp_path) -> None:
    store = ChannelLogStore(tmp_path / "logs")
    session = Session(key="feishu:chat1")

    entries = [
        ("2026-02-24T10:00:00", "m1", "old-1"),
        ("2026-02-24T10:05:00", "m2", "old-2"),
        ("2026-02-24T10:10:00", "m3", "new-1"),
    ]
    for ts, mid, content in entries:
        store.append(
            session.key,
            LogEntry(
                role="user",
                content=content,
                timestamp=ts,
                message_id=mid,
                sender_id="u1",
                channel="feishu",
                chat_id="chat1",
            ),
        )

    count = store.sync_users_to_session(
        session.key,
        session,
        after_timestamp="2026-02-24T10:05:00",
    )

    assert count == 1
    assert [m["content"] for m in session.messages] == ["new-1"]
    assert [m["message_id"] for m in session.messages] == ["m3"]


def test_sync_users_to_session_isolated_by_session_id(tmp_path) -> None:
    logs = ChannelLogStore(tmp_path / "logs")
    sessions = SessionManager(tmp_path / "sessions")

    first = sessions.get_or_create("feishu:chat1")
    logs.append(
        first,
        LogEntry(
            role="user",
            content="old session",
            timestamp="2026-02-24T10:00:00",
            message_id="m1",
            sender_id="u1",
            channel="feishu",
            chat_id="chat1",
        ),
    )

    second = sessions.rotate("feishu:chat1")
    logs.append(
        second,
        LogEntry(
            role="user",
            content="new session",
            timestamp="2026-02-24T11:00:00",
            message_id="m2",
            sender_id="u1",
            channel="feishu",
            chat_id="chat1",
        ),
    )

    count = logs.sync_users_to_session("feishu:chat1", second)

    assert count == 1
    assert [m["content"] for m in second.messages] == ["new session"]
