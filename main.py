import os
import json
import random
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
import aiofiles

from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=10000)

def keep_alive():
    t = Thread(target=run)
    t.start()


# --------------- 基本設定 ---------------

INTENTS = discord.Intents.default()
INTENTS.message_content = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)

DATA_FILE = "racing_data.json"
JST = timezone(timedelta(hours=9))

# --------------- ユーティリティ ---------------

async def load_data():
    if not os.path.exists(DATA_FILE):
        async with aiofiles.open(DATA_FILE, "w") as f:
            await f.write(json.dumps({
                "horses": {},
                "owners": {},
                "races": [],
                "season": {"year": datetime.now(JST).year, "week": 1},
                "schedule": default_schedule(),
                "rankings": {"prize": {}, "wins": {}, "stable": {}}
            }, ensure_ascii=False))
    async with aiofiles.open(DATA_FILE, "r") as f:
        text = await f.read()
        return json.loads(text)

async def save_data(data):
    async with aiofiles.open(DATA_FILE, "w") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))

def default_schedule():
    # 1か月＝30日周期の簡易GⅠスケジュール
    return {
        1:  {"name": "京都金杯", "distance": 1600, "track": "芝"},
        2:  {"name": "中山金杯", "distance": 2000, "track": "芝"},
        3:  {"name": "フェアリーS", "distance": 1600, "track": "芝"},
        4:  {"name": "京成杯", "distance": 2000, "track": "芝"},
        5:  {"name": "根岸S", "distance": 1400, "track": "ダート"},
        6:  {"name": "東京新聞杯", "distance": 1600, "track": "芝"},
        7:  {"name": "共同通信杯", "distance": 1800, "track": "芝"},
        8:  {"name": "フェブラリーS", "distance": 1600, "track": "ダート"},
        9:  {"name": "高松宮記念", "distance": 1200, "track": "芝"},
        10: {"name": "大阪杯", "distance": 2000, "track": "芝"},
        11: {"name": "桜花賞", "distance": 1600, "track": "芝"},
        12: {"name": "皐月賞", "distance": 2000, "track": "芝"},
        13: {"name": "天皇賞（春）", "distance": 3200, "track": "芝"},
        14: {"name": "NHKマイルC", "distance": 1600, "track": "芝"},
        15: {"name": "日本ダービー", "distance": 2400, "track": "芝"},
        16: {"name": "安田記念", "distance": 1600, "track": "芝"},
        17: {"name": "宝塚記念", "distance": 2200, "track": "芝"},
        18: {"name": "スプリンターズS", "distance": 1200, "track": "芝"},
        19: {"name": "秋華賞", "distance": 2000, "track": "芝"},
        20: {"name": "菊花賞", "distance": 3000, "track": "芝"},
        21: {"name": "天皇賞（秋）", "distance": 2000, "track": "芝"},
        22: {"name": "エリザベス女王杯", "distance": 2200, "track": "芝"},
        23: {"name": "マイルCS", "distance": 1600, "track": "芝"},
        24: {"name": "ジャパンカップ", "distance": 2400, "track": "芝"},
        25: {"name": "チャンピオンズC", "distance": 1800, "track": "ダート"},
        26: {"name": "阪神JF", "distance": 1600, "track": "芝"},
        27: {"name": "朝日杯FS", "distance": 1600, "track": "芝"},
        28: {"name": "東京大賞典", "distance": 2000, "track": "ダート"},
        29: {"name": "ホープフルS", "distance": 2000, "track": "芝"},
        30: {"name": "有馬記念", "distance": 2500, "track": "芝"},
    }

def new_horse_id(data):
    base = "H" + str(random.randint(10000, 99999))
    while base in data["horses"]:
        base = "H" + str(random.randint(10000, 99999))
    return base

def calc_race_score(horse, distance, track):
    speed = horse["stats"]["speed"]
    stamina = horse["stats"]["stamina"]
    temper = horse["stats"]["temper"]
    growth = horse["stats"]["growth"]

    if distance <= 1400:
        base = speed * 0.7 + stamina * 0.3
    elif distance <= 2200:
        base = speed * 0.5 + stamina * 0.5
    else:
        base = speed * 0.3 + stamina * 0.7

    if track == "ダート":
        base *= (0.95 + (temper / 100) * 0.1)
    else:
        base *= (1.0 + (growth / 100) * 0.05)

    rand = random.uniform(0.85, 1.15)
    fatigue = horse.get("fatigue", 0)
    cond = max(0.75, 1.0 - (fatigue * 0.02))

    score = base * rand * cond
    return score

def prize_pool_for_g1():
    total = 200_000
    return total, [0.55, 0.2, 0.12, 0.08, 0.05]

