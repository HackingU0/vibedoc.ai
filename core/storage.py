"""Persistence for logged design records — PostgreSQL + pgvector.

Layout decision worth knowing: the DesignRecord is stored **whole, as jsonb**,
and the handful of columns we actually query on (stage, subteam, component) are
Postgres generated columns derived from that jsonb. During the scoring loop the
schema changes constantly — §7 says fields are earned by appearing in real
samples — and this way adding or dropping a record field needs no migration,
while the query columns can never drift out of sync with the record they came
from. Envelope metadata (created_at, author, follow-up state) are real columns,
because they are facts the channel supplied rather than model output.

Semantic search is optional. If no embedding key is configured, everything here
works and `search()` simply returns nothing — the notebook must never depend on
a vector store being reachable.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

import asyncpg
from dotenv import load_dotenv

from .schema import DesignRecord, LoggedEntry, Stage

log = logging.getLogger(__name__)

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))

_pool: Optional[asyncpg.Pool] = None
_embedder = None

SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS entries (
    entry_id            text PRIMARY KEY,
    channel             text NOT NULL DEFAULT 'unknown',
    source              text NOT NULL DEFAULT 'ambient',
    channel_message_id  text,
    author              text,
    created_at          timestamptz NOT NULL,
    raw_text            text NOT NULL,
    record              jsonb NOT NULL,

    -- The follow-up conversation, whole, in the order it happened. jsonb for
    -- the same reason `record` is jsonb: the shape is still moving, and a
    -- generated column can never drift out of sync with what it derives from.
    followups jsonb NOT NULL DEFAULT '[]'::jsonb,

    embedding vector({EMBEDDING_DIM}),

    -- Derived from the record so they can never disagree with it. component_key
    -- is folded for grouping ("Intake" and "intake" are one design thread) while
    -- the record keeps the team's own capitalisation untouched.
    stage         text GENERATED ALWAYS AS (record->>'stage') STORED,
    subteam       text GENERATED ALWAYS AS (record->>'subteam') STORED,
    component_key text GENERATED ALWAYS AS
                  (lower(btrim(coalesce(record->>'component', '')))) STORED,

    -- The routing key: the message id a reply must target to count as an
    -- answer, and NULL once that question has been answered — otherwise later
    -- chatter in the thread would overwrite an answer that already landed.
    open_followup_message_id text GENERATED ALWAYS AS (
        CASE WHEN followups -> -1 ->> 'answered_at' IS NULL
             THEN followups -> -1 ->> 'message_id' END
    ) STORED
);

CREATE INDEX IF NOT EXISTS entries_created_at_idx ON entries (created_at);
CREATE INDEX IF NOT EXISTS entries_component_idx  ON entries (component_key);
CREATE INDEX IF NOT EXISTS entries_stage_idx      ON entries (stage);

-- The follow-up routing lookup: a channel resolves "this is a reply to message
-- N" into the entry whose question it answers. Hot path, one row. Doubles as
-- the index behind the per-channel open-question budget.
CREATE INDEX IF NOT EXISTS entries_open_followup_idx
    ON entries (open_followup_message_id)
    WHERE open_followup_message_id IS NOT NULL;

-- Redelivery guard. Discord will hand you the same message twice across a
-- reconnect; without this you pay for a second parse and log a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS entries_channel_msg_idx
    ON entries (channel, channel_message_id) WHERE channel_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS entries_embedding_idx
    ON entries USING hnsw (embedding vector_cosine_ops);
"""

_COLUMNS = """entry_id, channel, source, channel_message_id, author, created_at,
              raw_text, record, followups"""


# ── Connection ────────────────────────────────────────────────────────────────

async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set — see .env.example")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


async def init_schema() -> None:
    """Create the table, indexes and the vector extension. Safe to re-run.

    CREATE EXTENSION needs rights the app role may not have on a locked-down
    instance; on Supabase it is available, elsewhere a DBA may need to run that
    one line by hand.
    """
    async with (await pool()).acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ── Embeddings (optional) ─────────────────────────────────────────────────────

