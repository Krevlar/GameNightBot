"""
Monday Game Night Bot
---------------------
- Friday 5:00 PM: posts a message asking for game suggestions.
- Anyone suggests games with /suggest (Steam autocomplete) or /suggest_nonsteam
  (any game, by name), each with an optional short pitch. The list is capped at
  10 games, the most a Discord poll holds.
- Sunday 6:00 PM: reminder that suggestions close in an hour.
- Sunday 7:00 PM: posts a native Discord poll of the suggestions, followed by a
  message with each game's description, genre(s), and player info from Steam.
- Monday 6:00 PM: reminder to vote, one hour before the poll closes.

Setup:
    pip install -U discord.py aiohttp
    export DISCORD_TOKEN="your-bot-token"
    export GAME_CHANNEL_ID="123456789012345678"
    export GAME_DATA_FILE="/var/lib/gamenightbot/suggestions.json"  # optional
    python game_night_bot.py

Bot needs the "Send Messages" and "Create Polls" permissions in the channel,
and the "applications.commands" scope when you invite it.
"""

import asyncio
import html
import json
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

# ----------------------------- Configuration ------------------------------

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["GAME_CHANNEL_ID"])

TZ = ZoneInfo("America/Chicago")                    # change to your timezone
FRIDAY_PROMPT_TIME = dtime(17, 0, tzinfo=TZ)        # Friday 5:00 PM
SUGGESTION_REMINDER_TIME = dtime(18, 0, tzinfo=TZ)  # Sunday 6:00 PM (1 hour before the poll)
SUNDAY_POLL_TIME = dtime(19, 0, tzinfo=TZ)          # Sunday 7:00 PM
VOTE_REMINDER_TIME = dtime(18, 0, tzinfo=TZ)        # Monday 6:00 PM (1 hour before the poll closes)
POLL_DURATION = timedelta(hours=24)  # poll closes Monday 7:00 PM; keep VOTE_REMINDER_TIME 1 hour earlier

DATA_FILE = Path(os.environ.get("GAME_DATA_FILE", "suggestions.json"))
STATE_FILE = DATA_FILE.with_name("poll_state.json")  # remembers the weekly poll for the vote reminder
MAX_POLL_ANSWERS = 10     # Discord limit
MAX_ANSWER_LEN = 55       # Discord limit for poll answer text
MAX_PITCH_LEN = 200       # our own limit for the pitch

FULL_MESSAGE = (
    f"🚫 The list is full ({MAX_POLL_ANSWERS}/{MAX_POLL_ANSWERS}). Discord polls hold at most "
    f"{MAX_POLL_ANSWERS} games, so use `/suggestions` to see what's in and `/unsuggest` to make room."
)

STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
APP_DETAILS = "https://store.steampowered.com/api/appdetails"

# Steam category IDs that say something about player count / mode
PLAYER_CATEGORY_IDS = {
    2: "Single-player",
    1: "Multi-player",
    49: "PvP",
    36: "Online PvP",
    9: "Co-op",
    38: "Online Co-op",
    39: "Split-screen Co-op",
    20: "MMO",
}

# ------------------------------ Storage -----------------------------------


def load_suggestions() -> list[dict]:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return []


def save_suggestions(items: list[dict]) -> None:
    DATA_FILE.write_text(json.dumps(items, indent=2))


def load_poll_state() -> dict | None:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except ValueError:
            return None
    return None


def save_poll_state(state: dict | None) -> None:
    if state is None:
        STATE_FILE.unlink(missing_ok=True)
    else:
        STATE_FILE.write_text(json.dumps(state))


# ------------------------------ Steam API ---------------------------------