def progress_growth(horse):
    g = horse["stats"]["growth"]
    horse["stats"]["growth"] = min(100, g + random.randint(1, 3))

# --------------- コマンド ---------------

@bot.command(name="newhorse", help="新馬抽選：あなたの厩舎に新しい馬を追加します")
async def newhorse(ctx, name: str):
    data = await load_data()
    uid = str(ctx.author.id)

    # オーナー登録
    if uid not in data["owners"]:
        data["owners"][uid] = {"horses": [], "balance": 0, "wins": 0}

    # 馬の生成（ランダムステータス）
    horse_id = new_horse_id(data)
    stats = {
        "speed": random.randint(50, 95),
        "stamina": random.randint(50, 95),
        "temper": random.randint(40, 90),
        "growth": random.randint(40, 85),
    }
    horse = {
        "id": horse_id,
        "name": name,
        "owner": uid,
        "stats": stats,
        "age": 3,
        "fatigue": 0,
        "wins": 0,
        "history": []
    }

    data["horses"][horse_id] = horse
    data["owners"][uid]["horses"].append(horse_id)
    await save_data(data)

    await ctx.reply(
        f"新馬抽選完了！\nID: {horse_id} / 名前: {name}\n"
        f"ステータス: SPD {stats['speed']} / STA {stats['stamina']} / TEM {stats['temper']} / GRW {stats['growth']}"
    )

@bot.command(name="myhorses", help="自分の馬一覧を表示します")
async def myhorses(ctx):
    data = await load_data()
    uid = str(ctx.author.id)
    owner = data["owners"].get(uid)
    if not owner or not owner["horses"]:
        await ctx.reply("あなたの厩舎には馬がいません。!newhorse <名前> で新馬抽選しましょう。")
        return

    lines = []
    for hid in owner["horses"]:
        h = data["horses"][hid]
        s = h["stats"]
        lines.append(
            f"- {h['name']} (ID: {hid}) / 年齢:{h['age']} / 勝利:{h['wins']} / 疲労:{h['fatigue']} / "
            f"SPD:{s['speed']} STA:{s['stamina']} TEM:{s['temper']} GRW:{s['growth']}"
        )
    await ctx.reply("あなたの馬一覧:\n" + "\n".join(lines))

@bot.command(name="entry", help="今週のGⅠに出走登録します: 例) !entry H12345")
async def entry(ctx, horse_id: str):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)
    if not horse:
        await ctx.reply("そのIDの馬は存在しません。")
        return
    if horse["owner"] != uid:
        await ctx.reply("これはあなたの馬ではありません。")
        return
    # 疲労チェック（一定以上は出走不可）
    if horse.get("fatigue", 0) >= 8:
        await ctx.reply("この馬は疲労が高すぎます。今週は休ませましょう。")
        return

    # レースエントリー（今週の仮リストに保持）
    current_week = data["season"]["week"]
    # レースバッファを作る（開催時まとめて処理）
    pending = data.get("pending_entries", {})
    if str(current_week) not in pending:
        pending[str(current_week)] = []
    # 重複登録防止
    if horse_id in pending[str(current_week)]:
        await ctx.reply("すでに今週のレースにエントリー済みです。")
        return

    pending[str(current_week)].append(horse_id)
    data["pending_entries"] = pending
    await save_data(data)

    await ctx.reply(f"出走登録完了！ 今週({current_week}週)のGⅠに {horse['name']} をエントリーしました。")

@bot.command(name="rest", help="馬を休養させて疲労を回復します: 例) !rest H12345")
async def rest(ctx, horse_id: str):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)
    if not horse:
        await ctx.reply("そのIDの馬は存在しません。")
        return
    if horse["owner"] != uid:
        await ctx.reply("これはあなたの馬ではありません。")
        return
    # 疲労軽減
    old = horse.get("fatigue", 0)
    horse["fatigue"] = max(0, old - 3)
    await save_data(data)
    await ctx.reply(f"{horse['name']} を休養させました。疲労 {old} → {horse['fatigue']}")

@bot.command(name="balance", help="所持賞金と勝利数を確認します")
async def balance(ctx):
    data = await load_data()
    uid = str(ctx.author.id)
    owner = data["owners"].get(uid, {"balance": 0, "wins": 0})
    await ctx.reply(f"賞金: {owner['balance']} / 勝利数: {owner['wins']}")

