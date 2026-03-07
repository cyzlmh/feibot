from feibot.session.channel_log import ChannelLogStore, LogEntry
from feibot.session.manager import Session


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

