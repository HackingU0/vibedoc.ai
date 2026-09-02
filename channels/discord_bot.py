"""Discord channel — ears and mouth only. Every judgment lives in core/."""

import logging
import os
from datetime import datetime

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import pipeline
from core.inbox import Coalescer
from core.schema import STAGE_ORDER, UNFILED

load_dotenv()
log = logging.getLogger(__name__)

# Empty = listen everywhere. Set it: one API call per burst still adds up.
CHANNELS = {c.strip() for c in os.getenv("DISCORD_CHANNELS", "").split(",") if c.strip()}

# Dev convenience: a global command sync can take an hour to appear, which makes
# /log untestable during a session. Setting this syncs to one guild instead,
# which is instant. Leave it unset in production.
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()

# The capture receipt. A reaction rather than a message on purpose: it says
# "this is in the notebook" without spending a turn in the channel. §8's rule
# is that the bot posts publicly and should stay quiet when in doubt — a
# reaction is how you acknowledge without talking. Absence of one is also
# information: chitchat never gets it, so triage is visible at a glance.
CAPTURED = "📓"

# Cards shown per column before the rest are rolled into a "+N more" line.
# Discord caps a field value at 1024 characters and the whole embed at 6000;
# blowing either is an HTTP 400, not a graceful degrade.
# ponytail: a flat cap, not a character budget. Ten cards is roughly 400
# characters — measure before making it cleverer.
BOARD_MAX_CARDS = 10
DIGEST_MAX_THREADS = 8
RECALL_FIELD_CHARS = 800
RECAP_MAX_THREADS = 5

SESSION_IDLE_SECONDS = float(os.getenv("SESSION_IDLE_MINUTES", "90")) * 60

BUCKETS = (
    ("almost", "One field from done"),
    ("untested", "Decided, never tested"),
    ("stale", "Nobody has touched these"),
)

# The four fields a design record is judged on, in reading order. Matches
# exporters/notebook.py's SECTIONS — the notebook and the receipt must never
# disagree about what "complete" means.
COVERAGE = [
    ("problem_statement", "Problem"),
    ("alternatives_considered", "Alternatives"),
    ("rationale", "Why"),
    ("test_evidence", "Results"),
]


def _needs(gaps) -> str:
    return ", ".join(label for name, label in COVERAGE if name in gaps) or "—"


def _roles(user) -> list[str]:
    """The author's role names, in Discord's own order.

    Reported, not interpreted — §4's rule. `progress.team()` decides which of
    these names a team; putting that `if` here is exactly the drift this file
    exists to avoid.

    No Members intent needed: a guild message carries its author's roles in the
    payload, so `message.author` is already a Member. In a DM it is a plain
    User with no roles, hence the getattr.
    """
    return [r.name for r in getattr(user, "roles", ())]


def _card(result) -> discord.Embed:
    """The /log receipt, as a card.

    Coverage is the thread's, not this entry's: core already made that
    distinction in `result.gaps`, and this only renders it.
    """
    record = result.entry.record
    embed = discord.Embed(
        title=record.title,
        description=record.summary,
        colour=discord.Colour.orange() if result.gaps else discord.Colour.green(),
    )
    embed.add_field(name="Stage", value=record.stage.value)
    embed.add_field(name="Subteam", value=record.subteam.value)
    embed.add_field(name="Component", value=record.component or "—")
    if result.related:
        embed.add_field(
            name="Related earlier",
            value="\n".join(
                f"**{entry.record.component or UNFILED}** — {entry.record.title}"
                for entry in result.related
            )[:1024],
            inline=False,
        )
    embed.add_field(
        name="This design thread so far",
        value=" · ".join(
            f"{'✗' if name in result.gaps else '✓'} {label}"
            for name, label in COVERAGE
        ),
        inline=False,
    )
    return embed


