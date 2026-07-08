"""Discord bot: slash příkazy + notifikace o live games.

Spuštění: python -m lol.bot
Nastavení kanálu pro notifikace: napiš /setchannel v cílovém kanálu.
"""

import asyncio
import os
import pathlib

import discord
import httpx
from discord import app_commands
from discord.ext import tasks

from lol import db, stats
from lol.riot import RiotClient
from lol.tracker import live_game_event, load_config, resolve_players, sync_player

ROOT = pathlib.Path(__file__).parent.parent

QUEUES = {420: "Ranked Solo", 440: "Ranked Flex", 400: "Normal Draft",
          430: "Normal Blind", 450: "ARAM", 490: "Quickplay",
          700: "Clash", 900: "URF", 1700: "Arena", 1710: "Arena"}


async def load_champion_names() -> dict[int, str]:
    """championId -> jméno z Data Dragonu (statická CDN data, bez API klíče)."""
    async with httpx.AsyncClient(timeout=10) as http:
        version = (await http.get(
            "https://ddragon.leagueoflegends.com/api/versions.json")).json()[0]
        data = (await http.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
        )).json()["data"]
    return {int(c["key"]): c["name"] for c in data.values()}


class TrackerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.cfg = load_config()
        self.con = db.connect(str(ROOT / "lol.db"))
        self.riot = RiotClient()
        self.champions: dict[int, str] = {}

    async def setup_hook(self):
        self.champions = await load_champion_names()
        await resolve_players(self.con, self.riot, self.cfg)  # seed z config.toml
        register_commands(self)
        await self.tree.sync()
        self.poll_live.start()
        self.sync_matches.start()

    def db_players(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM players")]

    def notify_channel(self):
        row = self.con.execute(
            "SELECT value FROM settings WHERE key = 'notify_channel'").fetchone()
        return self.get_channel(int(row["value"])) if row else None

    def champ(self, champion_id: int) -> str:
        return self.champions.get(champion_id, f"champion {champion_id}")

    @tasks.loop(seconds=120)
    async def poll_live(self):
        for player in self.db_players():
            try:
                live = await self.riot.get_live_game(player["puuid"], player["platform"])
            except Exception as e:
                print(f"poll error {player['riot_id']}: {e}", flush=True)
                continue
            event = live_game_event(self.con, player["puuid"], live)
            if event and (channel := self.notify_channel()):
                await channel.send(embed=self.live_embed(player, event["live"]))

    @tasks.loop(hours=6)
    async def sync_matches(self):
        for player in self.db_players():
            try:
                await sync_player(self.con, self.riot, player)
            except Exception as e:
                print(f"sync error {player['riot_id']}: {e}", flush=True)

    @poll_live.before_loop
    @sync_matches.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    def live_embed(self, player: dict, live: dict) -> discord.Embed:
        me = next(p for p in live["participants"] if p["puuid"] == player["puuid"])
        queue = QUEUES.get(live.get("gameQueueConfigId"), "Neznámý mód")
        embed = discord.Embed(
            title=f"🎮 {player['riot_id']} začal hrát!",
            description=f"**{self.champ(me['championId'])}** — {queue}",
            color=0x1F8B4C,
        )
        for team_id, name in ((100, "Modrý tým"), (200, "Červený tým")):
            members = [p for p in live["participants"] if p["teamId"] == team_id]
            embed.add_field(name=name, value="\n".join(
                f"{self.champ(p['championId'])} — {p.get('riotId', '?')}"
                for p in members) or "?", inline=True)
        return embed


RANK_QUEUES = {"RANKED_SOLO_5x5": "SoloQ", "RANKED_FLEX_SR": "Flex"}


class StatsView(discord.ui.View):
    """Přepínatelný /stats embed: Přehled | Posledních 20 | Rekordy + filtr módu."""

    def __init__(self, bot: "TrackerBot", riot_id: str):
        super().__init__(timeout=600)
        self.bot = bot
        self.riot_id = riot_id
        self.page = "overview"
        self.mode = "All"

    def queues(self):
        return stats.QUEUE_GROUPS[self.mode]

    async def build_embed(self) -> discord.Embed:
        title = f"📊 {self.riot_id}" + (f" — {self.mode}" if self.mode != "All" else "")
        embed = discord.Embed(title=title, color=0x3498DB)
        s = stats.summary(self.bot.con, self.riot_id, self.queues())
        if not s:
            embed.description = "Žádné hry v tomhle módu."
            return embed

        if self.page == "overview":
            row = self.bot.con.execute(
                "SELECT * FROM players WHERE riot_id = ?", (self.riot_id,)).fetchone()
            if row:
                try:
                    for e in await self.bot.riot.get_league_entries(
                            row["puuid"], row["platform"]):
                        if (q := RANK_QUEUES.get(e["queueType"])):
                            embed.add_field(name=q, value=(
                                f"**{e['tier'].title()} {e['rank']}** {e['leaguePoints']} LP\n"
                                f"{e['wins']}W/{e['losses']}L "
                                f"({100 * e['wins'] / (e['wins'] + e['losses']):.0f}%)"))
                except Exception:
                    pass
            last = stats.recent_games(self.bot.con, self.riot_id, 20, self.queues())
            wins = sum(g["win"] for g in last)
            k = sum(g["kills"] for g in last); d = sum(g["deaths"] for g in last)
            a = sum(g["assists"] for g in last)
            embed.add_field(
                name=f"Posledních {len(last)} her",
                value=f"**{wins}W/{len(last) - wins}L** · KDA {(k + a) / max(d, 1):.2f}",
            )
            embed.add_field(name=f"Celkem ({s['games']} her)", value=(
                f"{s['winrate']:.1f}% WR · KDA {s['kda']:.2f}\n" + " · ".join(
                    f"{t['champion']} {t['games']}× ({100 * t['wins'] / t['games']:.0f}%)"
                    for t in s["top_champs"][:3])), inline=False)

        elif self.page == "games":
            lines = []
            for g in stats.recent_games(self.bot.con, self.riot_id, 20, self.queues()):
                lines.append(
                    f"{'✅' if g['win'] else '❌'} **{g['champion']}** "
                    f"{g['kills']}/{g['deaths']}/{g['assists']} · {g['cs']} CS · "
                    f"{QUEUES.get(g['queue_id'], '?')} · "
                    f"{stats.fmt_duration(g['duration'])} · {stats._when(g['game_creation'])}")
            embed.description = "\n".join(lines) or "Nic tu není."

        else:  # records
            lines = []
            for r in stats.records(self.bot.con, self.riot_id, self.queues()):
                val = (stats.fmt_duration(r["val"]) if r["label"] == "Nejdelší hra"
                       else stats.fmt_int(r["val"]))
                lines.append(f"{r['label']}: **{val}** — {r['champion']}, "
                             f"{stats._when(r['game_creation'])}, "
                             f"{'✅' if r['win'] else '❌'} {stats.fmt_duration(r['duration'])}")
            embed.description = "\n".join(lines)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Přehled", style=discord.ButtonStyle.primary)
    async def overview_btn(self, interaction, _):
        self.page = "overview"; await self.refresh(interaction)

    @discord.ui.button(label="Posledních 20", style=discord.ButtonStyle.secondary)
    async def games_btn(self, interaction, _):
        self.page = "games"; await self.refresh(interaction)

    @discord.ui.button(label="Rekordy", style=discord.ButtonStyle.secondary)
    async def records_btn(self, interaction, _):
        self.page = "records"; await self.refresh(interaction)

    @discord.ui.select(placeholder="Mód: All", options=[
        discord.SelectOption(label=m) for m in stats.QUEUE_GROUPS])
    async def mode_select(self, interaction, select):
        self.mode = select.values[0]
        select.placeholder = f"Mód: {self.mode}"
        await self.refresh(interaction)


def register_commands(bot: TrackerBot):
    tree = bot.tree

    @tree.command(name="stats", description="Statistiky a rekordy hráče")
    @app_commands.describe(riot_id="GameName#TAG")
    async def stats_cmd(interaction: discord.Interaction, riot_id: str):
        if not stats.summary(bot.con, riot_id):
            await interaction.response.send_message(
                f"Žádná data pro `{riot_id}` — sleduje se? (/track)", ephemeral=True)
            return
        view = StatsView(bot, riot_id)
        await interaction.response.send_message(
            embed=await view.build_embed(), view=view)

    @tree.command(name="live", description="Kdo ze sledovaných právě hraje")
    async def live_cmd(interaction: discord.Interaction):
        rows = bot.con.execute(
            "SELECT l.*, p.riot_id FROM live_games l JOIN players p USING (puuid)"
        ).fetchall()
        if not rows:
            await interaction.response.send_message("Nikdo teď nehraje. 😴")
            return
        await interaction.response.send_message("\n".join(
            f"🎮 **{r['riot_id']}** — {bot.champ(r['champion_id'])} (od {r['started_at']} UTC)"
            for r in rows))

    @tree.command(name="track", description="Přidat hráče ke sledování")
    @app_commands.describe(riot_id="GameName#TAG", platform="eun1 nebo euw1")
    async def track_cmd(interaction: discord.Interaction, riot_id: str, platform: str = "eun1"):
        await interaction.response.defer()
        name, _, tag = riot_id.partition("#")
        try:
            account = await bot.riot.get_account(name.strip(), tag.strip())
        except Exception:
            await interaction.followup.send(f"Účet `{riot_id}` nenalezen.")
            return
        # kanonické jméno z API (uživatelské překlepy/mezery by rozbily joiny)
        canonical = f"{account['gameName']}#{account['tagLine']}"
        bot.con.execute(
            "INSERT OR IGNORE INTO players (puuid, riot_id, platform) VALUES (?,?,?)",
            (account["puuid"], canonical, platform))
        bot.con.commit()
        await interaction.followup.send(
            f"✔ Sleduji `{canonical}` ({platform}). Stahuji celou historii — "
            "dám vědět, až bude hotová (~10 min kvůli rate limitu).")

        async def full_sync():
            player = {"puuid": account["puuid"], "riot_id": canonical,
                      "platform": platform}

            async def announce(n):
                await interaction.channel.send(
                    f"📥 Historie `{canonical}` stažena: {n} zápasů. "
                    f"`/stats riot_id: {canonical}` už funguje naplno. "
                    "(Build ordery se dotahují na pozadí.)")

            try:
                await sync_player(bot.con, bot.riot, player, full=True,
                                  on_matches_done=announce)
            except Exception as e:
                await interaction.channel.send(
                    f"⚠ Stahování historie `{canonical}` selhalo: {e}")

        asyncio.create_task(full_sync())

    @tree.command(name="untrack", description="Přestat sledovat hráče")
    async def untrack_cmd(interaction: discord.Interaction, riot_id: str):
        bot.con.execute("DELETE FROM players WHERE riot_id = ?", (riot_id,))
        bot.con.commit()
        await interaction.response.send_message(f"✔ `{riot_id}` už nesleduji.")

    @tree.command(name="setchannel", description="Posílat notifikace do tohoto kanálu")
    async def setchannel_cmd(interaction: discord.Interaction):
        bot.con.execute(
            "INSERT OR REPLACE INTO settings VALUES ('notify_channel', ?)",
            (str(interaction.channel_id),))
        bot.con.commit()
        await interaction.response.send_message("✔ Notifikace půjdou sem.")


def main():
    from lol.verify import load_env
    load_env()
    TrackerBot().run(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    main()