def _embed_text(entry: LoggedEntry) -> str:
    """What actually gets embedded.

    The record's content, not raw_text: raw_text carries "@everyone" noise and
    the record carries the cleaned-up substance plus the team's own part names.
    """
    r = entry.record
    parts = [
        r.title,
        r.summary,
        r.component,
        r.problem_statement,
        "; ".join(r.alternatives_considered) or None,
        r.rationale,
        r.test_evidence,
    ]
    return "\n".join(p for p in parts if p)


async def embed(text: str) -> Optional[list[float]]:
    """Return an embedding, or None when embeddings are not configured.

    DeepSeek has no embeddings endpoint, so this is a second vendor seam. It is
    deliberately the only one outside core/agent.py, and it is allowed to fail
    softly — search is a nice-to-have, persistence is not.
    """
    global _embedder
    key = os.getenv("EMBEDDING_API_KEY")
    if not key:
        return None
    if _embedder is None:
        from openai import AsyncOpenAI

        _embedder = AsyncOpenAI(
            api_key=key, base_url=os.getenv("EMBEDDING_BASE_URL") or None
        )
    try:
        result = await _embedder.embeddings.create(model=EMBEDDING_MODEL, input=text)
    except Exception:
        log.exception("embedding request failed")
        return None
    return result.data[0].embedding


def _vector_literal(values: list[float]) -> str:
    """pgvector accepts its text form, which avoids a codec registration and
    keeps the dependency list to asyncpg alone."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


# ── Row <-> LoggedEntry ───────────────────────────────────────────────────────

def _to_entry(row: asyncpg.Record) -> LoggedEntry:
    data = dict(row)
    raw = data.pop("record")
    followups = data.pop("followups", None) or []
    return LoggedEntry(
        **data,
        record=DesignRecord.model_validate(
            json.loads(raw) if isinstance(raw, str) else raw
        ),
        followups=json.loads(followups) if isinstance(followups, str) else followups,
    )


# ── Writes ────────────────────────────────────────────────────────────────────

async def save(entry: LoggedEntry, *, reembed: bool = True) -> LoggedEntry:
    """Insert or update one entry, keyed on entry_id.

    Used both for the first write and for the post-follow-up update, which is
    why it is an upsert: a merged record replaces the one it was built from
    rather than becoming a second row. The design thread stays one row per
    message.
    """
    vector = None
    if reembed:
        values = await embed(_embed_text(entry))
        vector = _vector_literal(values) if values else None

    async with (await pool()).acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO entries ({_COLUMNS}, embedding)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::vector)
            ON CONFLICT (entry_id) DO UPDATE SET
                record     = EXCLUDED.record,
                followups  = EXCLUDED.followups,
                embedding  = COALESCE(EXCLUDED.embedding, entries.embedding)
            """,
            entry.entry_id, entry.channel, entry.source, entry.channel_message_id,
            entry.author, entry.created_at, entry.raw_text,
            entry.record.model_dump_json(),
            json.dumps([t.model_dump(mode="json") for t in entry.followups]),
            vector,
        )
    return entry


# ── Reads ─────────────────────────────────────────────────────────────────────

async def get(entry_id: str) -> Optional[LoggedEntry]:
    async with (await pool()).acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM entries WHERE entry_id = $1", entry_id
        )
    return _to_entry(row) if row else None


async def find_by_open_followup(message_id: str) -> Optional[LoggedEntry]:
    """The follow-up routing lookup.

    A channel resolves "this message replies to N" into the entry whose live
    question N is, and hands it to pipeline.handle_reply. No match means the
    reply is an ordinary message and goes down the ambient path instead.

    Scoped by the generated column to *unanswered* questions only, so once a
    reply has landed, later chatter in the same thread cannot overwrite it.
    """
    async with (await pool()).acquire() as conn:
        row = await conn.fetchrow(
            f"""SELECT {_COLUMNS} FROM entries
                WHERE open_followup_message_id = $1""",
            message_id,
        )
    return _to_entry(row) if row else None