def _status_card(result) -> discord.Embed:
    if result.span is None:
        return discord.Embed(
            title="Nothing on the go",
            description="No design work logged for you in the last week.",
            colour=discord.Colour.greyple(),
        )

    span = result.span
    embed = discord.Embed(
        title=span.component or UNFILED,
        description=f"{result.entries} entr{'y' if result.entries == 1 else 'ies'} "
                    f"in this design thread",
        colour=discord.Colour.orange() if result.gaps else discord.Colour.green(),
    )
    # Discord renders <t:...> in each viewer's own timezone, which sidesteps
    # TEAM_TZ entirely for this surface (gotcha 8 is an export problem).
    embed.add_field(
        name="Active",
        value=f"<t:{int(span.started_at.timestamp())}:t>"
              f" – <t:{int(span.last_at.timestamp())}:t>",
        inline=False,
    )
    embed.add_field(
        name="Stages",
        value=" → ".join(dict.fromkeys(s.value for s in span.stages)),
        inline=False,
    )
    embed.add_field(
        name="Still missing",
        value=", ".join(label for name, label in COVERAGE if name in result.gaps)
              or "nothing — this thread is complete",
        inline=False,
    )
    return embed


def _board_card(board) -> discord.Embed:
    """The board as stage columns, with team names carried on each card."""
    columns = {stage: [] for stage in STAGE_ORDER}
    for team, lane in board.lanes.items():
        for stage in STAGE_ORDER:
            columns[stage].extend((team, span) for span in lane.get(stage, []))

    if not any(columns.values()):
        return discord.Embed(
            title="Nothing on the go",
            description="No design work logged in the last week.",
            colour=discord.Colour.greyple(),
        )

    visible = [cards for cards in columns.values() if cards]
    embed = discord.Embed(
        title="Board",
        colour=(
            discord.Colour.green()
            if all(any(span.is_open for _, span in cards) for cards in visible)
            else discord.Colour.orange()
        ),
    )
    one_team = len(board.lanes) == 1
    for stage, cards in columns.items():
        if not cards:
            continue
        cards.sort(key=lambda item: (item[1].is_open, item[1].last_at), reverse=True)
        lines = [
            f"{'●' if span.is_open else '○'} {span.component or UNFILED} "
            f"— {span.author or '—'}{'' if one_team else f' · {team}'}"
            for team, span in cards[:BOARD_MAX_CARDS]
        ]
        if len(cards) > BOARD_MAX_CARDS:
            lines.append(f"+{len(cards) - BOARD_MAX_CARDS} more")
        # ponytail: a blunt slice. Discord counts characters, not cards, and
        # over 1024 it answers HTTP 400 for the whole embed rather than
        # dropping a field — a line cut mid-word beats a dead command. Spend
        # a real character budget here only if long components turn out
        # common in the channel.
        embed.add_field(
            name=stage.value, value="\n".join(lines)[:1024], inline=True
        )

    empty = [stage.value for stage, cards in columns.items() if not cards]
    footer = [f"nothing yet in: {', '.join(empty)}"] if empty else []
    footer.append(f"last {pipeline.BOARD_WINDOW.days} days")
    embed.set_footer(text=" · ".join(footer))
    # Not <t:...> in the footer text: Discord parses markdown in descriptions
    # and field values, and nowhere else — a tag there reaches the reader
    # verbatim. embed.timestamp is the native slot and is localised per
    # viewer, which is what the tag was reaching for.
    embed.timestamp = board.since
    return embed


