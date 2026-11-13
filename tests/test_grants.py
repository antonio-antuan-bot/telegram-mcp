"""Tests for the grants file (telegram_mcp.grants) and the access tools."""

import asyncio
import base64
import json
import logging
import os
import stat
from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel, Chat, User

from telegram_mcp import grants, runtime
from telegram_mcp.tools import access, chats

CHANNEL = -1001234567890
GROUP = -4242
PERSON = 5550001
SELF = 1000
BOT = 6660001


def user(uid, first="Ann", last=None, bot=False, is_self=False):
    return User(id=uid, first_name=first, last_name=last, bot=bot, is_self=is_self)


def channel(bare_id, title="News"):
    return Channel(id=bare_id, title=title, photo=None, date=None, broadcast=True)


def group(bare_id, title="Book club"):
    return Chat(id=bare_id, title=title, photo=None, participants_count=3, date=None, version=1)


ENTITIES = {
    CHANNEL: channel(1234567890, "News"),
    GROUP: group(4242, "Book club"),
    PERSON: user(PERSON, "Ann", "Lee"),
    SELF: user(SELF, "Me", is_self=True),
    BOT: user(BOT, "Helper", bot=True),
    777000: user(777000, "Telegram"),
}


class FakeClient:
    def __init__(self, entities=ENTITIES):
        self.entities = entities

    async def get_me(self):
        return self.entities[SELF]

    async def get_entity(self, chat_id):
        if chat_id not in self.entities:
            raise ValueError("Could not find the input entity")
        return self.entities[chat_id]

    async def get_dialogs(self, limit=None, **kwargs):
        dialogs = [SimpleNamespace(entity=e) for e in self.entities.values()]
        return dialogs[:limit]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A grants store in tmp_path, seeded with the channel."""
    paths = SimpleNamespace(
        file=str(tmp_path / "grants" / "grants.json"),
        seal=str(tmp_path / "seal" / "seal.json"),
    )
    os.makedirs(os.path.dirname(paths.file))
    os.makedirs(os.path.dirname(paths.seal))
    monkeypatch.setenv("TELEGRAM_GRANTS_FILE", paths.file)
    monkeypatch.setenv("TELEGRAM_GRANTS_SEAL_FILE", paths.seal)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(CHANNEL))
    monkeypatch.delenv("TELEGRAM_RESTRICTED_CHAT_IDS", raising=False)
    monkeypatch.setenv("TELEGRAM_GRANT_URL", "http://approver:9001/grant")
    monkeypatch.setenv("TELEGRAM_GRANT_ACCOUNT", "telegram-test")
    monkeypatch.setattr(grants, "_config_env", None)
    monkeypatch.setattr(grants, "_pending", {})
    monkeypatch.setattr(grants, "_withdrawn", set())
    monkeypatch.setattr(grants, "_revocations", 0)
    client = FakeClient()
    monkeypatch.setattr(access, "get_client", lambda account=None: client)

    async def connected(cl=None):
        return None

    monkeypatch.setattr(access, "ensure_connected", connected)
    return paths


def run(coro, timeout=10):
    """asyncio.run with a limit, so a broken barrier fails instead of hanging."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def write_json(path, value):
    with open(path, "w") as fh:
        json.dump(value, fh)


def approver(status=200, answer=None, calls=None, wait=None):
    """A fake post(url, body, timeout) for ask_approver."""

    async def post(url, body, timeout):
        if calls is not None:
            calls.append((url, body, timeout))
        if wait is not None:
            await wait.wait()
        return status, ({"allow": True} if answer is None else answer)

    return post


async def ask(post, chat_id=PERSON, alias="Ann"):
    """One request the way request_access runs it: reserve, ask, release."""
    generation = grants.reserve(chat_id)
    if generation is None:
        return {"result": "denied", "code": "pending"}
    try:
        return await grants.ask_approver(post, chat_id, alias, generation)
    finally:
        grants.release(chat_id)


# --- off by default ---------------------------------------------------------


def test_off_without_grants_file(monkeypatch):
    monkeypatch.delenv("TELEGRAM_GRANTS_FILE", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(PERSON))
    monkeypatch.setattr(runtime, "ALLOWED_CHAT_IDS", None)
    assert runtime.is_chat_allowed(PERSON)
    assert not runtime.is_chat_allowed(GROUP)


# --- the store --------------------------------------------------------------


def test_new_store_is_seeded_once_with_private_files(store):
    assert runtime.is_chat_allowed(CHANNEL)
    assert not runtime.is_chat_allowed(PERSON)
    data = read_json(store.file)
    assert data["subjects"] == {str(CHANNEL): {"state": "granted", "generation": 0}}
    assert data["token"] == read_json(store.seal)["token"]
    for path in (store.file, store.seal):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_seed_entry_once_revoked_stays_revoked(store, monkeypatch):
    run(grants.revoke(CHANNEL))
    assert not runtime.is_chat_allowed(CHANNEL)
    # A restart reads the store again; the seed must not bring it back.
    monkeypatch.setattr(grants, "_config_env", None)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert read_json(store.file)["subjects"][str(CHANNEL)]["state"] == "revoked"


def test_missing_file_with_seal_denies_everything_and_does_not_reseed(store):
    assert runtime.is_chat_allowed(CHANNEL)
    os.unlink(store.file)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert runtime.is_chat_allowlist_enabled()
    assert not os.path.exists(store.file)


GOOD = {"state": "granted", "generation": 0}


