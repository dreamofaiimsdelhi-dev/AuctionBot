"""
cogs/helpdesk.py – User-facing feedback commands + owner memory inspector.

Commands (all slash + prefix hybrid):
  /bugreport <description> [image_url]  — send a bug report to BUG_CHANNEL_ID
  /suggest   <description> [image_url]  — send a suggestion to SUGGEST_CHANNEL_ID

Owner-only:
  j!meminfo  — live RAM usage, active View objects, _RegenState record counts

Setup:
  Set BUG_CHANNEL_ID and SUGGEST_CHANNEL_ID below to your target channel IDs.
  Add "cogs.helpdesk" to COGS in main.py.
"""
from __future__ import annotations

import gc
import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger(__name__)

# ── Channel IDs ───────────────────────────────────────────────────────────────
BUG_CHANNEL_ID:     int = 1503310019368587354
SUGGEST_CHANNEL_ID: int = 1503309996996169778
# ─────────────────────────────────────────────────────────────────────────────

SAFE_MENTIONS = discord.AllowedMentions.none()

_URL_PREFIXES = ("https://", "http://")

# Common image-hosting domains — used for a light sanity-check on image_url.
# We deliberately keep this permissive; Discord will show a preview anyway.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif")


def _is_plausible_image_url(url: str) -> bool:
    """Return True if the string looks like it could be an image URL."""
    if not any(url.startswith(p) for p in _URL_PREFIXES):
        return False
    low = url.lower().split("?")[0]   # strip query params before checking ext
    return any(low.endswith(ext) for ext in _IMAGE_EXTS) or "cdn.discordapp" in low or "media.discordapp" in low or "i.imgur" in low


REPLY_EMOJI = "<:reply:1503236369126916117>"

def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%-d %b %Y %H:%M UTC")


# ─────────────────────────────────────────────────────────────────────────────
# REPORT VIEW BUILDER  (Components V2 — sent to the log channel)
# ─────────────────────────────────────────────────────────────────────────────

def _build_report_view(
    kind: str,
    description: str,
    image_url: str | None,
    author: discord.User | discord.Member,
    guild: discord.Guild | None,
    channel: discord.abc.GuildChannel | discord.DMChannel | None,
) -> discord.ui.LayoutView:
    if kind == "bug":
        accent      = discord.Colour(0xFF4C4C)
        header_text = "## 🐛 Bug Report"
    else:
        accent      = discord.Colour(0x7B2FFF)
        header_text = "## 💡 Suggestion"

    guild_val   = f"{discord.utils.escape_markdown(guild.name)} (`{guild.id}`)" if guild else "DM"
    channel_val = f"<#{channel.id}>" if channel and hasattr(channel, "id") else "—"
    ts          = _now_ts()

    # Description wrapped in a code block so it renders as-is, no markdown parsing
    body_text = (
        f"{header_text}\n\n"
        f"{REPLY_EMOJI} **From:** {author.mention} — `{author}` (`{author.id}`)\n"
        f"{REPLY_EMOJI} **Server:** {guild_val}\n"
        f"{REPLY_EMOJI} **Channel:** {channel_val}\n"
        f"{REPLY_EMOJI} **Submitted:** {ts}"
    )

    description_text = f"```\n{description}\n```"

    components: list = [
        discord.ui.TextDisplay(content=body_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=description_text),
    ]

    if image_url:
        components += [
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=image_url),
            ),
        ]

    class ReportView(discord.ui.LayoutView):
        container = discord.ui.Container(
            *components,
            accent_colour=accent,
        )

    return ReportView()


# ─────────────────────────────────────────────────────────────────────────────
# CONFIRMATION VIEW  (ephemeral, sent back to the submitting user)
# ─────────────────────────────────────────────────────────────────────────────

def _confirm_view(kind: str) -> discord.ui.LayoutView:
    noun = "bug report" if kind == "bug" else "suggestion"
    icon = "🐛" if kind == "bug" else "💡"
    text = (
        f"{icon} **Thank you! Your {noun} has been submitted.**\n"
        f"-# {REPLY_EMOJI} Our team will review it soon. You don't need to do anything else."
    )

    class ConfirmView(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=discord.Colour(0x06D6A0),
        )

    return ConfirmView()


def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )

    return EV()


# ─────────────────────────────────────────────────────────────────────────────
# SEND HELPER
# ─────────────────────────────────────────────────────────────────────────────