def _digest_card(digest) -> discord.Embed:
    total = (
        f"{digest.total} design thread{'' if digest.total == 1 else 's'} · "
        f"{digest.complete} complete"
    )
    if not (digest.almost or digest.untested or digest.stale):
        return discord.Embed(
            title="Nothing missing", description=total,
            colour=discord.Colour.green(),
        )

    embed = discord.Embed(
        title="What the season still needs", description=total,
        colour=discord.Colour.orange(),
    )
    for attr, label in BUCKETS:
        rows = getattr(digest, attr)
        if not rows:
            continue
        lines = [
            f"**{thread.component}** — needs {_needs(thread.gaps)}"
            for thread in rows[:DIGEST_MAX_THREADS]
        ]
        if len(rows) > DIGEST_MAX_THREADS:
            lines.append(f"+{len(rows) - DIGEST_MAX_THREADS} more")
        embed.add_field(
            name=f"{label} ({len(rows)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    return embed


def _recall_card(recall) -> discord.Embed:
    """Render archive hits without synthesising or filtering them."""
    if not recall.hits:
        why = (
            "Nothing in the notebook matches that yet."
            if recall.enabled
            else "Search is not configured — set EMBEDDING_API_KEY to switch "
                 "it on. Everything else keeps working without it."
        )
        return discord.Embed(
            title=recall.query[:256], description=why,
            colour=discord.Colour.greyple(),
        )

    embed = discord.Embed(
        title=recall.query[:256],
        description=f"{len(recall.hits)} from the team's own records",
        colour=discord.Colour.blurple(),
    )
    for entry, score in recall.hits:
        when = f"<t:{int(entry.created_at.timestamp())}:D>"
        body = " · ".join(
            part for part in (entry.record.summary, entry.author) if part
        )
        embed.add_field(
            name=f"{entry.record.component or UNFILED} · {score:.2f}"[:256],
            value=(
                f"**{entry.record.title}**\n{when} · {body}"
            )[:RECALL_FIELD_CHARS],
            inline=False,
        )
    return embed


def _recap_card(recap) -> discord.Embed:
    """Render the incomplete threads left by tonight's session."""
    embed = discord.Embed(
        title="Tonight's session",
        description=(
            f"{recap.entries} entries captured · {len(recap.threads)} thread"
            f"{'' if len(recap.threads) == 1 else 's'} still open"
        ),
        colour=discord.Colour.blurple(),
    )
    lines = [
        f"**{thread.component}** — needs {_needs(thread.gaps)}"
        for thread in recap.threads[:RECAP_MAX_THREADS]
    ]
    if len(recap.threads) > RECAP_MAX_THREADS:
        lines.append(f"+{len(recap.threads) - RECAP_MAX_THREADS} more")
    embed.add_field(
        name="Still missing", value="\n".join(lines)[:1024], inline=False,
    )
    embed.set_footer(text="/digest for the whole season")
    return embed


async def _receipt_reaction(message: discord.Message) -> None:
    """Mark a message as captured. Never allowed to break the capture itself."""
    try:
        await message.add_reaction(CAPTURED)
    except discord.HTTPException:
        # Missing Add Reactions, or the message was deleted mid-flush. The
        # record is already saved and a receipt is cosmetic — same rule
        # storage.save() applies to embeddings, where the optional half is
        # never allowed to take the durable half down with it.
        log.warning("could not add the capture reaction", exc_info=True)


async def _say(result, send) -> None:
    """Post the question core decided on, and tell core which id it got."""
    if result is None or not result.question:
        return
    msg = await send(result.question)
    await pipeline.mark_asked(result.entry, result.question, str(msg.id), at=msg.created_at)


class LogModal(discord.ui.Modal, title="Log what you worked on"):
    # ponytail: single free-text box. Add per-field inputs only if the model
    # keeps mis-parsing recaps, which the scoring loop would show first.
    text = discord.ui.TextInput(label="What happened?", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)  # not ephemeral — gotcha 7
        result = await pipeline.ingest(
            channel="discord",
            source="log",
            author=interaction.user.display_name,
            author_roles=_roles(interaction.user),
            created_at=interaction.created_at,
            raw_text=str(self.text),
        )
        if result is None:
            await interaction.followup.send(
                "Logged nothing — that didn't look like design work."
            )
            return
        await interaction.followup.send(embed=_card(result))
        await _say(result, interaction.channel.send)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True, messages=True, message_content=True))
        self.tree = app_commands.CommandTree(self)
        # One thought is often four messages. Buffer per person per channel and
        # hand core the whole burst; see core/inbox.py.
        self.bursts = Coalescer(self._flush_burst)
        # Reuse the same reset-on-activity timer for one recap per quiet-ended
        # channel session. It never size-flushes and is not drained on shutdown.
        self.sessions = Coalescer(
            self._session_ended, quiet=SESSION_IDLE_SECONDS, max_items=10**6
        )

    async def setup_hook(self):
        await pipeline.init_schema()

        @self.tree.command(name="log", description="Log work the team did offline")
        async def _log(interaction: discord.Interaction):
            if CHANNELS and str(interaction.channel_id) not in CHANNELS:
                await interaction.response.send_message(
                    "This bot isn't listening in this channel.", ephemeral=True
                )
                return
            await interaction.response.send_modal(LogModal())

        @self.tree.command(name="status", description="What you're working on right now")
        async def _status(interaction: discord.Interaction):
            # Ephemeral is right here and wrong for /log. Gotcha 7 makes a /log
            # receipt public because the follow-up round trip is keyed on
            # replying to it; a status card starts no round trip, and posting
            # one person's summary to the whole channel is pure noise.
            await interaction.response.defer(thinking=True, ephemeral=True)
            result = await pipeline.status(
                channel="discord", author=interaction.user.display_name
            )
            await interaction.followup.send(embed=_status_card(result), ephemeral=True)

        @self.tree.command(name="board", description="Who is on what right now")
        async def _board(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            result = await pipeline.board(channel="discord")
            await interaction.followup.send(embed=_board_card(result), ephemeral=True)

        @self.tree.command(name="digest", description="What the season still needs")
        async def _digest(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            result = await pipeline.digest(channel="discord")
            await interaction.followup.send(embed=_digest_card(result), ephemeral=True)

        @self.tree.command(name="ask", description="Did we ever try this before?")
        @app_commands.describe(query="What to look for in the team's own records")
        async def _ask(interaction: discord.Interaction, query: str):
            await interaction.response.defer(thinking=True, ephemeral=True)
            result = await pipeline.recall(query=query)
            await interaction.followup.send(embed=_recall_card(result), ephemeral=True)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self):
        # The last burst of a meeting is the one most likely to be the recap.
        await self.bursts.drain()
        await super().close()

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content.strip():
            return
        if CHANNELS and str(message.channel.id) not in CHANNELS:
            return

        try:
            await self._handle(message)
        except Exception:
            log.exception("dropping message %s", message.id)

    async def _handle(self, message: discord.Message):
        await self.sessions.add(str(message.channel.id), message.created_at)

        # A reply to one of our questions is an answer, not a new record, and it
        # is never buffered — it is already a deliberate, complete thought.
        if message.reference and message.reference.message_id:
            result = await pipeline.handle_reply(
                open_message_id=str(message.reference.message_id),
                raw_text=message.content,
                at=message.created_at,
            )
            if result is not None:
                # A reply that filled nothing ends the exchange in silence, by
                # design. Without this the person cannot tell "merged, nothing
                # to add" from "the bot fell over" — which is exactly how the
                # first live run read.
                await _receipt_reaction(message)
                await _say(result, message.reply)
                return

        await self.bursts.add(f"{message.channel.id}:{message.author.id}", message)

    async def _session_ended(self, key: str, stamps: list[datetime]) -> None:
        recap = await pipeline.session_recap(channel="discord", since=min(stamps))
        if recap is None:
            return
        channel = self.get_channel(int(key))
        if channel is not None:
            await channel.send(embed=_recap_card(recap))

    async def _flush_burst(self, key: str, messages: list[discord.Message]) -> None:
        """One person's burst, parsed as one unit.

        Anchored to the FIRST message: that id is the dedup key and it is stable
        across a reconnect that re-forms the same burst. The reply goes to the
        LAST one, which is where the conversation actually is.
        """
        first, last = messages[0], messages[-1]
        result = await pipeline.ingest(
            channel="discord",
            author=first.author.display_name,
            author_roles=_roles(first.author),
            created_at=first.created_at,
            channel_message_id=str(first.id),
            raw_text="\n".join(m.content for m in messages),
            # first.reference survives coalescing untouched — discord.Message
            # objects carry it natively. This is the id that _handle() already
            # tried against find_by_open_followup and got no match for; here
            # it is tried again as a peer-merge candidate instead.
            reply_to_message_id=(
                str(first.reference.message_id)
                if first.reference and first.reference.message_id
                else None
            ),
        )
        if result is not None:
            await _receipt_reaction(last)
        await _say(result, last.reply)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Bot().run(os.environ["DISCORD_TOKEN"])
