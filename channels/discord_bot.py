"""Discord channel — ears and mouth only. Every judgment lives in core/."""

import logging
import os

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import storage
from core.agent import apply_followup_answer, log_session, parse_design_record
from core.schema import LoggedEntry

load_dotenv()
log = logging.getLogger(__name__)

# Empty = listen everywhere. Set it: one API call per message adds up fast.
CHANNELS = {c.strip() for c in os.getenv("DISCORD_CHANNELS", "").split(",") if c.strip()}


async def _persist_and_reply(entry: LoggedEntry, send) -> None:
    """Save, then post the question if there is one and remember its id."""
    await storage.save(entry)
    if entry.record.followup_question:
        msg = await send(entry.record.followup_question)
        await storage.save(entry.mark_followup_asked(str(msg.id), at=msg.created_at))


class LogModal(discord.ui.Modal, title="Log what you worked on"):
    # ponytail: single free-text box. Add per-field inputs only if the model
    # keeps mis-parsing recaps, which the scoring loop would show first.
    text = discord.ui.TextInput(label="What happened?", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)  # not ephemeral — gotcha 7
        record = await log_session(str(self.text))
        entry = LoggedEntry(
            channel="discord",
            source="log",
            channel_message_id=None,
            author=interaction.user.display_name,
            created_at=interaction.created_at,
            raw_text=str(self.text),
            record=record,
        )
        await interaction.followup.send(_receipt(record))
        await _persist_and_reply(entry, interaction.channel.send)


def _receipt(record) -> str:
    lines = [f"**{record.title}**", f"`{record.stage.value}` · `{record.subteam.value}`"]
    if record.missing_fields:
        lines.append("still missing: " + ", ".join(record.missing_fields))
    return "\n".join(lines)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True, messages=True, message_content=True))
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await storage.init_schema()

        @self.tree.command(name="log", description="Log work the team did offline")
        async def _log(interaction: discord.Interaction):
            await interaction.response.send_modal(LogModal())

        await self.tree.sync()

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
        # A reply to one of our questions is an answer, not a new record.
        if message.reference and message.reference.message_id:
            entry = await storage.find_by_followup_message_id(str(message.reference.message_id))
            if entry:
                await storage.save(
                    await apply_followup_answer(entry, message.content, at=message.created_at)
                )
                return

        # Reconnects redeliver; don't pay for the same message twice.
        if await storage.find_by_channel_message_id("discord", str(message.id)):
            return

        record = await parse_design_record(message.content)
        entry = LoggedEntry(
            channel="discord",
            channel_message_id=str(message.id),
            author=message.author.display_name,
            created_at=message.created_at,
            raw_text=message.content,
            record=record,
        )
        await _persist_and_reply(entry, message.reply)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Bot().run(os.environ["DISCORD_TOKEN"])