async def _send_report(
    bot: commands.Bot,
    channel_id: int,
    kind: str,
    description: str,
    image_url: str | None,
    author: discord.User | discord.Member,
    guild: discord.Guild | None,
    channel: discord.abc.GuildChannel | discord.DMChannel | None,
) -> tuple[bool, str]:
    """
    Post the Components V2 report view to the target channel.
    Returns (success: bool, error_message: str).
    """
    if not channel_id:
        noun = "BUG_CHANNEL_ID" if kind == "bug" else "SUGGEST_CHANNEL_ID"
        return False, f"❌ {noun} is not configured in `cogs/helpdesk.py`."

    target = bot.get_channel(channel_id)
    if target is None:
        try:
            target = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            return False, "❌ Report channel not found. Please contact an admin."
        except discord.Forbidden:
            return False, "❌ Bot doesn't have access to the report channel."
        except discord.HTTPException as e:
            return False, f"❌ Failed to fetch report channel: `{e}`"

    view = _build_report_view(kind, description, image_url, author, guild, channel)

    try:
        await target.send(view=view, allowed_mentions=SAFE_MENTIONS)
        return True, ""
    except discord.Forbidden:
        return False, "❌ Bot is missing Send Messages in the report channel."
    except discord.HTTPException as e:
        return False, f"❌ Failed to send report: `{e}`"


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class HelpDesk(commands.Cog, name="HelpDesk"):
    """User feedback and owner diagnostics."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /bugreport ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="bugreport", aliases=["bug", "reportbug"])
    @app_commands.describe(
        description="Describe the bug — what happened and what you expected to happen.",
        image_url="Optional direct link to a screenshot (must start with https://).",
    )
    async def bugreport_cmd(
        self,
        ctx: commands.Context,
        *,
        description: str = "",
        image_url: str | None = None,
    ):
        """Submit a bug report to the development team."""
        # ── Parse prefix invocations:  j!bugreport some text https://i.imgur.com/x.png
        # The last token is treated as an image URL if it looks like one.
        if not ctx.interaction and description:
            tokens = description.split()
            if tokens and _is_plausible_image_url(tokens[-1]):
                image_url   = tokens[-1]
                description = " ".join(tokens[:-1]).strip()

        if not description:
            await ctx.send(
                view=_error_view(
                    "❌ Please describe the bug.\n"
                    "-# Usage: `/bugreport <description> [image_url]`"
                ),
                ephemeral=True,
            )
            return

        if image_url and not _is_plausible_image_url(image_url):
            await ctx.send(
                view=_error_view(
                    "❌ The image URL doesn't look valid.\n"
                    "-# It must start with `https://` and point to an image file or Discord CDN link."
                ),
                ephemeral=True,
            )
            return

        ok, err = await _send_report(
            self.bot, BUG_CHANNEL_ID, "bug",
            description, image_url,
            ctx.author, ctx.guild, ctx.channel,
        )

        if ok:
            await ctx.send(view=_confirm_view("bug"), ephemeral=True)
        else:
            log.error("bugreport delivery failed: %s", err)
            await ctx.send(view=_error_view(err), ephemeral=True)

    # ── /suggest ──────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="suggest", aliases=["suggestion", "idea"])
    @app_commands.describe(
        description="Your idea or feature request.",
        image_url="Optional direct link to a mockup or screenshot (must start with https://).",
    )
    async def suggest_cmd(
        self,
        ctx: commands.Context,
        *,
        description: str = "",
        image_url: str | None = None,
    ):
        """Submit a suggestion or feature request to the development team."""
        if not ctx.interaction and description:
            tokens = description.split()
            if tokens and _is_plausible_image_url(tokens[-1]):
                image_url   = tokens[-1]
                description = " ".join(tokens[:-1]).strip()

        if not description:
            await ctx.send(
                view=_error_view(
                    "❌ Please describe your suggestion.\n"
                    "-# Usage: `/suggest <description> [image_url]`"
                ),
                ephemeral=True,
            )
            return

        if image_url and not _is_plausible_image_url(image_url):
            await ctx.send(
                view=_error_view(
                    "❌ The image URL doesn't look valid.\n"
                    "-# It must start with `https://` and point to an image file or Discord CDN link."
                ),
                ephemeral=True,
            )
            return

        ok, err = await _send_report(
            self.bot, SUGGEST_CHANNEL_ID, "suggest",
            description, image_url,
            ctx.author, ctx.guild, ctx.channel,
        )

        if ok:
            await ctx.send(view=_confirm_view("suggest"), ephemeral=True)
        else:
            log.error("suggest delivery failed: %s", err)
            await ctx.send(view=_error_view(err), ephemeral=True)

    # ── j!meminfo (owner only) ────────────────────────────────────────────────

    @commands.command(name="meminfo", aliases=["mem", "memory"])
    @commands.is_owner()
    async def meminfo_cmd(self, ctx: commands.Context):
        """
        Show live RAM usage, active discord.py View objects, and graph record counts.
        Owner-only. Prefix command only (not exposed as a slash command).
        """
        try:
            import psutil
            proc    = psutil.Process(os.getpid())
            rss_mb  = proc.memory_info().rss / 1024 / 1024
            vms_mb  = proc.memory_info().vms / 1024 / 1024
            rss_str = f"`{rss_mb:.1f} MB` RSS  •  `{vms_mb:.1f} MB` VMS"
        except ImportError:
            rss_str = "_psutil not installed — install it for RSS stats_"

        # ── Count live discord.py View objects ────────────────────────────────
        all_objs  = gc.get_objects()
        views     = [o for o in all_objs if isinstance(o, discord.ui.View)]
        view_counts: dict[str, int] = {}
        for v in views:
            name = type(v).__name__
            view_counts[name] = view_counts.get(name, 0) + 1

        view_lines = "\n".join(
            f"　• `{k}`: {n}"
            for k, n in sorted(view_counts.items(), key=lambda x: -x[1])
        ) or "　_(none)_"

        # ── _RegenState objects from graph.py ─────────────────────────────────
        regen_lines = ""
        try:
            from cogs.graph import _RegenState
            regen_states  = [o for o in all_objs if isinstance(o, _RegenState)]
            total_records = sum(len(s.slim_records) for s in regen_states)
            # slim tuples are ~80 bytes each (8 small Python values)
            est_mb        = total_records * 80 / 1024 / 1024
            regen_lines   = (
                f"\n**_RegenState objects (graph toggle state):**\n"
                f"　• Live states: `{len(regen_states)}`\n"
                f"　• Total records held: `{total_records:,}`\n"
                f"　• Estimated RAM from records: `~{est_mb:.1f} MB`"
            )
        except Exception as e:
            regen_lines = f"\n**_RegenState:** _could not inspect — {e}_"

        # ── 5 largest lists in memory ─────────────────────────────────────────
        big_lists = sorted(
            (o for o in all_objs if isinstance(o, list) and len(o) > 200),
            key=len, reverse=True,
        )[:5]
        big_list_lines = "\n".join(
            f"　• len=`{len(l):,}`  item type=`{type(l[0]).__name__ if l else '?'}`"
            for l in big_lists
        ) or "　_(none above threshold)_"

        # ── matplotlib figure count ───────────────────────────────────────────
        try:
            import matplotlib.pyplot as plt
            fig_count  = len(plt.get_fignums())
            fig_line   = f"\n**Open matplotlib figures:** `{fig_count}`"
        except ImportError:
            fig_line   = ""

        # ── GC generation counts ──────────────────────────────────────────────
        gc_counts = gc.get_count()
        gc_line   = f"gen0=`{gc_counts[0]}`  gen1=`{gc_counts[1]}`  gen2=`{gc_counts[2]}`"

        text = (
            f"## 🔧 Memory Inspector\n"
            f"**RAM usage:** {rss_str}\n\n"
            f"**Live View objects:** `{len(views)}`\n{view_lines}\n"
            f"{regen_lines}\n\n"
            f"**5 largest lists in RAM:**\n{big_list_lines}\n"
            f"{fig_line}\n\n"
            f"**GC object counts:** {gc_line}\n"
            f"-# Timestamp: {_now_ts()}"
        )

        class MemView(discord.ui.LayoutView):
            c = discord.ui.Container(
                discord.ui.TextDisplay(content=text),
                accent_colour=discord.Colour(0x7B2FFF),
            )

        await ctx.send(view=MemView(), ephemeral=False)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    if not BUG_CHANNEL_ID:
        log.warning("helpdesk: BUG_CHANNEL_ID is not set. Set it in cogs/helpdesk.py.")
    if not SUGGEST_CHANNEL_ID:
        log.warning("helpdesk: SUGGEST_CHANNEL_ID is not set. Set it in cogs/helpdesk.py.")
    await bot.add_cog(HelpDesk(bot))
