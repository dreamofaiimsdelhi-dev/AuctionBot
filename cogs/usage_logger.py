"""
cogs/usage_logger.py – Batched command usage reporter.

Tracks every command invocation (prefix and slash) in a local SQLite database.
Sends a formatted report to a designated log channel once every BATCH_SIZE uses,
then deletes those rows. No in-memory accumulation.

Setup:
  1. Set LOG_CHANNEL_ID below to your target channel's ID.
  2. Add "cogs.usage_logger" to COGS in main.py.

DB file: usage_log.db (created next to this file automatically).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands

log = logging.getLogger("usage_logger")

# ── Config ────────────────────────────────────────────────────────────────────
LOG_CHANNEL_ID: int = 1510518266442027148   # ← Replace with your log channel ID
BATCH_SIZE:     int = 5
DB_PATH = Path(__file__).parent.parent / "usage_log.db"

# User IDs to skip — commands from these users are never logged.
# Add as plain integers: 123456789, 987654321, ...
BLACKLISTED_USER_IDS: frozenset[int] = frozenset({
    1131217949672353832, 1271493110781837335
})
# ─────────────────────────────────────────────────────────────────────────────


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%-d %b %Y %H:%M UTC")


def _init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            type         TEXT NOT NULL,
            ts           TEXT NOT NULL,
            user         TEXT NOT NULL,
            user_id      INTEGER NOT NULL,
            command_name TEXT NOT NULL,
            invocation   TEXT,
            guild        TEXT NOT NULL,
            guild_id     TEXT NOT NULL,
            channel      TEXT NOT NULL,
            channel_id   TEXT NOT NULL
        )
    """)
    con.commit()
    return con