async def steam_search(session: aiohttp.ClientSession, term: str) -> list[dict]:
    params = {"term": term, "l": "english", "cc": "us"}
    try:
        async with session.get(STORE_SEARCH, params=params, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
        return data.get("items", [])
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []


async def steam_details(session: aiohttp.ClientSession, appid: int) -> dict | None:
    params = {"appids": appid, "l": "english", "cc": "us"}
    try:
        async with session.get(APP_DETAILS, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        entry = data.get(str(appid), {})
        return entry.get("data") if entry.get("success") else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def tidy(text: str | None) -> str | None:
    """Collapse newlines and repeated spaces; empty text becomes None."""
    text = " ".join(text.split()) if text else ""
    return text or None


def player_info(details: dict) -> str:
    cats = details.get("categories", [])
    labels = [PLAYER_CATEGORY_IDS[c["id"]] for c in cats if c["id"] in PLAYER_CATEGORY_IDS]
    return ", ".join(labels) if labels else "Unknown"


# ------------------------------- Bot --------------------------------------


class GameNightBot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),  # pitches are user text; never ping
        )
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        self.friday_prompt.start()
        self.suggestion_reminder.start()
        self.sunday_poll.start()
        self.vote_reminder.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def get_channel_safe(self) -> discord.abc.Messageable | None:
        return self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)

    # ---- Friday: ask for suggestions ----
    @tasks.loop(time=FRIDAY_PROMPT_TIME)
    async def friday_prompt(self):
        if datetime.now(TZ).weekday() != 4:  # Friday
            return
        channel = await self.get_channel_safe()
        await channel.send(
            "🎮 **Monday game night suggestions are open!**\n"
            "Use `/suggest` to add a game (start typing and pick from the Steam results), "
            "or `/suggest_nonsteam` for anything that isn't on Steam. "
            "Optionally add a short `pitch` on why it's a good pick.\n"
            f"The list holds up to {MAX_POLL_ANSWERS} games, and the poll goes up **Sunday at 7:00 PM**."
        )

    # ---- Sunday, 1 hour before the poll: last call for suggestions ----
    @tasks.loop(time=SUGGESTION_REMINDER_TIME)
    async def suggestion_reminder(self):
        if datetime.now(TZ).weekday() != 6:  # Sunday
            return
        channel = await self.get_channel_safe()
        count = len(load_suggestions())

        closes = datetime.now(TZ).replace(
            hour=SUNDAY_POLL_TIME.hour, minute=SUNDAY_POLL_TIME.minute, second=0, microsecond=0
        )
        ts = int(closes.timestamp())

        if count >= MAX_POLL_ANSWERS:
            status = f"The list is full ({count}/{MAX_POLL_ANSWERS})."
        else:
            status = f"{count}/{MAX_POLL_ANSWERS} games so far."
            if count < 2:
                status += " We need at least 2 to make a poll!"
        text = f"⏰ **Suggestions close <t:{ts}:R>!** The poll goes up at <t:{ts}:t>.\n{status}"
        if count < MAX_POLL_ANSWERS:
            text += "\nAdd a game with `/suggest` (Steam) or `/suggest_nonsteam` (anything else)."
        await channel.send(text)

    # ---- Sunday: post poll + details ----
    @tasks.loop(time=SUNDAY_POLL_TIME)
    async def sunday_poll(self):
        if datetime.now(TZ).weekday() != 6:  # Sunday
            return
        channel = await self.get_channel_safe()
        suggestions = load_suggestions()
        save_poll_state(None)  # forget last week's poll

        if len(suggestions) < 2:
            await channel.send(
                "😕 Not enough suggestions this week to make a poll "
                f"({len(suggestions)} submitted). Try again next Friday!"
            )
            save_suggestions([])
            return

        games = suggestions[:MAX_POLL_ANSWERS]
        overflow = len(suggestions) - len(games)

        poll = discord.Poll(
            question="What are we playing Monday night?",
            duration=POLL_DURATION,
            multiple=True,
        )
        for g in games:
            poll.add_answer(text=g["name"][:MAX_ANSWER_LEN])
        poll_message = await channel.send(poll=poll)
        save_poll_state({"message_id": poll_message.id})  # for the Monday vote reminder

        # Fetch Steam details in parallel
        details = await asyncio.gather(
            *(steam_details(self.session, g["appid"]) if g.get("appid") else asyncio.sleep(0, result=None)
              for g in games)
        )

        blocks = []
        for g, d in zip(games, details):
            pitch = f"\n💬 *{g['suggested_by']}:* {g['pitch']}" if g.get("pitch") else ""
            if d:
                desc = clean(d.get("short_description", "")) or "No description available."
                if len(desc) > 300:
                    desc = desc[:297].rstrip() + "..."
                genres = ", ".join(x["description"] for x in d.get("genres", [])) or "Unknown"
                players = player_info(d)
                blocks.append(
                    f"**{g['name']}**\n"
                    f"*Genre:* {genres}  |  *Players:* {players}\n"
                    f"{desc}{pitch}"
                )
            else:
                blocks.append(f"**{g['name']}**\n(No Steam info available.){pitch}")

        if overflow:
            blocks.append(f"*{overflow} extra suggestion(s) didn't fit (Discord polls max out at 10).*")

        # Split into <2000-char messages
        chunk = ""
        for block in blocks:
            if len(chunk) + len(block) + 2 > 1900:
                await channel.send(chunk)
                chunk = ""
            chunk += block + "\n\n"
        if chunk.strip():
            await channel.send(chunk)

        save_suggestions([])  # reset for next week

    # ---- Monday, 1 hour before the poll closes: last call to vote ----
    @tasks.loop(time=VOTE_REMINDER_TIME)
    async def vote_reminder(self):
        if datetime.now(TZ).weekday() != 0:  # Monday
            return
        state = load_poll_state()
        if not state:
            return  # no poll this week
        channel = await self.get_channel_safe()
        try:
            message = await channel.fetch_message(state["message_id"])
        except (discord.NotFound, discord.HTTPException):
            return  # poll message was deleted
        poll = message.poll
        if poll is None or poll.expires_at is None or poll.expires_at <= datetime.now(timezone.utc):
            return  # already closed
        ts = int(poll.expires_at.timestamp())
        await channel.send(
            f"🗳️ **Voting closes <t:{ts}:R>!** (at <t:{ts}:t>)\n"
            f"Pick every game you'd be happy to play: {message.jump_url}"
        )

    @friday_prompt.before_loop
    @suggestion_reminder.before_loop
    @sunday_poll.before_loop
    @vote_reminder.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()


