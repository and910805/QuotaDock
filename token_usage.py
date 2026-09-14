"""Local Codex usage ledger. No prompts, credentials, or cumulative estimates.

The rollout format is an internal Codex format. Only explicit per-response usage
is accepted; unsupported records remain visible as coverage gaps.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

TAIWAN_TZ = timezone(timedelta(hours=8))
PERIODS = (("今日", "today"), ("近 7 天", "week"), ("本月", "month"), ("全部", "all"))
COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens", "total_tokens")
MAX_LINE_BYTES = 8 * 1024 * 1024
BATCH_LINES = 256
MAX_COUNTER = 10**15


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def period_bounds(period: str, now: datetime | None = None) -> tuple[float, float]:
    local = (now or datetime.now(TAIWAN_TZ)).astimezone(TAIWAN_TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = {"today": midnight, "week": midnight - timedelta(days=6),
             "month": midnight.replace(day=1), "all": datetime(1970, 1, 1, tzinfo=timezone.utc)}[period]
    return start.timestamp(), (midnight + timedelta(days=1)).timestamp()


def _timestamp(value) -> float:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.year < 1970:
        raise ValueError("timestamp must include timezone")
    return parsed.timestamp()


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _counts(usage) -> tuple[int | None, ...]:
    if not isinstance(usage, dict):
        raise ValueError("missing usage")
    values = []
    for key in COUNTERS:
        value = usage.get(key)
        if value is None and key not in ("input_tokens", "output_tokens", "total_tokens"):
            values.append(None)
        elif type(value) is not int or not 0 <= value <= MAX_COUNTER:
            raise ValueError("invalid usage counter")
        else:
            values.append(value)
    i, cache, write, out, reasoning, total = values
    if total != i + out or any(v is not None and v > i for v in (cache, write)):
        raise ValueError("inconsistent usage counters")
    if reasoning is not None and reasoning > out:
        raise ValueError("inconsistent reasoning counter")
    return tuple(values)


def open_shared(path: Path):
    """Permit a running Windows Codex writer to append or archive this file."""
    if os.name != "nt":
        return path.open("rb")
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


@dataclass(frozen=True)
class UsageGroup:
    model: str
    effort: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    reasoning_output_tokens: int | None
    responses: int
    turns: int


@dataclass(frozen=True)
class UsageReport:
    period: str
    groups: tuple[UsageGroup, ...]
    total_tokens: int
    responses: int
    first_at: float | None
    last_at: float | None
    issues: dict[str, int]
    sources: int
    updated_at: float


class TokenUsageStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Token 資料庫版本較新，請更新小工具。")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS contexts (
                    thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '', effort TEXT NOT NULL DEFAULT '',
                    ambiguous INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(turn_id));
                CREATE TABLE IF NOT EXISTS responses (
                    response_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL, input_tokens INTEGER NOT NULL,
                    cached_input_tokens INTEGER, cache_write_input_tokens INTEGER,
                    output_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER,
                    total_tokens INTEGER NOT NULL, conflict INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS responses_time ON responses(occurred_at);
                CREATE INDEX IF NOT EXISTS responses_turn ON responses(thread_id, turn_id);
                CREATE TABLE IF NOT EXISTS sources (
                    path TEXT PRIMARY KEY, identity TEXT NOT NULL DEFAULT '',
                    offset INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0,
                    mtime INTEGER NOT NULL DEFAULT 0, prefix TEXT NOT NULL DEFAULT '',
                    prefix_len INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT '{}',
                    bad_lines INTEGER NOT NULL DEFAULT 0, invalid_usage INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS legacy_turns (
                    thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    PRIMARY KEY(turn_id));
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def context(self, db, thread: str, turn: str, model: str, effort: str, ambiguous=False):
        if not thread or not turn:
            return
        # Codex turn UUIDs are global. Fork rollouts contain copied session_meta
        # entries; joining on the surrounding session's id misattributes children.
        old = db.execute("SELECT * FROM contexts WHERE turn_id=?", (turn,)).fetchone()
        if old:
            ambiguous = ambiguous or old["ambiguous"] or bool(
                (model and old["model"] and model != old["model"]) or
                (effort and old["effort"] and effort != old["effort"]))
            model, effort = old["model"] or model, old["effort"] or effort
        db.execute("INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)",
                   (thread, turn, model, effort, int(ambiguous)))

    def response(self, db, payload: dict, stamp, state: dict, copies_expected=False,
                 snapshots=False):
        response_id = _text(payload.get("response_id"))
        if not response_id:
            raise ValueError("missing response identity")
        thread = _text(payload.get("thread_id")) or _text(payload.get("session_id")) or state.get("thread", "")
        # Only use an explicit turn identity; the currently active turn could be
        # different from an asynchronous/subagent response's turn.
        turn = _text(payload.get("turn_id"))
        values = _counts(payload.get("usage"))
        timestamp = _timestamp(stamp)
        old = db.execute("SELECT * FROM responses WHERE response_id=?", (response_id,)).fetchone()
        if old:
            # Claude resume/fork copies whole records into a new session file, so a
            # duplicate that only changed its surrounding thread is not a conflict.
            same_identity = old["turn_id"] == turn and (copies_expected or old["thread_id"] == thread)
            if same_identity and tuple(old[k] for k in COUNTERS) == values:
                return
            if same_identity and snapshots:
                # Streamed transcripts repeat a response while it grows; the
                # snapshot with the largest total is the final one, regardless
                # of the order the copies are read in.
                if values[-1] > old["total_tokens"]:
                    db.execute("""UPDATE responses SET input_tokens=?, cached_input_tokens=?,
                        cache_write_input_tokens=?, output_tokens=?, reasoning_output_tokens=?,
                        total_tokens=? WHERE response_id=?""", (*values, response_id))
                return
            db.execute("UPDATE responses SET conflict=1 WHERE response_id=?", (response_id,))
            return
        db.execute("INSERT INTO responses VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                   (response_id, thread, turn, timestamp, *values))

    def report(self, period="today", now: datetime | None = None) -> UsageReport:
        start, end = period_bounds(period, now)
        with self.connection() as db:
            groups = tuple(UsageGroup(**dict(row)) for row in db.execute("""
                SELECT CASE WHEN c.ambiguous=1 THEN '' ELSE COALESCE(c.model,'') END model,
                       CASE WHEN c.ambiguous=1 THEN '' ELSE COALESCE(c.effort,'') END effort,
                       SUM(r.input_tokens) input_tokens, SUM(r.output_tokens) output_tokens,
                       SUM(r.total_tokens) total_tokens,
                       CASE WHEN COUNT(r.cached_input_tokens)=COUNT(*) THEN SUM(r.cached_input_tokens) END cached_input_tokens,
                       CASE WHEN COUNT(r.cache_write_input_tokens)=COUNT(*) THEN SUM(r.cache_write_input_tokens) END cache_write_input_tokens,
                       CASE WHEN COUNT(r.reasoning_output_tokens)=COUNT(*) THEN SUM(r.reasoning_output_tokens) END reasoning_output_tokens,
                       COUNT(*) responses, COUNT(DISTINCT r.thread_id || ':' || r.turn_id) turns
                FROM responses r LEFT JOIN contexts c USING(turn_id)
                WHERE r.conflict=0 AND r.occurred_at>=? AND r.occurred_at<?
                GROUP BY 1,2 ORDER BY total_tokens DESC,model,effort
            """, (start, end)))
            first, last = db.execute("SELECT MIN(occurred_at),MAX(occurred_at) FROM responses WHERE conflict=0").fetchone()
            issues = {
                "legacy": db.execute("""SELECT COUNT(*) FROM legacy_turns l WHERE NOT EXISTS
                    (SELECT 1 FROM responses r WHERE r.turn_id=l.turn_id)""").fetchone()[0],
                "conflict": db.execute("SELECT COUNT(*) FROM responses WHERE conflict=1").fetchone()[0],
                "unknown": db.execute("""SELECT COUNT(*) FROM responses r LEFT JOIN contexts c USING(turn_id)
                    WHERE r.conflict=0 AND (COALESCE(c.model,'')='' OR COALESCE(c.effort,'')='' OR c.ambiguous=1)""").fetchone()[0],
                "missing_details": db.execute("""SELECT COUNT(*) FROM responses WHERE conflict=0 AND
                    (cached_input_tokens IS NULL OR cache_write_input_tokens IS NULL OR reasoning_output_tokens IS NULL)""").fetchone()[0],
            }
            sources, bad, invalid, errors = db.execute("""SELECT COUNT(*),COALESCE(SUM(bad_lines),0),
                COALESCE(SUM(invalid_usage),0),COALESCE(SUM(error!=''),0) FROM sources""").fetchone()
            issues.update(bad_lines=bad, invalid_usage=invalid, read_errors=errors)
        return UsageReport(period, groups, sum(g.total_tokens for g in groups), sum(g.responses for g in groups),
                           first, last, issues, sources, datetime.now(timezone.utc).timestamp())

    def turns(self, period: str, model: str, effort: str, page=0, page_size=100, now=None) -> tuple[list[dict], int]:
        start, end = period_bounds(period, now)
        where = """FROM responses r LEFT JOIN contexts c USING(turn_id)
            WHERE r.conflict=0 AND r.occurred_at>=? AND r.occurred_at<?
            AND (CASE WHEN c.ambiguous=1 THEN '' ELSE COALESCE(c.model,'') END)=?
            AND (CASE WHEN c.ambiguous=1 THEN '' ELSE COALESCE(c.effort,'') END)=?
            GROUP BY r.thread_id,r.turn_id"""
        args = (start, end, model, effort)
        with self.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM (SELECT 1 " + where + ")", args).fetchone()[0]
            rows = db.execute("""SELECT r.thread_id,r.turn_id,MIN(r.occurred_at) first_at,
                SUM(r.input_tokens) input_tokens,SUM(r.output_tokens) output_tokens,
                SUM(r.total_tokens) total_tokens,COUNT(*) responses """ + where +
                " ORDER BY first_at DESC,r.thread_id,r.turn_id LIMIT ? OFFSET ?",
                (*args, page_size, page * page_size)).fetchall()
        return [dict(row) for row in rows], count


@dataclass(frozen=True)
class ScanProgress:
    completed: int
    files: int
    bytes_read: int


class TokenUsageCollector:
    FOLDERS = ("sessions", "archived_sessions")

    def __init__(self, store: TokenUsageStore, root: Path | None = None):
        self.store = store
        self.root = Path(root) if root is not None else self.default_root()

    @staticmethod
    def default_root() -> Path:
        return codex_home()

    def scan(self, discover=True, stop: threading.Event | None = None) -> Iterator[ScanProgress]:
        stop = stop or threading.Event()
        with self.store.connection() as db:
            paths = {Path(row[0]) for row in db.execute("SELECT path FROM sources")}
            if discover:
                for name in self.FOLDERS:
                    folder = self.root / name
                    if folder.exists():
                        # Walk errors must be reported, not mistaken for an empty history.
                        def raise_error(error):
                            raise error
                        for parent, _, files in os.walk(folder, onerror=raise_error):
                            paths.update(Path(parent) / f for f in files if f.endswith(".jsonl"))
            # Recent sources first, so today's statistics appear during backfill.
            ordered = sorted(paths, key=lambda p: p.name, reverse=True)
            bytes_read = 0
            for index, path in enumerate(ordered):
                if stop.is_set():
                    return
                try:
                    for consumed in self._read(db, path, stop):
                        bytes_read += consumed
                        yield ScanProgress(index, len(ordered), bytes_read)
                except FileNotFoundError:
                    # Archiving/deletion does not erase previously imported usage.
                    with db:
                        db.execute("UPDATE sources SET error='' WHERE path=?", (str(path),))
                except OSError:
                    with db:
                        db.execute("INSERT OR IGNORE INTO sources(path) VALUES (?)", (str(path),))
                        db.execute("UPDATE sources SET error='read' WHERE path=?", (str(path),))
                yield ScanProgress(index + 1, len(ordered), bytes_read)
            if not ordered:
                yield ScanProgress(0, 0, 0)

    def _read(self, db, path: Path, stop: threading.Event):
        stat = path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}"
        old = db.execute("SELECT * FROM sources WHERE path=?", (str(path),)).fetchone()
        with open_shared(path) as stream:
            prefix_len = old["prefix_len"] if old and old["prefix_len"] else min(1024, stat.st_size)
            prefix = hashlib.sha256(stream.read(prefix_len)).hexdigest()
            reset = not old or old["identity"] != identity or stat.st_size < old["offset"] or old["prefix"] != prefix
            if old and stat.st_size == old["offset"] and stat.st_mtime_ns != old["mtime"]:
                reset = True
            if old and not reset:
                checkpoint = json.loads(old["state"]).get("tail")
                stream.seek(max(0, old["offset"] - 512))
                if checkpoint and hashlib.sha256(stream.read(min(512, old["offset"]))).hexdigest() != checkpoint:
                    reset = True
            if not reset and old["offset"] == stat.st_size and old["mtime"] == stat.st_mtime_ns:
                if old["error"]:
                    with db:
                        db.execute("UPDATE sources SET error='' WHERE path=?", (str(path),))
                return
            if reset:
                stream.seek(0)
                prefix_len = min(1024, stat.st_size)
                prefix = hashlib.sha256(stream.read(prefix_len)).hexdigest()
            offset = 0 if reset else old["offset"]
            state = {} if reset else json.loads(old["state"])
            bad = 0 if reset else old["bad_lines"]
            invalid = 0 if reset else old["invalid_usage"]
            stream.seek(offset)
            finished = False
            while not finished and not stop.is_set():
                before = offset
                with db:
                    for _ in range(BATCH_LINES):
                        if stop.is_set() or stream.tell() >= stat.st_size:
                            finished = True
                            break
                        raw = stream.readline(MAX_LINE_BYTES + 1)
                        if not raw:
                            finished = True
                            break
                        oversized = len(raw) > MAX_LINE_BYTES
                        while oversized and not raw.endswith(b"\n"):
                            raw = stream.readline(64 * 1024)
                            if not raw:
                                break
                        if not raw.endswith(b"\n"):
                            # Leave the cursor at the start of an unfinished line.
                            finished = True
                            break
                        offset = stream.tell()
                        if oversized:
                            bad += 1
                            continue
                        try:
                            record = json.loads(raw)
                            if not isinstance(record, dict):
                                raise ValueError("invalid record")
                        except (ValueError, UnicodeError):
                            bad += 1
                            continue
                        try:
                            self._record(db, record, state)
                        except (ValueError, TypeError, OverflowError):
                            invalid += 1
                    position = stream.tell()
                    stream.seek(max(0, offset - 512))
                    state["tail"] = hashlib.sha256(stream.read(min(512, offset))).hexdigest()
                    stream.seek(position)
                    db.execute("""INSERT OR REPLACE INTO sources
                        (path,identity,offset,size,mtime,prefix,prefix_len,state,bad_lines,invalid_usage,error)
                        VALUES (?,?,?,?,?,?,?,?,?,?,'')""",
                        (str(path), identity, offset, stat.st_size, stat.st_mtime_ns, prefix, prefix_len,
                         json.dumps(state, separators=(",", ":")), bad, invalid))
                yield offset - before

    def _record(self, db, record: dict, state: dict):
        kind, payload = record.get("type"), record.get("payload")
        if kind not in ("session_meta", "turn_context", "token_usage_record", "event_msg"):
            return
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        if kind == "session_meta":
            state.setdefault("thread", _text(payload.get("id")))
        elif kind == "turn_context":
            turn = _text(payload.get("turn_id"))
            # Legacy contexts have no turn id. Keep them separate from explicit turns.
            state["turn"] = turn or "legacy:" + _text(record.get("timestamp"))
            if turn:
                self.store.context(db, state.get("thread", ""), turn,
                                   _text(payload.get("model")), _text(payload.get("effort")))
        elif kind == "token_usage_record":
            self.store.response(db, payload, record.get("timestamp"), state)
        elif kind == "event_msg":
            event = payload.get("type")
            if event == "task_started":
                state["turn"] = _text(payload.get("turn_id"))
            elif event in ("model_rerouted", "model/rerouted"):
                self.store.context(db, state.get("thread", ""),
                                   _text(payload.get("turn_id")) or state.get("turn", ""), "", "", True)
            elif event == "token_count":
                info = payload.get("info")
                total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
                total = total_usage.get("total_tokens") if isinstance(total_usage, dict) else None
                if type(total) is int and total > 0:
                    # Modern rollouts emit these alongside per-response records.
                    # Store only a coverage marker; never add these token totals.
                    if total != state.get("last_total"):
                        db.execute("INSERT OR IGNORE INTO legacy_turns VALUES (?,?)",
                                   (state.get("thread", ""), state.get("turn", "") or "legacy:" + _text(record.get("timestamp"))))
                    state["last_total"] = total