class UsageLogger(commands.Cog):
    """Batched command usage logger backed by SQLite."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._db  = _init_db()

    def cog_unload(self):
        self._db.close()

    # ── Write one row ─────────────────────────────────────────────────────────

    def _insert(self, entry: dict):
        self._db.execute(
            """INSERT INTO usage_log
               (type, ts, user, user_id, command_name, invocation,
                guild, guild_id, channel, channel_id)
               VALUES (:type, :ts, :user, :user_id, :command_name, :invocation,
                       :guild, :guild_id, :channel, :channel_id)""",
            entry,
        )
        self._db.commit()

    def _count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0]

    def _pop_batch(self) -> list[sqlite3.Row]:
        """Fetch the oldest BATCH_SIZE rows and delete them atomically."""
        rows = self._db.execute(
            "SELECT * FROM usage_log ORDER BY id ASC LIMIT ?", (BATCH_SIZE,)
        ).fetchall()
        if rows:
            ids = [r[0] for r in rows]
            self._db.execute(
                f"DELETE FROM usage_log WHERE id IN ({','.join('?'*len(ids))})", ids
            )
            self._db.commit()
        return rows

    # ── Prefix command listener ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        if ctx.author.bot:
            return
        if ctx.author.id in BLACKLISTED_USER_IDS:
            return

        self._insert({
            "type":         "prefix",
            "ts":           _now_str(),
            "user":         str(ctx.author),
            "user_id":      ctx.author.id,
            "command_name": ctx.command.qualified_name if ctx.command else "?",
            "invocation":   ctx.message.content[:120],
            "guild":        ctx.guild.name if ctx.guild else "DM",
            "guild_id":     str(ctx.guild.id) if ctx.guild else "—",
            "channel":      ctx.channel.name if hasattr(ctx.channel, "name") else "DM",
            "channel_id":   str(ctx.channel.id),
        })

        if self._count() >= BATCH_SIZE:
            await self._flush()

    # ── Slash command listener ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.application_command:
            return
        if interaction.user.bot:
            return
        if interaction.user.id in BLACKLISTED_USER_IDS:
            return

        channel      = interaction.channel
        channel_name = channel.name if channel and hasattr(channel, "name") else "DM"
        channel_id   = str(channel.id) if channel else "—"
        cmd_name     = interaction.data.get("name", "?") if interaction.data else "?"

        self._insert({
            "type":         "slash",
            "ts":           _now_str(),
            "user":         str(interaction.user),
            "user_id":      interaction.user.id,
            "command_name": cmd_name,
            "invocation":   None,   # not available for slash commands
            "guild":        interaction.guild.name if interaction.guild else "DM",
            "guild_id":     str(interaction.guild.id) if interaction.guild else "—",
            "channel":      channel_name,
            "channel_id":   channel_id,
        })

        if self._count() >= BATCH_SIZE:
            await self._flush()

    # ── Flush ─────────────────────────────────────────────────────────────────

    async def _flush(self):
        rows = self._pop_batch()
        if not rows:
            return

        channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(LOG_CHANNEL_ID)
            except Exception as e:
                log.error(f"Usage logger: cannot reach channel {LOG_CHANNEL_ID}: {e}")
                return

        col = {desc[0]: i for i, desc in enumerate(self._db.execute("SELECT * FROM usage_log LIMIT 0").description or [])}
        # Fallback column index map using known schema order:
        # id, type, ts, user, user_id, command_name, invocation, guild, guild_id, channel, channel_id
        IDX = dict(id=0, type=1, ts=2, user=3, user_id=4,
                   command_name=5, invocation=6, guild=7,
                   guild_id=8, channel=9, channel_id=10)

        embed = discord.Embed(
            title=f"📊 Command Usage — last {len(rows)} use(s)",
            colour=discord.Colour(0x7b2fff),
            timestamp=datetime.now(timezone.utc),
        )

        lines = []
        for i, row in enumerate(rows, 1):
            type_icon  = "⚡" if row[IDX["type"]] == "slash" else "💬"
            invocation = row[IDX["invocation"]]
            inv_line   = (
                f"  📝 `{discord.utils.escape_markdown(invocation)}`\n"
                if invocation
                else "  📝 _slash command — arguments not trackable_\n"
            )
            line = (
                f"**{i}.** {type_icon} `{row[IDX['command_name']]}`\n"
                f"  👤 {discord.utils.escape_markdown(row[IDX['user']])} (`{row[IDX['user_id']]}`)\n"
                f"  🏠 {discord.utils.escape_markdown(row[IDX['guild']])} (`{row[IDX['guild_id']]}`)\n"
                f"  💬 #{discord.utils.escape_markdown(row[IDX['channel']])} (`{row[IDX['channel_id']]}`)\n"
                f"{inv_line}"
                f"  🕐 {row[IDX['ts']]}"
            )
            lines.append(line)

        chunk: list[str] = []
        chunk_len = 0
        field_idx = 1

        for line in lines:
            if chunk_len + len(line) + 1 > 1000:
                embed.add_field(
                    name=f"Entries (part {field_idx})",
                    value="\n\n".join(chunk),
                    inline=False,
                )
                chunk = []
                chunk_len = 0
                field_idx += 1
            chunk.append(line)
            chunk_len += len(line) + 1

        if chunk:
            name = "Entries" if field_idx == 1 else f"Entries (part {field_idx})"
            embed.add_field(name=name, value="\n\n".join(chunk), inline=False)

        embed.set_footer(text=f"Batch of {len(rows)}  •  every {BATCH_SIZE} uses  •  ⚡ slash  💬 prefix")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.error(f"Usage logger: missing Send Messages / Embed Links in channel {LOG_CHANNEL_ID}")
        except discord.HTTPException as e:
            log.error(f"Usage logger: failed to send report: {e}")


async def setup(bot: commands.Bot):
    if not LOG_CHANNEL_ID:
        log.warning(
            "usage_logger: LOG_CHANNEL_ID is not set (still 0). "
            "Set it in cogs/usage_logger.py before loading this cog."
        )
    await bot.add_cog(UsageLogger(bot))