@pytest.mark.parametrize(
    "change",
    [
        lambda d: "{not json",
        lambda d: {**d, "version": 2},
        lambda d: {**d, "version": "1"},
        lambda d: {k: v for k, v in d.items() if k != "token"},
        lambda d: {**d, "subjects": []},
        lambda d: {**d, "subjects": {**d["subjects"], "abc": GOOD}},
        lambda d: {**d, "subjects": {**d["subjects"], "007": GOOD}},
        lambda d: {**d, "subjects": {**d["subjects"], "+7": GOOD}},
        lambda d: {**d, "subjects": {**d["subjects"], "5": "granted"}},
        lambda d: {**d, "subjects": {**d["subjects"], "5": {"state": "maybe", "generation": 0}}},
        lambda d: {
            **d,
            "subjects": {**d["subjects"], "5": {"state": "granted", "generation": -1}},
        },
        lambda d: {
            **d,
            "subjects": {**d["subjects"], "5": {"state": "granted", "generation": True}},
        },
        lambda d: {
            **d,
            "subjects": {**d["subjects"], "5": {"state": "granted", "generation": 1.0}},
        },
        lambda d: {**d, "subjects": {**d["subjects"], "5": {"state": "granted"}}},
    ],
)
def test_broken_file_denies_everything_then_reloads(store, change):
    """Each broken file keeps the right token and a valid grant for the
    channel, so only the refusal of the whole file can close it."""
    assert runtime.is_chat_allowed(CHANNEL)
    good = open(store.file).read()
    broken = change(json.loads(good))
    with open(store.file + ".new", "w") as fh:
        fh.write(broken if isinstance(broken, str) else json.dumps(broken))
    os.replace(store.file + ".new", store.file)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert runtime.get_effective_allowed_chat_ids() == set()
    # Putting the very same file back is a restore: the seal was replaced
    # when the damage was seen, so the grant needs a new approval.
    with open(store.file + ".new", "w") as fh:
        fh.write(good)
    os.replace(store.file + ".new", store.file)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert grants.config().store.load().subjects[CHANNEL].state == "unconfirmed"


def test_lost_file_put_back_from_backup_needs_new_approval(store):
    assert runtime.is_chat_allowed(CHANNEL)
    backup = open(store.file).read()
    os.unlink(store.file)
    assert not runtime.is_chat_allowed(CHANNEL)
    with open(store.file, "w") as fh:
        fh.write(backup)
    assert not runtime.is_chat_allowed(CHANNEL)


def test_unreadable_file_or_seal_denies_even_when_cached(store):
    assert runtime.is_chat_allowed(CHANNEL)
    os.chmod(store.seal, 0)
    try:
        assert not runtime.is_chat_allowed(CHANNEL)
    finally:
        os.chmod(store.seal, 0o600)
    assert runtime.is_chat_allowed(CHANNEL)
    os.chmod(store.file, 0)
    try:
        assert not runtime.is_chat_allowed(CHANNEL)
    finally:
        os.chmod(store.file, 0o600)
    # An unreadable grants file counts as lost: putting it back needs approval.
    assert not runtime.is_chat_allowed(CHANNEL)


def test_a_valid_edit_with_the_right_token_is_live(store):
    """The control for the test above: the same edit made valid is accepted."""
    assert not runtime.is_chat_allowed(5)
    data = read_json(store.file)
    data["subjects"]["5"] = {"state": "granted", "generation": 2}
    with open(store.file + ".new", "w") as fh:
        json.dump(data, fh)
    os.replace(store.file + ".new", store.file)
    assert runtime.is_chat_allowed(5) and runtime.is_chat_allowed(CHANNEL)


def test_broken_seal_denies_everything(store):
    assert runtime.is_chat_allowed(CHANNEL)
    with open(store.seal + ".new", "w") as fh:
        fh.write("[]")
    os.replace(store.seal + ".new", store.seal)
    assert not runtime.is_chat_allowed(CHANNEL)


