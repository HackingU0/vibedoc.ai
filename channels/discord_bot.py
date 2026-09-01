"""Discord channel — ears and mouth only. Every judgment lives in core/."""

import logging
import os

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import pipeline
from core.inbox import Coalescer

load_dotenv()
log = logging.getLogger(__name__)

# Empty = listen everywhere. Set it: one API call per burst still adds up.
CHANNELS = {c.strip() for c in os.getenv("DISCORD_CHANNELS", "").split(",") if c.strip()}

# The capture receipt. A reaction rather than a message on purpose: it says
# "this is in the notebook" without spending a turn in the channel. §8's rule
# is that the bot posts publicly and should stay quiet when in doubt — a
# reaction is how you acknowledge without talking. Absence of one is also
# information: chitchat never gets it, so triage is visible at a glance.
CAPTURED = "📓"

# The four fields a design record is judged on, in reading order. Matches
# exporters/notebook.py's SECTIONS — the notebook and the receipt must never
# disagree about what "complete" means.
COVERAGE = [
    ("problem_statement", "Problem"),
    ("alternatives_considered", "Alternatives"),
    ("rationale", "Why"),
    ("test_evidence", "Results"),
]


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
    embed.add_field(
        name="This design thread so far",
        value=" · ".join(
            f"{'✗' if name in result.gaps else '✓'} {label}"
            for name, label in COVERAGE
        ),
        inline=False,
    )
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
            created_at=first.created_at,
            channel_message_id=str(first.id),
            raw_text="\n".join(m.content for m in messages),
        )
        if result is not None:
            await _receipt_reaction(last)
        await _say(result, last.reply)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Bot().run(os.environ["DISCORD_TOKEN"])
