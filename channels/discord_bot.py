"""Discord channel — ears and mouth only. Every judgment lives in core/."""

import logging
import os

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import pipeline, storage
from core.inbox import Coalescer

load_dotenv()
log = logging.getLogger(__name__)

# Empty = listen everywhere. Set it: one API call per burst still adds up.
CHANNELS = {c.strip() for c in os.getenv("DISCORD_CHANNELS", "").split(",") if c.strip()}


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
        await interaction.followup.send(_receipt(result))
        await _say(result, interaction.channel.send)


def _receipt(result) -> str:
    if result is None:
        return "Logged nothing — that didn't look like design work."
    record = result.entry.record
    lines = [f"**{record.title}**", f"`{record.stage.value}` · `{record.subteam.value}`"]
    if record.missing_fields:
        lines.append("still missing: " + ", ".join(record.missing_fields))
    return "\n".join(lines)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True, messages=True, message_content=True))
        self.tree = app_commands.CommandTree(self)
        # One thought is often four messages. Buffer per person per channel and
        # hand core the whole burst; see core/inbox.py.
        self.bursts = Coalescer(self._flush_burst)

    async def setup_hook(self):
        await storage.init_schema()

        @self.tree.command(name="log", description="Log work the team did offline")
        async def _log(interaction: discord.Interaction):
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
        await _say(result, last.reply)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Bot().run(os.environ["DISCORD_TOKEN"])
