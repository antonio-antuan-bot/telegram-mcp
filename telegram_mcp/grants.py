"""Read grants kept in a file: chats are added on a human's approval.

Off unless ``TELEGRAM_GRANTS_FILE`` is set. When on, it replaces the
``TELEGRAM_ALLOWED_CHAT_IDS`` allowlist: a chat is readable only if the file
grants it, and that list only seeds a new, empty store. The file is re-read
when it changes, so no restart is needed.

Settings (all marked chat ids, e.g. ``-1001234567890``):

- ``TELEGRAM_GRANTS_FILE``: the grants, rewritten atomically. Back this one up.
- ``TELEGRAM_GRANTS_SEAL_FILE``: a token that changes on every write and is
  also stored in the grants file. Keep it out of backups: a grants file
  restored from a backup no longer matches it, and its grants then stay
  inactive until a human approves each again.
- ``TELEGRAM_RESTRICTED_CHAT_IDS``: never readable and never granted, even if
  the file says otherwise.
- ``TELEGRAM_GRANT_URL``: the approver's endpoint. ``request_access`` posts
  ``{"messenger", "account", "subject", "alias", "warnings"}`` there and
  grants only on a 200 answer with ``"allow": true``.
- ``TELEGRAM_GRANT_ACCOUNT``: the account name shown on the approval card.
- ``TELEGRAM_GRANT_TIMEOUT``: seconds to wait for the approver (600).
- ``TELEGRAM_GRANT_PHOTO``: ``1`` adds ``"photo"``, the chat's small profile
  photo as base64 JPEG, left out when there is none.
- ``TELEGRAM_GRANT_PHOTO_MAX_BYTES``: the photo's size limit in bytes (32768,
  at most 1048576).
- ``TELEGRAM_GRANT_PHOTO_TIMEOUT``: seconds for the photo download (10, at
  most 60). It runs before the approver is asked, so a `request_access` call
  can take up to this plus ``TELEGRAM_GRANT_TIMEOUT``. An invalid value of
  either leaves the photo out.

A missing, unreadable or inconsistent store denies every read.
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from mcp.server.fastmcp.exceptions import ToolError
from telethon import utils

logger = logging.getLogger("telegram_mcp.grants")

FORMAT_VERSION = 1
SERVICE_CHAT_IDS = frozenset({777000})
GRANTED, REVOKED, UNCONFIRMED = "granted", "revoked", "unconfirmed"
_STATES = (GRANTED, REVOKED, UNCONFIRMED)


class StoreError(Exception):
    """The store cannot be used; every read is denied."""


def enabled() -> bool:
    return bool(os.getenv("TELEGRAM_GRANTS_FILE"))


def parse_ids(raw: Optional[str], name: str) -> Set[int]:
    """Parse a comma list of marked chat ids; anything else is an error."""
    ids: Set[int] = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            raise StoreError(f"{name}: '{token}' is not a chat id") from None
    return ids


def canonical_id(value: Any) -> Optional[int]:
    """Return a marked chat id for an int or a decimal string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        body = text[1:] if text.startswith("-") else text
        if body.isascii() and body.isdigit() and len(body) <= 20:
            return int(text)
    return None


@dataclass
class Subject:
    state: str
    generation: int = 0


@dataclass
class Snapshot:
    """One consistent reading of the store."""

    subjects: Dict[int, Subject] = field(default_factory=dict)
    token: str = ""
    restored: bool = False


def _fsync_dir(path: str) -> None:
    fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: str, data: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    # mkstemp creates the file 0600.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".grants-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(path)


# A file whose metadata cannot be read: never equal to a trusted key.
_UNKNOWN = ("unknown",)