bot = GameNightBot()

# ---------------------------- Slash commands ------------------------------


async def game_autocomplete(interaction: discord.Interaction, current: str):
    if len(current) < 2:
        return []
    items = await steam_search(bot.session, current)
    return [
        app_commands.Choice(name=i["name"][:100], value=str(i["id"]))
        for i in items[:10]
    ]


async def reject_early(interaction: discord.Interaction, pitch: str | None) -> bool:
    """Private checks that run before any slow work. True means we replied and the caller should stop."""
    if len(load_suggestions()) >= MAX_POLL_ANSWERS:
        await interaction.response.send_message(FULL_MESSAGE, ephemeral=True)
        return True
    if pitch and len(pitch) > MAX_PITCH_LEN:
        await interaction.response.send_message(
            f"Your pitch is {len(pitch)} characters; the max is {MAX_PITCH_LEN}. Try trimming it.",
            ephemeral=True,
        )
        return True
    return False


async def add_suggestion(interaction: discord.Interaction, appid: int | None, name: str,
                         pitch: str | None, note: str = ""):
    """Save a suggestion and confirm it. Call after interaction.response.defer()."""
    items = load_suggestions()  # reload: the list may have changed while we waited on Steam
    if len(items) >= MAX_POLL_ANSWERS:
        await interaction.followup.send(FULL_MESSAGE)
        return
    if any((appid and g.get("appid") == appid) or g["name"].lower() == name.lower() for g in items):
        await interaction.followup.send(f"**{name}** is already on the list.")
        return

    items.append({
        "appid": appid,
        "name": name,
        "pitch": pitch,
        "suggested_by": interaction.user.display_name,
    })
    save_suggestions(items)

    msg = f"✅ **{name}** added by {interaction.user.display_name} ({len(items)}/{MAX_POLL_ANSWERS}){note}."
    if pitch:
        msg += f"\n> {pitch}"
    if len(items) == MAX_POLL_ANSWERS:
        msg += "\n⚠️ That was the last spot: the list is now full."
    await interaction.followup.send(msg)