@pytest.mark.parametrize(
    "name,value",
    [("TELEGRAM_ALLOWED_CHAT_IDS", "@somechat"), ("TELEGRAM_RESTRICTED_CHAT_IDS", "1,x")],
)
def test_invalid_settings_deny_everything(store, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert runtime.is_chat_allowlist_enabled()


def test_seal_in_same_file_is_refused(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_GRANTS_SEAL_FILE", store.file + "/../grants.json")
    assert not runtime.is_chat_allowed(CHANNEL)
    with pytest.raises(grants.StoreError, match="different file"):
        grants.config()
    assert not os.path.exists(store.file)


def test_restored_file_needs_new_approval(store):
    run(ask(approver(), PERSON, "Ann"))
    assert runtime.is_chat_allowed(PERSON)
    backup = open(store.file).read()
    # Any later write changes the token.
    run(grants.revoke(GROUP))
    with open(store.file + ".new", "w") as fh:
        fh.write(backup)
    os.replace(store.file + ".new", store.file)
    assert not runtime.is_chat_allowed(PERSON)
    assert not runtime.is_chat_allowed(CHANNEL)
    # Approving one does not wake the others.
    assert run(ask(approver(), PERSON, "Ann")) == {"result": "granted"}
    assert runtime.is_chat_allowed(PERSON)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert read_json(store.file)["subjects"][str(CHANNEL)]["state"] == "unconfirmed"


def test_file_without_seal_counts_as_restored(store):
    assert runtime.is_chat_allowed(CHANNEL)
    os.unlink(store.seal)
    assert not runtime.is_chat_allowed(CHANNEL)


def test_rewriting_a_live_file_unchanged_keeps_it_active(store):
    assert runtime.is_chat_allowed(CHANNEL)
    backup = open(store.file).read()
    with open(store.file + ".new", "w") as fh:
        fh.write(backup)
    os.replace(store.file + ".new", store.file)
    assert runtime.is_chat_allowed(CHANNEL)


# --- what is readable -------------------------------------------------------


def test_restricted_and_service_chats_are_never_readable(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{CHANNEL},777000,{GROUP}")
    monkeypatch.setenv("TELEGRAM_RESTRICTED_CHAT_IDS", str(GROUP))
    assert runtime.is_chat_allowed(CHANNEL)
    assert not runtime.is_chat_allowed(777000)
    assert not runtime.is_chat_allowed(GROUP)
    assert runtime.get_effective_allowed_chat_ids() == {CHANNEL}


def test_a_user_grant_does_not_open_a_channel_with_the_same_bare_id(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1234567890")
    assert runtime.is_chat_allowed(None, user(1234567890))
    assert not runtime.is_chat_allowed(None, ENTITIES[CHANNEL])
    assert not runtime.is_chat_allowed(CHANNEL, ENTITIES[CHANNEL])
    assert not runtime.is_chat_allowed(None, group(1234567890))


def test_booleans_are_not_chat_ids(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    assert runtime.is_chat_allowed(1)
    assert not runtime.is_chat_allowed(True)
    assert grants.canonical_id(True) is None and grants.canonical_id(False) is None


def test_marked_ids_only(store):
    # The channel's bare id, or a user whose id equals it, is another chat.
    assert not runtime.is_chat_allowed(1234567890)
    assert not runtime.is_chat_allowed(None, user(1234567890))
    assert runtime.is_chat_allowed(None, ENTITIES[CHANNEL])
    assert runtime.is_chat_allowed(str(CHANNEL))
    # A name is matched only through its resolved entity.
    assert not runtime.is_chat_allowed("news")
    assert runtime.is_chat_allowed("news", ENTITIES[CHANNEL])
    assert not runtime.is_chat_allowed(True)


def test_upstream_list_chats_shows_granted_only(store, monkeypatch):
    client = SimpleNamespace(
        get_dialogs=FakeClient().get_dialogs,
    )

    async def get_dialogs(limit=None, archived=None):
        return [
            SimpleNamespace(
                entity=e,
                unread_count=0,
                archived=False,
                dialog=SimpleNamespace(
                    unread_mark=False, notify_settings=SimpleNamespace(mute_until=None)
                ),
            )
            for e in ENTITIES.values()
        ]

    client.get_dialogs = get_dialogs
    monkeypatch.setattr(chats, "get_client", lambda account=None: client)
    monkeypatch.setattr(chats, "ensure_connected", lambda cl=None: asyncio.sleep(0))
    out = run(chats.list_chats(limit=50))
    assert "News" in out
    assert "Ann" not in out and "Book club" not in out


# --- asking the approver ----------------------------------------------------


def test_only_an_explicit_allow_grants(store):
    calls = []
    result = run(ask(approver(calls=calls), PERSON, "Ann Lee"))
    assert result == {"result": "granted"}
    assert runtime.is_chat_allowed(PERSON)
    url, body, timeout = calls[0]
    assert url == "http://approver:9001/grant"
    assert body == {
        "messenger": "telegram",
        "account": "telegram-test",
        "subject": str(PERSON),
        "alias": "Ann Lee",
        "warnings": [],
    }
    assert timeout == 600


@pytest.mark.parametrize(
    "status,answer,code",
    [
        (403, {"allow": False, "code": "rejected"}, "rejected"),
        (200, {"allow": False}, "denied"),
        (200, {"allow": "true"}, "denied"),
        (403, {"allow": True}, "denied"),
        (200, [], "denied"),
        (500, {"code": "x" * 41}, "denied"),
        (403, {"code": "ok "}, "denied"),
    ],
)
def test_anything_else_denies(store, status, answer, code):
    result = run(ask(approver(status, answer), PERSON, "Ann"))
    assert result == {"result": "denied", "code": code}
    assert not runtime.is_chat_allowed(PERSON)
    assert str(PERSON) not in read_json(store.file)["subjects"]


def test_unreachable_approver(store):
    async def post(url, body, timeout):
        raise ConnectionError("refused")

    result = run(ask(post, PERSON, "Ann"))
    assert result == {"result": "error", "code": "approver_unreachable"}
    assert not runtime.is_chat_allowed(PERSON)


@pytest.mark.parametrize("name", ["TELEGRAM_GRANT_URL", "TELEGRAM_GRANT_ACCOUNT"])
def test_approver_not_configured(store, monkeypatch, name):
    monkeypatch.delenv(name)
    result = run(ask(approver(), PERSON, "Ann"))
    assert result == {"result": "error", "code": "not_configured"}


def test_one_pending_request_per_chat(store):
    async def scenario():
        gate = asyncio.Event()
        first = asyncio.create_task(ask(approver(wait=gate), PERSON, "Ann"))
        await asyncio.sleep(0)
        second = await ask(approver(), PERSON, "Ann")
        assert PERSON in grants._pending
        gate.set()
        return second, await first

    second, first = run(scenario())
    assert second == {"result": "denied", "code": "pending"}
    assert first == {"result": "granted"}


def test_revoke_withdraws_a_pending_request(store):
    async def scenario():
        gate = asyncio.Event()
        request = asyncio.create_task(ask(approver(wait=gate), PERSON, "Ann"))
        await asyncio.sleep(0)
        await grants.revoke(PERSON)
        return await request

    assert run(scenario()) == {"result": "denied", "code": "withdrawn"}
    assert not runtime.is_chat_allowed(PERSON)
    assert read_json(store.file)["subjects"][str(PERSON)] == {"state": "revoked", "generation": 1}


def test_allow_after_a_revoke_is_not_written(store, monkeypatch):
    """The approver answers allow, but a revoke happened since the request began."""

    async def post(url, body, timeout):
        # The revoke lands while the answer is on its way back.
        grants._pending[PERSON] = None
        await grants.revoke(PERSON)
        return 200, {"allow": True}

    result = run(ask(post, PERSON, "Ann"))
    assert result == {"result": "denied", "code": "revoked_meanwhile"}
    assert not runtime.is_chat_allowed(PERSON)


def test_client_disconnect_cancels_the_request(store):
    async def scenario():
        gate = asyncio.Event()
        calls = []
        request = asyncio.create_task(ask(approver(wait=gate, calls=calls), PERSON, "Ann"))
        await asyncio.sleep(0.01)
        inner = grants._pending[PERSON]
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        return inner

    inner = run(scenario())
    assert inner.cancelled()
    assert not runtime.is_chat_allowed(PERSON)


# --- the revoke barrier -----------------------------------------------------


def barrier_manager(log):
    manager = SimpleNamespace()

    async def call_tool(name, arguments, context=None, convert_result=False):
        if "gate" in arguments:
            await arguments["gate"].wait()
        log.append(name)
        return name

    manager.call_tool = call_tool
    server = SimpleNamespace(_tool_manager=manager)
    grants.install_call_barrier(server)
    grants.install_call_barrier(server)  # idempotent
    return manager


def test_a_call_overlapping_a_revoke_returns_an_error(store):
    log = []
    manager = barrier_manager(log)

    async def scenario():
        gate = asyncio.Event()
        running = asyncio.create_task(manager.call_tool("get_history", {"gate": gate}))
        await asyncio.sleep(0)
        await grants.revoke(PERSON)
        gate.set()
        with pytest.raises(Exception, match="changed during this call"):
            await running
        return await manager.call_tool("list_chats", {})

    assert run(scenario()) == "list_chats"
    # The read itself ran; only its result was withheld.
    assert log == ["get_history", "list_chats"]


def test_a_revoke_after_the_read_but_before_the_return(store):
    """The read has its text when the revoke lands, just before the result
    goes back: the result must still be withheld."""
    manager = SimpleNamespace()

    async def call_tool(name, arguments, context=None, convert_result=False):
        await asyncio.sleep(0)
        text = "secret"
        await grants.revoke(PERSON)
        return text

    manager.call_tool = call_tool
    grants.install_call_barrier(SimpleNamespace(_tool_manager=manager))
    with pytest.raises(Exception, match="changed during this call"):
        run(manager.call_tool("get_history", {}))


def test_barrier_skips_grant_tools(store):
    log = []
    manager = barrier_manager(log)

    async def scenario():
        gate = asyncio.Event()
        running = asyncio.create_task(manager.call_tool("request_access", {"gate": gate}))
        await asyncio.sleep(0)
        await grants.revoke(PERSON)
        gate.set()
        return await running

    assert run(scenario()) == "request_access"


def test_barrier_is_installed_on_the_server():
    assert getattr(runtime.mcp._tool_manager.call_tool, "_grants_barrier", False)


# --- the tools --------------------------------------------------------------


def test_list_access_chats(store):
    out = json.loads(run(access.list_access_chats()))
    assert out["next_offset"] is None
    rows = {r["chat_id"]: r for r in out["chats"]}
    assert set(rows) == {CHANNEL, GROUP, PERSON}
    assert rows[CHANNEL] == {
        "chat_id": CHANNEL,
        "name": "News",
        "type": "Channel",
        "access": "granted",
    }
    assert rows[PERSON]["name"] == "Ann Lee" and rows[PERSON]["access"] == "none"
    assert set(rows[GROUP]) == {"chat_id", "name", "type", "access"}


def test_list_access_chats_hides_restricted_and_pages(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_RESTRICTED_CHAT_IDS", str(GROUP))
    first = json.loads(run(access.list_access_chats(offset=0, limit=2)))
    assert [r["chat_id"] for r in first["chats"]] == [CHANNEL]
    assert first["next_offset"] == 2
    second = json.loads(run(access.list_access_chats(offset=2, limit=2)))
    assert [r["chat_id"] for r in second["chats"]] == [PERSON]
    assert second["next_offset"] == 4
    last = json.loads(run(access.list_access_chats(offset=4, limit=2)))
    assert last == {"chats": [], "next_offset": None}


@pytest.mark.parametrize("offset,limit", [(-1, 10), (0, 0), (0, 101), (1990, 20)])
def test_list_access_chats_bad_page(store, offset, limit):
    assert json.loads(run(access.list_access_chats(offset, limit)))["code"] == "bad_page"


def test_list_access_chats_shows_unconfirmed(store):
    assert runtime.is_chat_allowed(CHANNEL)
    os.unlink(store.seal)
    rows = json.loads(run(access.list_access_chats()))["chats"]
    assert {r["chat_id"]: r["access"] for r in rows}[CHANNEL] == "unconfirmed"


def test_request_access_grants_on_approval(store, monkeypatch):
    calls = []
    monkeypatch.setattr(access, "_post", approver(calls=calls))
    assert json.loads(run(access.request_access(str(PERSON)))) == {"result": "granted"}
    assert calls[0][1]["alias"] == "Ann Lee"
    assert runtime.is_chat_allowed(PERSON)


@pytest.mark.parametrize(
    "chat_id,code",
    [
        (777000, "not_allowed"),
        (SELF, "not_allowed"),
        (BOT, "not_allowed"),
        (GROUP, "not_allowed"),  # restricted below
        (999, "unknown_chat"),
        ("@ann", "bad_chat_id"),
        ("1.5", "bad_chat_id"),
        (" ", "bad_chat_id"),
        ("١٢", "bad_chat_id"),
    ],
)
def test_request_access_refuses_without_a_card(store, monkeypatch, chat_id, code):
    monkeypatch.setenv("TELEGRAM_RESTRICTED_CHAT_IDS", str(GROUP))
    calls = []
    monkeypatch.setattr(access, "_post", approver(calls=calls))
    out = json.loads(run(access.request_access(chat_id)))
    assert out == {"result": "denied", "code": code}
    assert calls == []


def test_request_access_refuses_an_entity_of_another_id(store, monkeypatch):
    calls = []
    monkeypatch.setattr(access, "_post", approver(calls=calls))
    access.get_client().entities = {**ENTITIES, 1234567890: user(1234567890)}
    try:
        # Asked for the user, resolved to something else.
        access.get_client().entities[PERSON] = ENTITIES[GROUP]
        out = json.loads(run(access.request_access(PERSON)))
    finally:
        access.get_client().entities = ENTITIES
    assert out == {"result": "denied", "code": "unknown_chat"}
    assert calls == []


def test_request_access_for_a_granted_chat_needs_no_card(store, monkeypatch):
    calls = []
    monkeypatch.setattr(access, "_post", approver(calls=calls))
    out = json.loads(run(access.request_access(CHANNEL)))
    assert out == {"result": "granted", "code": "already"}
    assert calls == []


def test_request_access_with_unusable_store(store, monkeypatch):
    calls = []
    monkeypatch.setattr(access, "_post", approver(calls=calls))
    runtime.is_chat_allowed(CHANNEL)
    os.unlink(store.file)
    out = json.loads(run(access.request_access(PERSON)))
    assert out == {"result": "error", "code": "store_unavailable"}
    assert calls == []


def test_revoke_access_tool(store):
    assert json.loads(run(access.revoke_access(str(CHANNEL)))) == {"result": "revoked"}
    assert not runtime.is_chat_allowed(CHANNEL)
    assert json.loads(run(access.revoke_access("x")))["code"] == "bad_chat_id"


def test_tools_off_without_store(monkeypatch):
    monkeypatch.delenv("TELEGRAM_GRANTS_FILE", raising=False)
    for call in (access.list_access_chats(), access.request_access(1), access.revoke_access(1)):
        assert json.loads(run(call))["code"] == "not_configured"


# --- review findings: resolution, bots, a revoke during a request -----------


class GuessingClient:
    """get_entity knows the channel only by its marked id, like a cold cache
    where the user with the same bare id is unknown."""

    def __init__(self):
        self.asked = []

    async def get_entity(self, identifier):
        self.asked.append(identifier)
        if identifier == CHANNEL:
            return ENTITIES[CHANNEL]
        if identifier == BOT:
            return ENTITIES[BOT]
        raise ValueError("Could not find the input entity")

    async def get_dialogs(self, *args, **kwargs):
        return []


@pytest.fixture
def no_connect(monkeypatch):
    async def connected(cl=None):
        return None

    monkeypatch.setattr(runtime, "ensure_connected", connected)


def test_resolution_never_guesses_another_chat(store, monkeypatch, no_connect):
    """A granted user id must not resolve to the channel with that bare id."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1234567890")
    assert runtime.is_chat_allowed(1234567890)
    client = GuessingClient()
    with pytest.raises(Exception):
        run(runtime.resolve_entity(1234567890, client))
    assert CHANNEL not in client.asked


def test_resolution_guesses_without_grants(monkeypatch, no_connect):
    """The control: upstream guessing is unchanged when grants are off."""
    monkeypatch.delenv("TELEGRAM_GRANTS_FILE", raising=False)
    found = run(runtime.resolve_entity(1234567890, GuessingClient()))
    assert found is ENTITIES[CHANNEL]


def test_check_resolved(store):
    grants.check_resolved(CHANNEL, ENTITIES[CHANNEL])
    grants.check_resolved("news", ENTITIES[CHANNEL])
    with pytest.raises(ValueError):
        grants.check_resolved(1234567890, ENTITIES[CHANNEL])
    with pytest.raises(ValueError):
        grants.check_resolved(str(PERSON), ENTITIES[GROUP])
    with pytest.raises(ValueError):
        grants.check_resolved("helper_bot", ENTITIES[BOT])


def test_a_granted_bot_is_never_readable(store, monkeypatch, no_connect):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{CHANNEL},{BOT}")
    assert not runtime.is_chat_allowed(BOT, ENTITIES[BOT])
    assert not runtime.is_chat_allowed(None, ENTITIES[BOT])
    with pytest.raises(Exception):
        run(runtime.resolve_entity(BOT, GuessingClient()))


@pytest.mark.parametrize("chat_id", [BOT, SELF])
def test_request_access_refuses_a_granted_bot_or_self_before_already(store, monkeypatch, chat_id):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", f"{CHANNEL},{BOT},{SELF}")
    out = json.loads(run(access.request_access(chat_id)))
    assert out == {"result": "denied", "code": "not_allowed"}


def test_a_revoke_while_the_request_looks_the_chat_up(store, monkeypatch):
    calls = []
    monkeypatch.setattr(access, "_post", approver(calls=calls))
    client = access.get_client()
    gate = asyncio.Event()
    original = client.get_entity

    async def slow_get_entity(chat_id):
        await gate.wait()
        return await original(chat_id)

    monkeypatch.setattr(client, "get_entity", slow_get_entity)

    async def scenario():
        request = asyncio.create_task(access.request_access(PERSON))
        await asyncio.sleep(0)
        assert PERSON in grants._pending
        await grants.revoke(PERSON)
        gate.set()
        return json.loads(await request)

    assert run(scenario()) == {"result": "denied", "code": "revoked_meanwhile"}
    assert calls == []
    assert not runtime.is_chat_allowed(PERSON)
    assert PERSON not in grants._pending


def test_a_second_request_while_the_first_looks_up(store, monkeypatch):
    monkeypatch.setattr(access, "_post", approver())
    client = access.get_client()
    gate = asyncio.Event()
    original = client.get_entity

    async def slow_get_entity(chat_id):
        await gate.wait()
        return await original(chat_id)

    monkeypatch.setattr(client, "get_entity", slow_get_entity)

    async def scenario():
        first = asyncio.create_task(access.request_access(PERSON))
        await asyncio.sleep(0)
        second = json.loads(await access.request_access(PERSON))
        gate.set()
        return second, json.loads(await first)

    second, first = run(scenario())
    assert second == {"result": "denied", "code": "pending"}
    assert first == {"result": "granted"}


# --- review round 2: contact history, channel comments, seal failure --------

from datetime import datetime, timezone  # noqa: E402

from telethon.tl import types as tl  # noqa: E402

from telegram_mcp.tools import contacts, messages  # noqa: E402

DATE = datetime(2026, 9, 16, tzinfo=timezone.utc)
DISCUSSION = -1000000009999


def tl_message(msg_id, peer, text):
    msg = tl.Message(id=msg_id, peer_id=peer, date=DATE, message=text)
    msg._sender = SimpleNamespace(first_name="Ann", last_name=None, username=None)
    return msg


class HistoryClient:
    def __init__(self, found, msgs):
        self.found = found
        self.msgs = msgs

    async def get_entity(self, identifier):
        return self.found

    async def get_messages(self, entity, limit=None, reply_to=None, **kwargs):
        return list(self.msgs)

    async def get_dialogs(self, *args, **kwargs):
        return []


@pytest.fixture
def history(store, monkeypatch, no_connect):
    def install(module, client):
        monkeypatch.setattr(module, "get_client", lambda account=None: client)
        monkeypatch.setattr(runtime, "get_client", lambda account=None: client)

    async def nothing(*args, **kwargs):
        return {}

    monkeypatch.setattr(messages.transcription, "prefetch_transcripts", nothing)
    monkeypatch.setattr(messages, "topics_for_messages", nothing)
    return install


@pytest.mark.parametrize("granted", [False, True])
def test_last_interaction_needs_a_grant(history, monkeypatch, granted):
    seed = f"{CHANNEL},{PERSON}" if granted else str(CHANNEL)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", seed)
    client = HistoryClient(ENTITIES[PERSON], [tl_message(1, tl.PeerUser(PERSON), "secret")])
    history(contacts, client)
    out = run(contacts.get_last_interaction(contact_id=PERSON))
    assert ("secret" in out) is granted
    if not granted:
        assert "restricted" in out


@pytest.mark.parametrize("discussion_granted", [False, True])
def test_channel_comments_need_their_own_grant(history, monkeypatch, discussion_granted):
    seed = f"{CHANNEL},{DISCUSSION}" if discussion_granted else str(CHANNEL)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", seed)
    comments = [
        tl_message(5, tl.PeerChannel(9999), "comment text"),
        tl_message(6, tl.PeerChannel(1234567890), "post text"),
    ]
    history(messages, HistoryClient(ENTITIES[CHANNEL], comments))
    out = run(messages.get_history(chat_id=CHANNEL, topic_id=17))
    assert "post text" in out
    assert ("comment text" in out) is discussion_granted


def test_a_failed_seal_replacement_keeps_everything_closed(store, monkeypatch):
    assert runtime.is_chat_allowed(CHANNEL)
    good = open(store.file).read()
    real_write = grants._write_atomic
    failing = {"on": True}

    def write(path, data):
        if failing["on"] and path == store.seal:
            raise OSError("no space")
        return real_write(path, data)

    monkeypatch.setattr(grants, "_write_atomic", write)
    os.unlink(store.file)
    assert not runtime.is_chat_allowed(CHANNEL)
    # The backup goes back while the old token is still in the seal.
    with open(store.file, "w") as fh:
        fh.write(good)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert not runtime.is_chat_allowed(CHANNEL)
    # Once the seal can be replaced, the restored file still needs approval.
    failing["on"] = False
    assert not runtime.is_chat_allowed(CHANNEL)
    assert grants.config().store.load().subjects[CHANNEL].state == "unconfirmed"


def test_a_seal_directory_that_cannot_be_written_closes_everything(store):
    assert runtime.is_chat_allowed(CHANNEL)
    seal_dir = os.path.dirname(store.seal)
    os.chmod(seal_dir, 0o500)
    try:
        assert not runtime.is_chat_allowed(CHANNEL)
    finally:
        os.chmod(seal_dir, 0o700)
    assert runtime.is_chat_allowed(CHANNEL)


# --- review round 3: a lost file while the seal directory is read-only ------


def test_lost_file_while_seal_directory_is_read_only(store):
    assert runtime.is_chat_allowed(CHANNEL)
    good = open(store.file).read()
    seal_dir = os.path.dirname(store.seal)
    os.chmod(seal_dir, 0o500)
    try:
        assert not runtime.is_chat_allowed(CHANNEL)
        os.unlink(store.file)
        assert not runtime.is_chat_allowed(CHANNEL)
        with open(store.file, "w") as fh:
            fh.write(good)
        assert not runtime.is_chat_allowed(CHANNEL)
    finally:
        os.chmod(seal_dir, 0o700)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert grants.config().store.load().subjects[CHANNEL].state == "unconfirmed"


def test_read_only_seal_directory_alone_needs_no_new_approval(store):
    """The control: with the grants file untouched, nothing is re-approved."""
    assert runtime.is_chat_allowed(CHANNEL)
    seal_dir = os.path.dirname(store.seal)
    os.chmod(seal_dir, 0o500)
    try:
        assert not runtime.is_chat_allowed(CHANNEL)
        assert not runtime.is_chat_allowed(CHANNEL)
    finally:
        os.chmod(seal_dir, 0o700)
    assert runtime.is_chat_allowed(CHANNEL)


# --- review round 4: a lost file while the seal cannot be read --------------


def test_lost_file_while_seal_is_unreadable(store):
    assert runtime.is_chat_allowed(CHANNEL)
    good = open(store.file).read()
    os.chmod(store.seal, 0)
    try:
        assert not runtime.is_chat_allowed(CHANNEL)
        os.unlink(store.file)
        assert not runtime.is_chat_allowed(CHANNEL)
        with open(store.file, "w") as fh:
            fh.write(good)
        assert not runtime.is_chat_allowed(CHANNEL)
    finally:
        os.chmod(store.seal, 0o600)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert grants.config().store.load().subjects[CHANNEL].state == "unconfirmed"


def test_unreadable_seal_alone_needs_no_new_approval(store):
    """The control: with the grants file untouched, nothing is re-approved."""
    assert runtime.is_chat_allowed(CHANNEL)
    os.chmod(store.seal, 0)
    try:
        assert not runtime.is_chat_allowed(CHANNEL)
        assert not runtime.is_chat_allowed(CHANNEL)
    finally:
        os.chmod(store.seal, 0o600)
    assert runtime.is_chat_allowed(CHANNEL)


def test_a_lasting_loss_rotates_the_seal_once(store):
    assert runtime.is_chat_allowed(CHANNEL)
    os.unlink(store.file)
    for _ in range(3):
        assert not runtime.is_chat_allowed(CHANNEL)
    token = read_json(store.seal)["token"]
    for _ in range(3):
        assert not runtime.is_chat_allowed(CHANNEL)
    assert read_json(store.seal)["token"] == token


@pytest.mark.parametrize("seal_writable", [True, False])
def test_restart_with_the_file_already_lost(store, monkeypatch, seal_writable):
    """After a restart nothing is trusted yet; a loss seen first still
    rotates the seal (or latches it), so the backup put back stays closed."""
    assert runtime.is_chat_allowed(CHANNEL)
    good = open(store.file).read()
    os.unlink(store.file)
    monkeypatch.setattr(grants, "_config_env", None)  # a restart
    real_write = grants._write_atomic
    failing = {"on": not seal_writable}

    def write(path, data):
        if failing["on"] and path == store.seal:
            raise OSError("no space")
        return real_write(path, data)

    monkeypatch.setattr(grants, "_write_atomic", write)
    assert not runtime.is_chat_allowed(CHANNEL)
    with open(store.file, "w") as fh:
        fh.write(good)
    assert not runtime.is_chat_allowed(CHANNEL)
    failing["on"] = False
    assert not runtime.is_chat_allowed(CHANNEL)
    assert grants.config().store.load().subjects[CHANNEL].state == "unconfirmed"


# --- review round 5: the grants directory cannot be searched ----------------


def test_lost_file_while_grants_directory_is_inaccessible(store):
    assert runtime.is_chat_allowed(CHANNEL)
    good = open(store.file).read()
    grants_dir = os.path.dirname(store.file)
    os.chmod(grants_dir, 0o600)  # no search permission
    try:
        assert not runtime.is_chat_allowed(CHANNEL)
        os.chmod(grants_dir, 0o700)
        os.unlink(store.file)
        with open(store.file, "w") as fh:
            fh.write(good)
        os.chmod(grants_dir, 0o600)
        assert not runtime.is_chat_allowed(CHANNEL)
    finally:
        os.chmod(grants_dir, 0o700)
    assert not runtime.is_chat_allowed(CHANNEL)
    assert grants.config().store.load().subjects[CHANNEL].state == "unconfirmed"


# --- final review: file tools must not reach the store ----------------------


def test_file_roots_overlapping_the_store_disable_file_tools(store, tmp_path):
    grants_dir = os.path.dirname(store.file)
    seal_dir = os.path.dirname(store.seal)
    other = tmp_path / "downloads"
    other.mkdir()
    for roots in ([tmp_path], [grants_dir], [seal_dir], [os.path.join(grants_dir, "sub")]):
        assert grants.roots_overlap(roots), roots
    assert not grants.roots_overlap([other])
    link = tmp_path / "link"
    link.symlink_to(grants_dir)
    assert grants.roots_overlap([link])


def test_ensure_allowed_roots_refuses_overlap(store, monkeypatch, tmp_path):
    async def roots(ctx):
        return [tmp_path], "ok"

    monkeypatch.setattr(runtime, "_get_effective_allowed_roots_with_status", roots)
    found, error = run(runtime._ensure_allowed_roots(None, "download_media"))
    assert found == [] and "grants store" in error
    monkeypatch.delenv("TELEGRAM_GRANTS_FILE")
    found, error = run(runtime._ensure_allowed_roots(None, "download_media"))
    assert found == [tmp_path] and error is None


# --- the chat's small photo (TELEGRAM_GRANT_PHOTO) --------------------------

JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 8 + b"\xff\xd9"  # 2054 bytes: padded base64


def photo_client(monkeypatch, parts=(JPEG,), error=None, wait=None, none=False):
    """Give the fake client a download_profile_photo that writes ``parts`` to
    the given file; it records each call and how many parts were taken."""
    seen = SimpleNamespace(calls=[], written=0, refused=None, limit=None)

    async def download_profile_photo(entity, file=None, download_big=True):
        seen.calls.append((entity, download_big))
        seen.limit = file.limit
        if wait is not None:
            await asyncio.sleep(wait)
        if error is not None:
            raise error
        if none:
            return None
        for part in parts:
            try:
                file.write(part)
            except Exception as e:
                seen.refused = e
                raise
            seen.written += 1
        return file

    monkeypatch.setattr(
        access.get_client(), "download_profile_photo", download_profile_photo, raising=False
    )
    return seen


def request(monkeypatch, chat_id=PERSON):
    calls = []
    monkeypatch.setattr(access, "_post", approver(calls=calls))
    out = run(access.request_access(chat_id))
    return out, calls


def test_photo_flag_off_sends_todays_body(store, monkeypatch):
    monkeypatch.delenv("TELEGRAM_GRANT_PHOTO", raising=False)
    seen = photo_client(monkeypatch)
    out, calls = request(monkeypatch)
    assert json.loads(out) == {"result": "granted"}
    assert calls[0][1] == {
        "messenger": "telegram",
        "account": "telegram-test",
        "subject": str(PERSON),
        "alias": "Ann Lee",
        "warnings": [],
    }
    assert seen.calls == []


@pytest.mark.parametrize("value", ["0", "true", ""])
def test_photo_flag_other_values_are_off(store, monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO", value)
    seen = photo_client(monkeypatch)
    out, calls = request(monkeypatch)
    assert "photo" not in calls[0][1] and seen.calls == []


@pytest.mark.parametrize("chat_id", [PERSON, GROUP])
def test_photo_is_sent_as_padded_base64_and_never_returned(store, monkeypatch, chat_id):
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO", "1")
    seen = photo_client(monkeypatch)
    out, calls = request(monkeypatch, chat_id)
    photo = calls[0][1]["photo"]
    assert photo == base64.b64encode(JPEG).decode()
    assert photo.endswith("=") and base64.b64decode(photo, validate=True) == JPEG
    assert seen.calls == [(ENTITIES[chat_id], False)]
    assert json.loads(out) == {"result": "granted"}
    assert photo not in out and photo[:16] not in out


@pytest.mark.parametrize(
    "case",
    ["no photo", "timeout", "not a jpeg", "error", "empty"],
)
def test_photo_left_out(store, monkeypatch, case):
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO", "1")
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO_TIMEOUT", "0.05")
    kwargs = {
        "no photo": {"none": True},
        "timeout": {"wait": 1},
        "not a jpeg": {"parts": (b"\x89PNG\r\n\x1a\n" + bytes(100),)},
        "error": {"error": RuntimeError("flood wait")},
        "empty": {"parts": ()},
    }[case]
    seen = photo_client(monkeypatch, **kwargs)
    out, calls = request(monkeypatch)
    assert json.loads(out) == {"result": "granted"}
    assert len(seen.calls) == 1 and "photo" not in calls[0][1]


def test_photo_download_stops_at_32_kib(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO", "1")
    part = b"\xff\xd8\xff" + bytes(16 * 1024 - 3)
    seen = photo_client(monkeypatch, parts=(part, bytes(16 * 1024), bytes(16 * 1024)))
    out, calls = request(monkeypatch)
    assert seen.written == 2 and seen.refused is not None
    assert json.loads(out) == {"result": "granted"}
    assert "photo" not in calls[0][1]


def test_photo_of_exactly_32_kib_is_sent(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO", "1")
    part = b"\xff\xd8\xff" + bytes(16 * 1024 - 3)
    photo_client(monkeypatch, parts=(part, bytes(16 * 1024)))
    _, calls = request(monkeypatch)
    assert base64.b64decode(calls[0][1]["photo"]) == part + bytes(16 * 1024)


@pytest.mark.parametrize("chat_id", [CHANNEL, BOT, 999])
def test_photo_not_fetched_without_a_request(store, monkeypatch, chat_id):
    """Already granted, never grantable or unknown: no download."""
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO", "1")
    seen = photo_client(monkeypatch)
    out, calls = request(monkeypatch, chat_id)
    assert calls == [] and seen.calls == []


def test_ask_approver_adds_photo_only_when_given(store):
    calls = []
    run(ask(approver(calls=calls)))
    assert "photo" not in calls[0][1]


# --- the photo's size limit and timeout are settings ------------------------

KIB16 = 16 * 1024


def jpeg_parts(n):
    """n parts of 16 KiB, the first starting with the JPEG marker."""
    return (b"\xff\xd8\xff" + bytes(KIB16 - 3),) + (bytes(KIB16),) * (n - 1)


def photo_on(monkeypatch, **env):
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO", "1")
    for name in ("MAX_BYTES", "TIMEOUT"):
        monkeypatch.delenv(f"TELEGRAM_GRANT_PHOTO_{name}", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(f"TELEGRAM_GRANT_PHOTO_{name}", value)


def timeouts(monkeypatch):
    """The timeouts passed to asyncio.wait_for from now on."""
    seen = []
    real = asyncio.wait_for

    async def wait_for(aw, timeout):
        seen.append(timeout)
        return await real(aw, timeout)

    monkeypatch.setattr(access.asyncio, "wait_for", wait_for)
    return seen


def test_photo_max_bytes_raised_sends_a_larger_photo(store, monkeypatch):
    photo_on(monkeypatch, MAX_BYTES="50000")
    seen = photo_client(monkeypatch, parts=jpeg_parts(3))
    _, calls = request(monkeypatch)
    assert len(base64.b64decode(calls[0][1]["photo"])) == 3 * KIB16
    assert seen.limit == 50000


def test_photo_max_bytes_lowered_refuses_the_second_part(store, monkeypatch):
    photo_on(monkeypatch, MAX_BYTES=str(KIB16))
    seen = photo_client(monkeypatch, parts=jpeg_parts(2))
    out, calls = request(monkeypatch)
    assert seen.written == 1 and seen.refused is not None
    assert "photo" not in calls[0][1] and json.loads(out) == {"result": "granted"}


def test_photo_of_exactly_the_configured_limit_is_sent(store, monkeypatch):
    photo_on(monkeypatch, MAX_BYTES=str(2 * KIB16))
    photo_client(monkeypatch, parts=jpeg_parts(2))
    _, calls = request(monkeypatch)
    assert len(base64.b64decode(calls[0][1]["photo"])) == 2 * KIB16


def test_photo_timeout_setting(store, monkeypatch):
    photo_on(monkeypatch, TIMEOUT="5")
    waits = timeouts(monkeypatch)
    photo_client(monkeypatch)
    _, calls = request(monkeypatch)
    assert calls[0][1]["photo"] == base64.b64encode(JPEG).decode()
    assert 5.0 in waits


@pytest.mark.parametrize(
    "name,value,expected",
    [
        ("MAX_BYTES", "1", 1),
        ("MAX_BYTES", "1048576", 1048576),
        ("MAX_BYTES", " 20000 ", 20000),
        ("TIMEOUT", "0.001", 0.001),
        ("TIMEOUT", "60", 60.0),
    ],
)
def test_photo_settings_valid_edges(store, monkeypatch, caplog, name, value, expected):
    photo_on(monkeypatch, **{name: value})
    waits = timeouts(monkeypatch)
    seen = photo_client(monkeypatch, parts=(JPEG[:1],) if expected == 1 else (JPEG,))
    caplog.set_level("WARNING", logger="telegram_mcp.access")
    out, calls = request(monkeypatch)
    assert len(seen.calls) == 1 and len(calls) == 1
    if name == "MAX_BYTES":
        assert seen.limit == expected
    else:
        assert expected in waits
    assert caplog.records == []


@pytest.mark.parametrize(
    "name,value",
    [
        ("MAX_BYTES", "0"),
        ("MAX_BYTES", "-1"),
        ("MAX_BYTES", "+5"),
        ("MAX_BYTES", "1_000"),
        ("MAX_BYTES", "32k"),
        ("MAX_BYTES", "1e4"),
        ("MAX_BYTES", "\u0661\u0662"),
        ("MAX_BYTES", "1048577"),
        ("MAX_BYTES", "9" * 5000),
        ("TIMEOUT", "0"),
        ("TIMEOUT", "-1"),
        ("TIMEOUT", "nan"),
        ("TIMEOUT", "inf"),
        ("TIMEOUT", "-inf"),
        ("TIMEOUT", "60.001"),
        ("TIMEOUT", "abc"),
    ],
)
def test_photo_settings_invalid(store, monkeypatch, caplog, name, value):
    photo_on(monkeypatch, **{name: value})
    seen = photo_client(monkeypatch)
    caplog.set_level("DEBUG", logger="telegram_mcp.access")
    out, calls = request(monkeypatch)
    assert len(calls) == 1 and "photo" not in calls[0][1]
    assert json.loads(out) == {"result": "granted"}
    assert seen.calls == []
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and len(caplog.records) == 1
    message = errors[0].getMessage()
    assert f"TELEGRAM_GRANT_PHOTO_{name}" in message and value.strip() not in message


@pytest.mark.parametrize("value", ["", "   "])
def test_photo_settings_empty_mean_the_defaults(store, monkeypatch, value):
    photo_on(monkeypatch, MAX_BYTES=value, TIMEOUT=value)
    waits = timeouts(monkeypatch)
    seen = photo_client(monkeypatch)
    _, calls = request(monkeypatch)
    assert seen.limit == 32768 and 10.0 in waits
    assert "photo" in calls[0][1]


def test_photo_flag_off_ignores_invalid_settings(store, monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_GRANT_PHOTO", raising=False)
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO_MAX_BYTES", "nope")
    monkeypatch.setenv("TELEGRAM_GRANT_PHOTO_TIMEOUT", "nope")
    seen = photo_client(monkeypatch)
    caplog.set_level("DEBUG", logger="telegram_mcp.access")
    _, calls = request(monkeypatch)
    assert calls[0][1] == {
        "messenger": "telegram",
        "account": "telegram-test",
        "subject": str(PERSON),
        "alias": "Ann Lee",
        "warnings": [],
    }
    assert seen.calls == [] and caplog.records == []


def test_invalid_photo_setting_is_logged_with_the_runtime_logging(store, monkeypatch):
    """The server logs at ERROR only: the message must get through its real
    logger and handler levels, with no level lowered for the test."""
    root = logging.getLogger("telegram_mcp")
    assert root.level == logging.ERROR == runtime.console_handler.level
    got = []
    handler = logging.Handler(level=runtime.console_handler.level)
    handler.emit = got.append
    root.addHandler(handler)
    try:
        photo_on(monkeypatch, MAX_BYTES="32k")
        photo_client(monkeypatch)
        request(monkeypatch)
    finally:
        root.removeHandler(handler)
    assert [(r.name, r.levelno, r.getMessage()) for r in got] == [
        (
            "telegram_mcp.access",
            logging.ERROR,
            "TELEGRAM_GRANT_PHOTO_MAX_BYTES is invalid; photo left out",
        )
    ]
