"""
main.py – Entry point for the Pokémon Auction Bot.

Extras vs vanilla setup:
  • Tolerates spaces after the prefix:  "j!   auction s" works fine
  • Accepts a bot @mention as a prefix: "@Bot auction s" works too
  • Re-processes edited messages so commands work after an edit
  • Robust cog loading with per-cog error reporting
  • Cleaner on_command_error with full traceback logging
"""
import asyncio
import logging
import traceback

import discord
from discord.ext import commands

import config

# ─── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")

# ─── Cogs to load ─────────────────────────────────────────────────────────────
COGS = [
    "cogs.usage_logger",
    "cogs.auction",
    "cogs.graph",
    "cogs.price",
    "cogs.stats",
    "cogs.utils_cog",
    "cogs.trade_warn",
]


# ─────────────────────────────────────────────────────────────────────────────
# PREFIX RESOLVER
#
# Supports:
#   1. Any prefix from config.COMMAND_PREFIX (e.g. "j!", "J!")
#      with optional whitespace after it: "j!   auction s" → valid
#   2. Bot @mention as a prefix: "@Bot auction s" → valid
# ─────────────────────────────────────────────────────────────────────────────

def _make_prefix(bot: commands.Bot, message: discord.Message) -> list[str]:
    """
    Return all valid prefix strings for this message.
    Handles space-tolerant prefix variants and bot mention prefix.
    """
    prefixes: list[str] = []

    # ── Standard prefixes (from config) ───────────────────────────────────────
    # For each configured prefix, also accept it followed by 1–5 spaces.
    for p in config.COMMAND_PREFIX:
        prefixes.append(p)
        for n in range(1, 6):
            prefixes.append(p + " " * n)

    # ── Bot mention as prefix ─────────────────────────────────────────────────
    # Discord sends mentions in two forms: <@ID> and <@!ID>
    if bot.user:
        prefixes.append(f"<@{bot.user.id}>")
        prefixes.append(f"<@!{bot.user.id}>")
        # Also with a trailing space after the mention: "@Bot auction s"
        prefixes.append(f"<@{bot.user.id}> ")
        prefixes.append(f"<@!{bot.user.id}> ")

    return prefixes


# ─────────────────────────────────────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True


class CaseInsensitiveBot(commands.Bot):
    """
    Bot subclass that makes ALL command (and subcommand) lookups case-insensitive.
    j!LB, j!Lb, j!lb → all resolve to the same command.
    Achieved by normalizing the command name to lowercase before lookup.
    """

    def get_command(self, name: str) -> commands.Command | None:
        return super().get_command(name.lower())

    async def get_context(self, message: discord.Message, *, cls=commands.Context):
        ctx = await super().get_context(message, cls=cls)
        # Normalize invoked_with so subcommand dispatch also lowercases correctly.
        if ctx.command and ctx.invoked_with:
            ctx.invoked_with = ctx.invoked_with.lower()
        return ctx