async def list_thread(
    channel: str, component: Optional[str], *, limit: int = 20
) -> list[LoggedEntry]:
    """The recent entries in one component's design thread, oldest first.

    This is what stops the bot asking about a problem the team stated last
    Tuesday. Entries with no component share the "unfiled" bucket, which is
    loose enough that the caller should treat a hit there as weak evidence.
    """
    key = (component or "").strip().lower()
    async with (await pool()).acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT {_COLUMNS} FROM entries
                WHERE channel = $1 AND component_key = $2
                ORDER BY created_at DESC LIMIT $3""",
            channel, key, limit,
        )
    return [_to_entry(r) for r in reversed(rows)]


async def count_open_followups(
    channel: str, *, since: datetime, author: Optional[str] = None
) -> int:
    """How many questions this channel, or one author in it, is waiting on.

    The bot asking six things during one meeting is how it gets muted in week
    one. The optional author scope also stops one person who touches several
    components from receiving several questions at once.
    """
    async with (await pool()).acquire() as conn:
        return await conn.fetchval(
            """SELECT count(*) FROM entries
               WHERE channel = $1 AND created_at >= $2
                 AND open_followup_message_id IS NOT NULL
                 AND ($3::text IS NULL OR author = $3)""",
            channel, since, author,
        )


async def find_by_channel_message_id(
    channel: str, message_id: str
) -> Optional[LoggedEntry]:
    """Redelivery check. Call before parsing to avoid paying twice for a message
    Discord handed you again after a reconnect."""
    async with (await pool()).acquire() as conn:
        row = await conn.fetchrow(
            f"""SELECT {_COLUMNS} FROM entries
                WHERE channel = $1 AND channel_message_id = $2""",
            channel, message_id,
        )
    return _to_entry(row) if row else None


async def list_entries(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    subteam: Optional[str] = None,
    source: Optional[str] = None,
    include_unknown: bool = False,
) -> list[LoggedEntry]:
    """Everything the exporter needs, oldest first.

    `include_unknown` defaults to False because chitchat never enters a
    notebook — but the rows stay in the table on purpose: the exporter reports
    how many were dropped, and the scoring loop needs them to measure silence.
    """
    clauses, args = [], []

    def add(sql: str, value) -> None:
        args.append(value)
        clauses.append(sql.format(n=len(args)))

    if since is not None:
        add("created_at >= ${n}", since)
    if until is not None:
        add("created_at <= ${n}", until)
    if subteam is not None:
        add("subteam = ${n}", subteam)
    if source is not None:
        add("source = ${n}", source)
    if not include_unknown:
        add("stage <> ${n}", Stage.UNKNOWN.value)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with (await pool()).acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_COLUMNS} FROM entries {where} ORDER BY created_at", *args
        )
    return [_to_entry(r) for r in rows]


async def search(query: str, *, limit: int = 5) -> list[tuple[LoggedEntry, float]]:
    """Semantic search over the team's own records, best match first.

    This is what makes the archive answerable — "did we ever try compliant
    wheels?" mid-season, when nobody remembers which meeting that was. Returns
    an empty list when embeddings are not configured; callers must treat that as
    a normal outcome, not an error.
    """
    values = await embed(query)
    if not values:
        return []

    async with (await pool()).acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_COLUMNS}, 1 - (embedding <=> $1::vector) AS score
            FROM entries
            WHERE embedding IS NOT NULL AND stage <> $2
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            _vector_literal(values), Stage.UNKNOWN.value, limit,
        )

    out = []
    for row in rows:
        data = dict(row)
        score = data.pop("score")
        out.append((_to_entry(data), float(score)))
    return out
