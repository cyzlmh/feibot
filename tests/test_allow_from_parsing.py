from types import SimpleNamespace

from feibot.bus.events import OutboundMessage
from feibot.bus.queue import MessageBus
from feibot.channels.allow_from import (
    extract_allow_from_msisdn_map,
    extract_allow_from_open_ids,
    parse_allow_from_entry,
)
from feibot.channels.base import BaseChannel


class _DummyChannel(BaseChannel):
    async def start(self) -> None:  # pragma: no cover - test helper
        return None

    async def stop(self) -> None:  # pragma: no cover - test helper
        return None

    async def send(self, msg: OutboundMessage) -> None:  # pragma: no cover - test helper
        return None


def _make_channel(allow_from: list[str]) -> _DummyChannel:
    return _DummyChannel(
        config=SimpleNamespace(allow_from=allow_from),
        bus=MessageBus(),
    )


def test_parse_allow_from_entry_supports_optional_phone() -> None:
    assert parse_allow_from_entry("ou_user") == ("ou_user", "")
    assert parse_allow_from_entry("ou_user:13800000000") == ("ou_user", "13800000000")


def test_extract_allow_from_helpers_normalize_entries() -> None:
    entries = [" ou_a : 13800000000 ", "ou_b", "", " :13900000000", "ou_c: "]
    assert extract_allow_from_open_ids(entries) == ["ou_a", "ou_b", "ou_c"]
    assert extract_allow_from_msisdn_map(entries) == {"ou_a": "13800000000"}


def test_base_channel_allows_sender_when_allow_from_entry_contains_phone() -> None:
    channel = _make_channel(["ou_requester:13800000000"])
    assert channel.is_allowed("ou_requester")
    assert channel.is_allowed("ou_requester|u_requester")
    assert not channel.is_allowed("ou_other")