@bot.tree.command(name="suggest", description="Suggest a Steam game for Monday night")
@app_commands.describe(
    game="Start typing a game name and pick it from the list",
    pitch="Optional: a few words on why it's a good pick",
)
@app_commands.autocomplete(game=game_autocomplete)
async def suggest(interaction: discord.Interaction, game: str, pitch: str | None = None):
    pitch = tidy(pitch)
    if await reject_early(interaction, pitch):
        return
    if game.isdigit() and any(g.get("appid") == int(game) for g in load_suggestions()):
        await interaction.response.send_message("That game is already on the list.", ephemeral=True)
        return

    await interaction.response.defer()

    if game.isdigit():  # picked from autocomplete
        details = await steam_details(bot.session, int(game))
        if not details:
            await interaction.followup.send(
                "Couldn't load that game from Steam right now. Try again in a minute, "
                "or use `/suggest_nonsteam`."
            )
            return
        appid, name = int(game), details["name"]
    else:               # typed without picking from the list: only accept an exact Steam match
        results = await steam_search(bot.session, game)
        match = next((r for r in results if r["name"].lower() == game.strip().lower()), None)
        if not match:
            hint = f" Did you mean **{results[0]['name']}**?" if results else ""
            await interaction.followup.send(
                f"I couldn't find an exact Steam match for **{game}**.{hint} "
                "Pick a game from the autocomplete list, or use `/suggest_nonsteam` if it isn't on Steam."
            )
            return
        appid, name = match["id"], match["name"]

    await add_suggestion(interaction, appid, name, pitch)


@bot.tree.command(name="suggest_nonsteam", description="Suggest a game that isn't on Steam")
@app_commands.describe(
    name="The game's name",
    pitch="Optional: a few words on why it's a good pick",
)
async def suggest_nonsteam(interaction: discord.Interaction, name: str, pitch: str | None = None):
    name = tidy(name) or ""
    pitch = tidy(pitch)
    if not 2 <= len(name) <= MAX_ANSWER_LEN:
        await interaction.response.send_message(
            f"Game names need to be 2 to {MAX_ANSWER_LEN} characters (that's Discord's poll limit).",
            ephemeral=True,
        )
        return
    if await reject_early(interaction, pitch):
        return
    if any(g["name"].lower() == name.lower() for g in load_suggestions()):
        await interaction.response.send_message("That game is already on the list.", ephemeral=True)
        return

    await interaction.response.defer()
    await add_suggestion(interaction, None, name, pitch, note=" (non-Steam, so no description will be shown)")


@bot.tree.command(name="suggestions", description="Show current suggestions")
async def suggestions(interaction: discord.Interaction):
    items = load_suggestions()
    if not items:
        await interaction.response.send_message("No suggestions yet. Use `/suggest`!", ephemeral=True)
        return
    lines = [f"**Suggestions ({len(items)}/{MAX_POLL_ANSWERS})**"]
    lines += [
        f"{i}. {g['name']}{'' if g.get('appid') else ' *(non-Steam)*'} — {g['suggested_by']}"
        for i, g in enumerate(items, 1)
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="unsuggest", description="Remove a suggestion by its number from /suggestions")
async def unsuggest(interaction: discord.Interaction, number: int):
    items = load_suggestions()
    if not 1 <= number <= len(items):
        await interaction.response.send_message("Invalid number.", ephemeral=True)
        return
    removed = items.pop(number - 1)
    save_suggestions(items)
    await interaction.response.send_message(f"Removed **{removed['name']}**.")


if __name__ == "__main__":
    bot.run(TOKEN)