def _stat_key(path: str):
    """What changes when a file is replaced, written, chmod-ed or chown-ed."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return _UNKNOWN
    return (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


class Store:
    """The grants file and its seal, re-read whenever either changes."""

    def __init__(self, path: str, seal_path: str, seed: Set[int]):
        if not seal_path:
            raise StoreError("TELEGRAM_GRANTS_SEAL_FILE is not set")
        if os.path.abspath(seal_path) == os.path.abspath(path):
            raise StoreError("the seal must be a different file")
        self.path = path
        self.seal_path = seal_path
        self.seed = seed
        self._key = object()
        self._snapshot: Optional[Snapshot] = None
        self._error: Optional[str] = None
        # Set when the grants file was found lost or damaged but the seal
        # could not be replaced: nothing is trusted until it is.
        self._burn_owed = False
        # The grants file's key when it was last loaded without error.
        self._trusted_file = None

    def _key_now(self):
        return (_stat_key(self.path), _stat_key(self.seal_path))

    def _read_seal(self) -> Optional[str]:
        try:
            with open(self.seal_path, "rb") as fh:
                data = json.loads(fh.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise StoreError(f"seal unreadable: {type(e).__name__}") from None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise StoreError("seal malformed")
        return token

    def _read_file(self) -> Optional[dict]:
        try:
            with open(self.path, "rb") as fh:
                data = json.loads(fh.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise _FileLost(f"grants file unreadable: {type(e).__name__}") from None
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            raise _FileLost("grants file has an unknown format")
        return data

    def _parse(self, data: dict, seal: Optional[str]) -> Snapshot:
        token = data.get("token")
        raw = data.get("subjects")
        if not isinstance(token, str) or not isinstance(raw, dict):
            raise _FileLost("grants file malformed")
        restored = seal is None or not secrets.compare_digest(token, seal)
        subjects: Dict[int, Subject] = {}
        for key, entry in raw.items():
            chat_id = canonical_id(key)
            if chat_id is None or str(chat_id) != key or not isinstance(entry, dict):
                raise _FileLost("grants file malformed")
            state, generation = entry.get("state"), entry.get("generation")
            if state not in _STATES or type(generation) is not int or generation < 0:
                raise _FileLost("grants file malformed")
            if restored and state == GRANTED:
                state = UNCONFIRMED
            subjects[chat_id] = Subject(state, generation)
        return Snapshot(subjects, token, restored)

    def _burn_seal(self) -> None:
        """The grants file was lost or damaged: give the seal a token no file
        has, so that any copy put back (a backup, even one identical to the
        lost file) needs new approvals. If that fails, deny everything and
        retry on every load."""
        try:
            _write_atomic(self.seal_path, json.dumps({"token": secrets.token_hex(16)}).encode())
        except OSError:
            if not self._burn_owed:
                logger.error("grants seal could not be replaced, every read is denied")
            self._burn_owed = True
            return
        self._burn_owed = False
        # No copy of the old file can match the seal any more.
        self._trusted_file = None

    def _note_failure(self) -> None:
        """A load failed. If the grants file is no longer the one last
        trusted, it may have been lost and put back meanwhile (a backup can
        carry the old token): the seal must change before anything is
        trusted again."""
        if self._trusted_file is not None and _stat_key(self.path) != self._trusted_file:
            self._burn_owed = True

    def load(self) -> Snapshot:
        """Return the current snapshot, or raise StoreError."""
        if self._burn_owed:
            self._burn_seal()
            if self._burn_owed:
                self._key, self._error, self._snapshot = object(), "seal not replaced", None
                raise StoreError("grants seal could not be replaced")
        # Checked on every load: a directory's mode is not in the files' key.
        if not os.access(os.path.dirname(os.path.abspath(self.seal_path)), os.W_OK | os.X_OK):
            self._note_failure()
            self._key, self._error, self._snapshot = object(), "seal not writable", None
            raise StoreError("seal directory not writable")
        key = self._key_now()
        if _UNKNOWN in key:
            # The grants file cannot be checked against the one last trusted:
            # treat it as changed.
            self._note_failure()
            self._key, self._error, self._snapshot = object(), "store not accessible", None
            raise StoreError("grants store not accessible")
        if key == self._key:
            if self._error:
                raise StoreError(self._error)
            return self._snapshot
        try:
            seal = self._read_seal()
            try:
                data = self._read_file()
                if data is None and seal is not None:
                    raise _FileLost("grants file missing but its seal exists")
                if data is not None:
                    snapshot = self._parse(data, seal)
            except _FileLost:
                if seal is not None:
                    self._burn_seal()
                    key = self._key_now()
                raise
            if data is None:
                # A new store: the seed fills it once.
                try:
                    self._commit(Snapshot({i: Subject(GRANTED) for i in self.seed}))
                except OSError as e:
                    raise StoreError(f"grants store not writable: {type(e).__name__}") from None
                return self.load()
        except StoreError as e:
            self._note_failure()
            if self._error != str(e):
                logger.error("grants store unusable, every read is denied")
            self._key, self._error, self._snapshot = key, str(e), None
            raise StoreError(str(e)) from None
        if snapshot.restored and not (self._snapshot and self._snapshot.restored):
            logger.warning(
                "grants file does not match its seal (restored?): "
                "its grants stay inactive until approved again"
            )
        self._key, self._error, self._snapshot = key, None, snapshot
        self._trusted_file = key[0]
        return snapshot

    def _commit(self, snapshot: Snapshot) -> None:
        """Write the subjects under a new token, then the seal.

        A crash between the two writes leaves them unmatched: every grant
        then needs a new approval, which fails closed.
        """
        token = secrets.token_hex(16)
        body = {
            "version": FORMAT_VERSION,
            "token": token,
            "subjects": {
                str(k): {"state": v.state, "generation": v.generation}
                for k, v in sorted(snapshot.subjects.items())
            },
        }
        _write_atomic(self.path, (json.dumps(body, indent=1) + "\n").encode())
        _write_atomic(self.seal_path, json.dumps({"token": token}).encode())

    def update(self, chat_id: int, change) -> Subject:
        """Apply ``change(subject_or_None) -> Subject`` and write the store."""
        snapshot = self.load()
        subjects = dict(snapshot.subjects)
        subjects[chat_id] = change(subjects.get(chat_id))
        self._commit(Snapshot(subjects))
        return self.load().subjects[chat_id]


class _FileLost(StoreError):
    """The grants file is gone or damaged while its seal exists."""


@dataclass
class _Config:
    store: Store
    restricted: Set[int]


_config: Optional[_Config] = None
_config_env: Optional[tuple] = None
_config_error: Optional[str] = None
_write_lock = asyncio.Lock()


def _env_tuple() -> tuple:
    return tuple(
        os.getenv(n)
        for n in (
            "TELEGRAM_GRANTS_FILE",
            "TELEGRAM_GRANTS_SEAL_FILE",
            "TELEGRAM_ALLOWED_CHAT_IDS",
            "TELEGRAM_RESTRICTED_CHAT_IDS",
        )
    )


def config() -> _Config:
    """The store and restricted list for the current environment."""
    global _config, _config_env, _config_error
    env = _env_tuple()
    if env != _config_env:
        _config_env, _config, _config_error = env, None, None
        try:
            path, seal, seed, restricted = env
            _config = _Config(
                Store(path, seal or "", parse_ids(seed, "TELEGRAM_ALLOWED_CHAT_IDS")),
                parse_ids(restricted, "TELEGRAM_RESTRICTED_CHAT_IDS"),
            )
        except StoreError as e:
            _config_error = str(e)
            logger.error("grants settings invalid, every read is denied")
    if _config is None:
        raise StoreError(_config_error or "not configured")
    return _config


def roots_overlap(roots) -> bool:
    """Whether any file-tool root contains, or lies inside, the grants or
    seal directory: file tools must never read or write the store."""
    if not enabled():
        return False
    stores = [
        os.path.realpath(os.path.dirname(os.path.abspath(p)))
        for p in (os.getenv("TELEGRAM_GRANTS_FILE"), os.getenv("TELEGRAM_GRANTS_SEAL_FILE"))
        if p
    ]
    for root in roots:
        r = os.path.realpath(str(root))
        for d in stores:
            if os.path.commonpath([r, d]) in (r, d):
                return True
    return False


def readable_ids() -> Set[int]:
    """Chat ids readable now; empty when the store is unusable."""
    try:
        cfg = config()
        snapshot = cfg.store.load()
    except StoreError:
        return set()
    return {
        i
        for i, s in snapshot.subjects.items()
        if s.state == GRANTED and i not in cfg.restricted and i not in SERVICE_CHAT_IDS
    }


def _is_bot(entity: Any) -> bool:
    return bool(getattr(entity, "bot", False))


def allows(chat_identifier: Any, entity: Any = None) -> bool:
    """Whether a chat may be read, by marked id only (never bare ids or names).

    A username or link counts only through its resolved entity. A bot is
    never readable, even if granted.
    """
    allowed = readable_ids()
    if entity is not None:
        try:
            return utils.get_peer_id(entity) in allowed and not _is_bot(entity)
        except Exception:
            return False
    chat_id = canonical_id(chat_identifier)
    return chat_id is not None and chat_id in allowed


def check_resolved(identifier: Any, resolved: Any) -> None:
    """Raise ValueError if a lookup by id found another chat, or a bot.

    Access is checked on the id the caller named, so the chat a tool then
    reads must be exactly that one.
    """
    if not enabled():
        return
    if _is_bot(resolved):
        raise ValueError("Bots cannot be read here.")
    chat_id = canonical_id(identifier)
    if chat_id is None:
        return
    try:
        found = utils.get_peer_id(resolved)
    except Exception:
        found = None
    if found != chat_id:
        raise ValueError(f"Chat {chat_id} not found.")


# ---------------------------------------------------------------------------
# Revocation barrier: a tool call during which any revoke happened returns an
# error instead of its result, so once a revoke returns, no read that may
# have passed the old check returns text.
# ---------------------------------------------------------------------------

_BARRIER_EXEMPT = frozenset({"request_access", "revoke_access", "list_access_chats"})
_revocations = 0


def install_call_barrier(server) -> None:
    """Wrap every tool call of a FastMCP server with the revocation check."""
    manager = server._tool_manager
    original = manager.call_tool
    if getattr(original, "_grants_barrier", False):
        return

    async def call_tool(name, arguments, *args, **kwargs):
        if name in _BARRIER_EXEMPT:
            return await original(name, arguments, *args, **kwargs)
        started = _revocations
        result = await original(name, arguments, *args, **kwargs)
        # No await between this check and the return.
        if _revocations != started:
            raise ToolError("Read access changed during this call; call again.")
        return result

    call_tool._grants_barrier = True
    manager.call_tool = call_tool


# ---------------------------------------------------------------------------
# Grant and revoke
# ---------------------------------------------------------------------------

# chat id -> the approver call in flight, or None while the request is still
# looking the chat up.
_pending: Dict[int, Optional[asyncio.Task]] = {}
_withdrawn: Set[int] = set()


async def revoke(chat_id: int) -> None:
    """Revoke a chat: raise its generation and withdraw its pending request.

    Calls already running end with an error instead of a result.
    """
    global _revocations
    cfg = config()
    async with _write_lock:
        cfg.store.update(
            chat_id,
            lambda s: Subject(REVOKED, (s.generation if s else 0) + 1),
        )
        _revocations += 1
    task = _pending.get(chat_id)
    if task is not None and not task.done():
        _withdrawn.add(chat_id)
        task.cancel()


async def grant_if_unchanged(chat_id: int, generation: int) -> bool:
    """Write a grant only if no revoke happened since the request began."""
    cfg = config()
    async with _write_lock:
        current = cfg.store.load().subjects.get(chat_id)
        if (current.generation if current else 0) != generation:
            return False
        cfg.store.update(chat_id, lambda s: Subject(GRANTED, generation))
        return True


def generation_of(chat_id: int) -> int:
    subject = config().store.load().subjects.get(chat_id)
    return subject.generation if subject else 0


def reserve(chat_id: int) -> Optional[int]:
    """Start a request: take the chat's pending slot and return its current
    generation, or None if a request for it is already running.

    Call before the request's first await, and ``release`` when it ends.
    Raises StoreError (the slot stays free) if the store is unusable.
    """
    if chat_id in _pending:
        return None
    generation = generation_of(chat_id)
    _pending[chat_id] = None
    return generation


def release(chat_id: int) -> None:
    _pending.pop(chat_id, None)
    _withdrawn.discard(chat_id)


async def ask_approver(
    post, chat_id: int, alias: str, generation: int, photo: Optional[bytes] = None
) -> Dict[str, Any]:
    """Run one approval for a reserved ``chat_id`` through
    ``post(url, body, timeout)``, which returns (status, decoded JSON or None).
    ``photo``, when given, is sent as ``"photo"`` in standard base64.

    ``generation`` is what ``reserve`` returned; a revoke since then denies,
    and a revoke while the card is open withdraws it.
    """
    url = os.getenv("TELEGRAM_GRANT_URL")
    account = os.getenv("TELEGRAM_GRANT_ACCOUNT")
    if not url or not account:
        return {"result": "error", "code": "not_configured"}
    try:
        timeout = float(os.getenv("TELEGRAM_GRANT_TIMEOUT", "600"))
    except ValueError:
        return {"result": "error", "code": "not_configured"}
    if generation_of(chat_id) != generation:
        return {"result": "denied", "code": "revoked_meanwhile"}
    body = {
        "messenger": "telegram",
        "account": account,
        "subject": str(chat_id),
        "alias": alias,
        "warnings": [],
    }
    if photo is not None:
        body["photo"] = base64.b64encode(photo).decode("ascii")
    task = asyncio.ensure_future(post(url, body, timeout))
    _pending[chat_id] = task
    try:
        status, answer = await task
    except asyncio.CancelledError:
        if chat_id in _withdrawn and task.cancelled():
            # Our own task was not cancelled: only the request was withdrawn.
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                return {"result": "denied", "code": "withdrawn"}
        raise
    except Exception:
        logger.warning("approver unreachable")
        return {"result": "error", "code": "approver_unreachable"}
    if status != 200 or not isinstance(answer, dict) or answer.get("allow") is not True:
        code = answer.get("code") if isinstance(answer, dict) else None
        if not isinstance(code, str) or not code.isascii() or len(code) > 40:
            code = "denied"
        return {"result": "denied", "code": code}
    if not await grant_if_unchanged(chat_id, generation):
        return {"result": "denied", "code": "revoked_meanwhile"}
    return {"result": "granted"}