@bot.command(name="rank", help="ランキング表示（賞金・勝利）")
async def rank(ctx, category: str = "prize"):
    data = await load_data()

    if category not in ["prize", "wins"]:
        await ctx.reply("カテゴリは 'prize' か 'wins' を指定してください。例) !rank prize")
        return

    # 集計
    if category == "prize":
        board = {}
        for uid, o in data["owners"].items():
            board[uid] = o.get("balance", 0)
        sorted_board = sorted(board.items(), key=lambda x: x[1], reverse=True)
        text = "\n".join([f"{i+1}. <@{uid}>: {amt}" for i, (uid, amt) in enumerate(sorted_board[:10])])
        await ctx.reply("賞金ランキング TOP10:\n" + (text if text else "該当者なし"))
    else:
        board = {}
        for uid, o in data["owners"].items():
            board[uid] = o.get("wins", 0)
        sorted_board = sorted(board.items(), key=lambda x: x[1], reverse=True)
        text = "\n".join([f"{i+1}. <@{uid}>: {wins}勝" for i, (uid, wins) in enumerate(sorted_board[:10])])
        await ctx.reply("勝利数ランキング TOP10:\n" + (text if text else "該当者なし"))

@bot.command(name="schedule", help="今週のGⅠ情報を表示します")
async def schedule(ctx):
    data = await load_data()
    week = data["season"]["week"]
    race = data["schedule"].get(week)
    if not race:
        await ctx.reply(f"今週({week}週)はGⅠはありません。")
        return
    await ctx.reply(f"今週({week}週)のGⅠ: {race['name']} / 距離:{race['distance']}m / トラック:{race['track']}")

@bot.command(name="season", help="シーズン情報を表示します")
async def season(ctx):
    data = await load_data()
    await ctx.reply(f"シーズン: {data['season']['year']}年 / 第{data['season']['week']}週")

# --------------- レース開催タスク（毎日開催に変更） ---------------

@tasks.loop(hours=24)
async def daily_race_task():
    await bot.wait_until_ready()
    data = await load_data()

    current_week = data["season"]["week"]
    race_info = data["schedule"].get(current_week)
    entries = data.get("pending_entries", {}).get(str(current_week), [])

    if race_info and len(entries) >= 2:
        total, ratios = prize_pool_for_g1()
        field = []
        for hid in entries:
            horse = data["horses"].get(hid)
            if not horse:
                continue
            score = calc_race_score(horse, race_info["distance"], race_info["track"])
            field.append((hid, horse["name"], horse["owner"], score))

        field.sort(key=lambda x: x[3], reverse=True)

        results = []
        for idx, (hid, hname, owner, score) in enumerate(field):
            pos = idx + 1
            prize = 0
            if idx < len(ratios):
                prize = int(total * ratios[idx])
            o = data["owners"].get(owner)
            if o:
                o["balance"] = o.get("balance", 0) + prize
                if pos == 1:
                    o["wins"] = o.get("wins", 0) + 1

            h = data["horses"].get(hid)
            if h:
                if pos == 1:
                    h["wins"] = h.get("wins", 0) + 1
                h["fatigue"] = min(10, h.get("fatigue", 0) + random.randint(2, 4))
                progress_growth(h)
                h["history"].append({
                    "year": data["season"]["year"],
                    "week": current_week,
                    "race": race_info["name"],
                    "pos": pos,
                    "score": round(score, 2),
                    "prize": prize
                })

            results.append({
                "pos": pos, "horse_id": hid, "horse_name": hname,
                "owner": owner, "score": round(score, 2), "prize": prize
            })

        # レース履歴
        data["races"].append({
            "year": data["season"]["year"],
            "week": current_week,
            "name": race_info["name"],
            "distance": race_info["distance"],
            "track": race_info["track"],
            "results": results
        })

        # 今週のエントリー消去
        data.get("pending_entries", {}).pop(str(current_week), None)

        # 次週へ（30日でリセット）
        data["season"]["week"] += 1
        if data["season"]["week"] > 30:
            data["season"]["week"] = 1
            data["season"]["year"] += 1

        await save_data(data)


@daily_race_task.before_loop
async def before_daily_race_task():
    await bot.wait_until_ready()

# --------------- 管理系（任意） ---------------

@bot.command(name="forcerace", help="[管理] 今週のレースを即時開催します")
@commands.has_permissions(administrator=True)
async def forcerace(ctx):
    await ctx.reply("今週のレース開催を試みます（条件が揃っていれば実行）。")
    await daily_race_task.__call__()

@bot.command(name="resetchannel", help="[管理] 告知チャンネルを設定（未実装のプレースホルダ）")
@commands.has_permissions(administrator=True)
async def resetchannel(ctx):
    await ctx.reply("告知チャンネル機能はこの最小構成では未実装です。必要ならID保持とpostを追加してください。")

# --------------- 起動 ---------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    daily_race_task.start()

if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    bot.run(token)
