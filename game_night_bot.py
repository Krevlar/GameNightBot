"""
Monday Game Night Bot
---------------------
- Friday evening: posts a message asking for game suggestions.
- Anyone suggests games with /suggest (Steam autocomplete).
- Sunday 7:00 PM: posts a native Discord poll of the suggestions, followed by a
  message with each game's description, genre(s), and player info from Steam.

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
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

# ----------------------------- Configuration ------------------------------

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["GAME_CHANNEL_ID"])

TZ = ZoneInfo("America/Chicago")      # change to your timezone
FRIDAY_PROMPT_TIME = dtime(17, 0, tzinfo=TZ)   # Friday 5:00 PM
SUNDAY_POLL_TIME = dtime(19, 0, tzinfo=TZ)     # Sunday 7:00 PM
POLL_DURATION = timedelta(hours=24)

DATA_FILE = Path(os.environ.get("GAME_DATA_FILE", "suggestions.json"))
MAX_POLL_ANSWERS = 10     # Discord limit
MAX_ANSWER_LEN = 55       # Discord limit for poll answer text

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


def player_info(details: dict) -> str:
    cats = details.get("categories", [])
    labels = [PLAYER_CATEGORY_IDS[c["id"]] for c in cats if c["id"] in PLAYER_CATEGORY_IDS]
    return ", ".join(labels) if labels else "Unknown"


# ------------------------------- Bot --------------------------------------


class GameNightBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        self.friday_prompt.start()
        self.sunday_poll.start()

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
            "Use `/suggest` to add a game (start typing and pick from the Steam results). "
            "Optionally add the player count, e.g. `players: 2-4`.\n"
            "The poll goes up **Sunday at 7:00 PM**."
        )

    # ---- Sunday: post poll + details ----
    @tasks.loop(time=SUNDAY_POLL_TIME)
    async def sunday_poll(self):
        if datetime.now(TZ).weekday() != 6:  # Sunday
            return
        channel = await self.get_channel_safe()
        suggestions = load_suggestions()

        if len(suggestions) < 2:
            await channel.send(
                "😕 Not enough suggestions this week to make a poll "
                f"({len(suggestions)} submitted). Try again next Friday!"
            )
            save_suggestions([])
            return

        games = suggestions[:MAX_POLL_ANSWERS]
        overflow = len(suggestions) - len(games)

        poll = discord.Poll(question="What are we playing Monday night?", duration=POLL_DURATION)
        for g in games:
            poll.add_answer(text=g["name"][:MAX_ANSWER_LEN])
        await channel.send(poll=poll)

        # Fetch Steam details in parallel
        details = await asyncio.gather(
            *(steam_details(self.session, g["appid"]) if g.get("appid") else asyncio.sleep(0, result=None)
              for g in games)
        )

        blocks = []
        for g, d in zip(games, details):
            if d:
                desc = clean(d.get("short_description", "")) or "No description available."
                if len(desc) > 300:
                    desc = desc[:297].rstrip() + "..."
                genres = ", ".join(x["description"] for x in d.get("genres", [])) or "Unknown"
                players = g.get("players") or player_info(d)
                blocks.append(
                    f"**{g['name']}**\n"
                    f"*Genre:* {genres}  |  *Players:* {players}\n"
                    f"{desc}"
                )
            else:
                players = g.get("players") or "Unknown"
                blocks.append(
                    f"**{g['name']}**\n*Players:* {players}\n(No Steam info found.)"
                )

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

    @friday_prompt.before_loop
    @sunday_poll.before_loop
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


@bot.tree.command(name="suggest", description="Suggest a game for Monday night")
@app_commands.describe(game="Start typing a game name", players="Optional player count, e.g. 2-4")
@app_commands.autocomplete(game=game_autocomplete)
async def suggest(interaction: discord.Interaction, game: str, players: str | None = None):
    await interaction.response.defer()
    appid, name = None, game

    if game.isdigit():  # picked from autocomplete
        details = await steam_details(bot.session, int(game))
        if details:
            appid, name = int(game), details["name"]
    else:               # free text: take the top Steam result if any
        results = await steam_search(bot.session, game)
        if results:
            appid, name = results[0]["id"], results[0]["name"]

    items = load_suggestions()
    if any((appid and g.get("appid") == appid) or g["name"].lower() == name.lower() for g in items):
        await interaction.followup.send(f"**{name}** is already on the list.", ephemeral=True)
        return

    items.append({
        "appid": appid,
        "name": name,
        "players": players,
        "suggested_by": interaction.user.display_name,
    })
    save_suggestions(items)

    note = "" if appid else " (not found on Steam, so no description will be shown)"
    await interaction.followup.send(f"✅ **{name}** added by {interaction.user.display_name}{note}.")


@bot.tree.command(name="suggestions", description="Show current suggestions")
async def suggestions(interaction: discord.Interaction):
    items = load_suggestions()
    if not items:
        await interaction.response.send_message("No suggestions yet. Use `/suggest`!", ephemeral=True)
        return
    lines = [f"{i}. {g['name']} — {g['suggested_by']}" for i, g in enumerate(items, 1)]
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