bot = CaseInsensitiveBot(
    command_prefix=_make_prefix,
    intents=intents,
    help_command=None,
    # Strip leading whitespace from the content AFTER the prefix has been matched.
    # Combined with our space-padded prefix list, this makes "j!   cmd" work.
    strip_after_prefix=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # ── Print registered commands ─────────────────────────────────────────────
    log.info("── Prefix commands ──")
    for cmd in sorted(bot.commands, key=lambda c: c.name):
        log.info(f"  {cmd.name} (aliases: {cmd.aliases})")
        if hasattr(cmd, "commands"):
            for sub in cmd.commands:
                log.info(f"    └─ {sub.name} (aliases: {sub.aliases})")

    log.info("── App tree commands (before sync) ──")
    for cmd in bot.tree.get_commands():
        log.info(f"  /{cmd.name} ({type(cmd).__name__})")
        if hasattr(cmd, "commands"):
            for sub in cmd.commands:
                log.info(f"    └─ /{cmd.name} {sub.name}")

    # ── Sync slash commands ───────────────────────────────────────────────────
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s):")
        for c in synced:
            log.info(f"  /{c.name}")
    except discord.HTTPException as e:
        log.error(f"Slash command sync failed (HTTP {e.status}): {e.text}")
    except Exception:
        log.exception("Unexpected error during slash command sync")


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from bots (including ourselves)
    if message.author.bot:
        return
    await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Re-process edited messages so commands work after an edit."""
    if after.author.bot:
        return
    # Only re-process if the content actually changed
    if before.content == after.content:
        return
    await bot.process_commands(after)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Global error handler for prefix commands."""

    # Unwrap CheckFailure wrappers
    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        log.error(
            f"CommandInvokeError in '{ctx.command}' "
            f"(by {ctx.author} in #{getattr(ctx.channel, 'name', '?')}): {original}",
            exc_info=original,
        )
        await ctx.send(
            f"⚠️ An unexpected error occurred while running `{ctx.command}`.\n"
            f"```{type(original).__name__}: {original}```",
            reference=ctx.message,
            mention_author=False,
        )
        return

    if isinstance(error, commands.CommandNotFound):
        # Silently ignore unknown commands — keeps the bot clean
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            f"❌ Missing required argument: `{error.param.name}`\n"
            f"Use `j!a h` or `j!help` for usage info.",
            reference=ctx.message,
            mention_author=False,
        )
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(
            f"❌ Bad argument: {error}\n"
            f"Use `j!a h` for usage info.",
            reference=ctx.message,
            mention_author=False,
        )
        return

    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("❌ This command cannot be used in DMs.")
        return

    if isinstance(error, commands.NotOwner):
        await ctx.send("❌ Only the bot owner can use this command.")
        return

    if isinstance(error, commands.DisabledCommand):
        await ctx.send(f"❌ `{ctx.command}` is currently disabled.")
        return

    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(
            f"⏳ Slow down! Try again in `{error.retry_after:.1f}s`.",
            reference=ctx.message,
            mention_author=False,
        )
        return

    # Catch-all: log and notify
    log.error(
        f"Unhandled command error in '{ctx.command}' "
        f"(by {ctx.author} in #{getattr(ctx.channel, 'name', '?')}): {error}",
        exc_info=error,
    )
    await ctx.send(
        f"⚠️ Something went wrong: `{error}`",
        reference=ctx.message,
        mention_author=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# COG LOADER
# ─────────────────────────────────────────────────────────────────────────────

async def load_cogs():
    success = 0
    for cog in COGS:
        try:
            await bot.load_extension(cog)
            log.info(f"✅ Loaded cog: {cog}")
            success += 1
        except commands.ExtensionAlreadyLoaded:
            log.warning(f"⚠️  Cog already loaded: {cog}")
            success += 1
        except commands.ExtensionNotFound:
            log.error(f"❌ Cog not found: {cog}")
        except commands.NoEntryPointError:
            log.error(f"❌ Cog has no setup() function: {cog}")
        except commands.ExtensionFailed as e:
            log.error(f"❌ Cog failed to load: {cog}")
            log.error(f"   Caused by: {e.original}", exc_info=e.original)
        except Exception:
            log.exception(f"❌ Unexpected error loading cog: {cog}")

    log.info(f"Loaded {success}/{len(COGS)} cog(s)")
    if success == 0:
        log.critical("No cogs loaded — bot has no commands. Check the errors above.")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    if not config.DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN is not set. Set the environment variable and try again.")
        return

    async with bot:
        await load_cogs()
        try:
            await bot.start(config.DISCORD_TOKEN)
        except discord.LoginFailure:
            log.critical("Invalid Discord token — login failed.")
        except discord.PrivilegedIntentsRequired:
            log.critical(
                "Privileged intents are required but not enabled in the Discord Developer Portal. "
                "Enable 'Message Content Intent' for your bot."
            )
        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt).")
        except Exception:
            log.exception("Fatal error during bot.start()")


if __name__ == "__main__":
    asyncio.run(main())
