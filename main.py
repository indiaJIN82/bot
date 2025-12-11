import os
import json
import random
import asyncio
import calendar
from datetime import datetime, timezone, timedelta, time 

import discord
from discord.ext import commands, tasks
import aiofiles

from flask import Flask
from threading import Thread

# --------------- Keep Alive 用の Flask 設定 ---------------

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

# 2段階認証用の待機状態を保持 (ファイルには保存しないインメモリデータ)
PENDING_RESETS = {}

# 自動レース時刻と事前告知時刻
RACE_TIME_JST = time(hour=19, minute=0, tzinfo=JST)
PRE_ANNOUNCE_TIME_JST = time(hour=18, minute=0, tzinfo=JST) 

# --------------- ユーティリティ ---------------

# 最大保有頭数 
MAX_HORSES_PER_OWNER = 5
# 👈 修正: 1週間に同一オーナーがエントリーできる最大頭数
MAX_ENTRIES_PER_WEEK = 4 

async def load_data():
    """データをロードし、存在しない場合は初期データを作成して保存する"""
    default_data = {
        "horses": {},
        "owners": {},
        "races": [],
        "schedule": default_schedule(),
        "rankings": {"prize": {}, "wins": {}, "stable": {}},
        "announce_channel": None,
        "pending_entries": {}
    }
    
    if not os.path.exists(DATA_FILE):
        today = datetime.now(JST)
        current_week = ((today.day - 1) % 30) + 1
        year = today.year
        month = today.month

        days_in_month = calendar.monthrange(year, month)[1]
        if current_week > days_in_month:
            current_week = 1
            month += 1
            if month > 12:
                month = 1
                year += 1
        
        default_data["season"] = {"year": year, "month": month, "week": current_week}
        
        async with aiofiles.open(DATA_FILE, "w") as f:
            await f.write(json.dumps(default_data, ensure_ascii=False, indent=2))
        return default_data

    async with aiofiles.open(DATA_FILE, "r") as f:
        text = await f.read()
        data = json.loads(text)
        
        if "pending_entries" not in data:
            data["pending_entries"] = {}
        if "announce_channel" not in data:
             data["announce_channel"] = None
        
        # 芝・ダート適性のデータ移行（既存の馬にも適性を付与）
        for hid, horse in data["horses"].items():
            if "turf_apt" not in horse["stats"]:
                horse["stats"]["turf_apt"] = random.randint(50, 90)
                horse["stats"]["dirt_apt"] = random.randint(50, 90)
        
        return data

async def save_data(data):
    """データをファイルに保存する"""
    async with aiofiles.open(DATA_FILE, "w") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))

def default_schedule():
    """レーススケジュール定義（キーは文字列）"""
    return {
        "1":  {"name": "京都金杯", "distance": 1600, "track": "芝"},
        "2":  {"name": "中山金杯", "distance": 2000, "track": "芝"},
        "3":  {"name": "フェアリーS", "distance": 1600, "track": "芝"},
        "4":  {"name": "京成杯", "distance": 2000, "track": "芝"},
        "5":  {"name": "根岸S", "distance": 1400, "track": "ダート"},
        "6":  {"name": "東京新聞杯", "distance": 1600, "track": "芝"},
        "7":  {"name": "共同通信杯", "distance": 1800, "track": "芝"},
        "8":  {"name": "フェブラリーS", "distance": 1600, "track": "ダート"},
        "9":  {"name": "高松宮記念", "distance": 1200, "track": "芝"},
        "10": {"name": "大阪杯", "distance": 2000, "track": "芝"},
        "11": {"name": "桜花賞", "distance": 1600, "track": "芝"},
        "12": {"name": "皐月賞", "distance": 2000, "track": "芝"},
        "13": {"name": "天皇賞（春）", "distance": 3200, "track": "芝"},
        "14": {"name": "NHKマイルC", "distance": 1600, "track": "芝"},
        "15": {"name": "日本ダービー", "distance": 2400, "track": "芝"},
        "16": {"name": "安田記念", "distance": 1600, "track": "芝"},
        "17": {"name": "宝塚記念", "distance": 2200, "track": "芝"},
        "18": {"name": "スプリンターズS", "distance": 1200, "track": "芝"},
        "19": {"name": "秋華賞", "distance": 2000, "track": "芝"},
        "20": {"name": "菊花賞", "distance": 3000, "track": "芝"},
        "21": {"name": "天皇賞（秋）", "distance": 2000, "track": "芝"},
        "22": {"name": "エリザベス女王杯", "distance": 2200, "track": "芝"},
        "23": {"name": "マイルCS", "distance": 1600, "track": "芝"},
        "24": {"name": "ジャパンカップ", "distance": 2400, "track": "芝"},
        "25": {"name": "チャンピオンズC", "distance": 1800, "track": "ダート"},
        "26": {"name": "阪神JF", "distance": 1600, "track": "芝"},
        "27": {"name": "朝日杯FS", "distance": 1600, "track": "芝"},
        "28": {"name": "東京大賞典", "distance": 2000, "track": "ダート"},
        "29": {"name": "ホープフルS", "distance": 2000, "track": "芝"},
        "30": {"name": "有馬記念", "distance": 2500, "track": "芝"},
    }

def new_horse_id(data):
    base = "H" + str(random.randint(10000, 99999))
    while base in data["horses"]:
        base = "H" + str(random.randint(10000, 99999))
    return base

def calc_race_score(horse, distance, track):
    s = horse["stats"]
    speed = s["speed"]
    stamina = s["stamina"]
    temper = s["temper"]
    growth = s["growth"]
    turf_apt = s.get("turf_apt", 70) 
    dirt_apt = s.get("dirt_apt", 70) 

    # 距離適性
    if distance <= 1400:
        base = speed * 0.7 + stamina * 0.3
    elif distance <= 2200:
        base = speed * 0.5 + stamina * 0.5
    else:
        base = speed * 0.3 + stamina * 0.7
    
    # 馬場適性
    if track == "ダート":
        apt_factor = dirt_apt / 100 
    else:
        apt_factor = turf_apt / 100

    # 根幹能力以外の補正
    if track == "ダート":
        condition_factor = 0.95 + (temper / 100) * 0.1 
    else:
        condition_factor = 1.0 + (growth / 100) * 0.05

    rand = random.uniform(0.85, 1.15)
    fatigue = horse.get("fatigue", 0)
    cond = max(0.75, 1.0 - (fatigue * 0.02))

    score = base * apt_factor * condition_factor * rand * cond
    return score

def prize_pool_for_g1():
    total = 200_000
    return total, [0.55, 0.2, 0.12, 0.08, 0.05]

def progress_growth(horse):
    g = horse["stats"]["growth"]
    horse["stats"]["growth"] = min(100, g + random.randint(1, 3))

def generate_commentary(race_info, results, entries_count):
    if entries_count < 2:
        return ""
    
    winner = results[0]
    second = results[1] if len(results) > 1 else None
    
    commentary = [
        f"さあ、ゴール！ 激しい叩き合いを制したのは、見事な走りを見せた**{winner['horse_name']}**だ！",
        f"最後の直線！ **{winner['horse_name']}**が力強い末脚で一気に抜け出し、優勝の栄冠に輝きました！",
    ]
    
    # スコアが results に含まれている前提で比較
    if second and winner['score'] - second['score'] < 5:
        commentary.append(
            f"大接戦！ ほとんど差がありませんでしたが、僅かに**{winner['horse_name']}**の鼻がゴール板を先に通過！ {second['horse_name']}は惜しくも2着！"
        )
    
    if race_info['track'] == 'ダート':
        commentary.append(f"砂塵を巻き上げてのダート戦、**{winner['horse_name']}**が他馬を圧倒しました！")
    elif race_info['distance'] >= 2400:
        commentary.append(f"長距離戦を制したのは、スタミナと根性が光った**{winner['horse_name']}**！")

    return random.choice(commentary)

async def announce_race_results(data, race_info, results, week, year, channel, entries_count):
    
    commentary = generate_commentary(race_info, results, entries_count) 
    
    msg_lines = [
        f"🎉 レース結果速報 - {year}年 第{week}週 🎉",
        f"**【GⅠ {race_info['name']}】** 距離:{race_info['distance']}m / 馬場:{race_info['track']}",
        "---------------------",
        f"🎙️ *{commentary}*", 
        "---------------------",
    ]
    
    for r in results:
        # スコアは小数点第2位まで表示
        msg_lines.append(
            f"{r['pos']}着 **{r['horse_name']}** "
            f"(オーナー:<@{r['owner']}>) "
            f"賞金:{r['prize']} (スコア:{r['score']:.2f})"
        )
        
    for r in results[5:]:
        msg_lines.append(f"{r['pos']}着 **{r['horse_name']}** (オーナー:<@{r['owner']}>)")

    await channel.send("\n".join(msg_lines))

# ----------------- コマンド -----------------

@bot.command(name="resetdata", help="[管理] データファイルを初期化します（2段階認証が必要です）")
@commands.has_permissions(administrator=True)
async def resetdata(ctx):
    global PENDING_RESETS
    
    user_id = ctx.author.id
    
    if user_id in PENDING_RESETS:
        await ctx.reply("既にリセット確認待ちです。`!confirmreset` で確定するか、しばらく待ってキャンセルしてください。")
        return

    # リセット待ち状態を設定し、タイムスタンプを保存
    PENDING_RESETS[user_id] = datetime.now(JST) 
    
    await ctx.reply(
        "⚠️ **警告**: データファイルを初期化します。この操作は元に戻せません。\n"
        "実行する場合は、**10秒以内**に `!confirmreset` と送信してください。"
    )

@bot.command(name="confirmreset", help="[管理] !resetdataの実行を確定します")
@commands.has_permissions(administrator=True) 
async def confirmreset(ctx):
    global PENDING_RESETS
    
    user_id = ctx.author.id
    
    if user_id not in PENDING_RESETS:
        await ctx.reply("リセット確認待ちの状態ではありません。先に `!resetdata` を実行してください。")
        return

    confirmation_time = PENDING_RESETS.pop(user_id)
    time_elapsed = (datetime.now(JST) - confirmation_time).total_seconds()

    if time_elapsed > 10:
        await ctx.reply("リセット確認の期限（10秒）が過ぎました。再度 `!resetdata` を実行してください。")
        return

    if os.path.exists(DATA_FILE):
        os.remove(DATA_FILE)
    
    await ctx.reply("✅ **データファイルを削除しました。** Botを再起動すると新しい状態で始まります。")


@bot.command(name="setannounce", help="[管理] レース結果を告知するチャンネルを設定します")
@commands.has_permissions(administrator=True)
async def setannounce(ctx, channel: discord.TextChannel):
    data = await load_data()
    data["announce_channel"] = channel.id
    await save_data(data)
    await ctx.reply(f"告知チャンネルを {channel.mention} に設定しました。")

@bot.command(name="newhorse", help="新馬抽選：あなたの厩舎に新しい馬を追加します")
async def newhorse(ctx, name: str):
    data = await load_data()
    uid = str(ctx.author.id)

    if uid not in data["owners"]:
        data["owners"][uid] = {"horses": [], "balance": 0, "wins": 0}

    if len(data["owners"][uid]["horses"]) >= MAX_HORSES_PER_OWNER:
        await ctx.reply(f"最大保有頭数**{MAX_HORSES_PER_OWNER}頭**を超えています。`!retire <ID>` で馬を引退させてください。")
        return

    horse_id = new_horse_id(data)
    stats = {
        "speed": random.randint(50, 95),
        "stamina": random.randint(50, 95),
        "temper": random.randint(40, 90),
        "growth": random.randint(40, 85),
        "turf_apt": random.randint(50, 90), 
        "dirt_apt": random.randint(50, 90), 
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
    
    s = stats
    await ctx.reply(
        f"新馬抽選完了！\nID: {horse_id} / 名前: {name}\n"
        f"ステータス: SPD {s['speed']} / STA {s['stamina']} / TEM {s['temper']} / GRW {s['growth']}\n"
        f"適性: 芝 {s['turf_apt']} / ダート {s['dirt_apt']}"
    )

@bot.command(name="retire", help="馬を引退させて厩舎から削除します: 例) !retire H12345")
async def retire(ctx, horse_id: str):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)

    if not horse:
        await ctx.reply("そのIDの馬は存在しません。")
        return
    if horse["owner"] != uid:
        await ctx.reply("これはあなたの馬ではありません。")
        return
    
    data["owners"][uid]["horses"].remove(horse_id)
    del data["horses"][horse_id]
    
    await save_data(data)
    await ctx.reply(f"馬 **{horse['name']} (ID: {horse_id})** を引退させ、厩舎から削除しました。")


@bot.command(name="myhorses", help="自分の馬一覧を表示します")
async def myhorses(ctx):
    data = await load_data()
    uid = str(ctx.author.id)
    owner = data["owners"].get(uid)
    if not owner or not owner["horses"]:
        await ctx.reply("あなたの厩舎には馬がいません。`!newhorse <名前>` で新馬抽選しましょう。")
        return

    lines = ["あなたの馬一覧:"]
    for hid in owner["horses"]:
        h = data["horses"][hid]
        s = h["stats"]
        lines.append(
            f"- {h['name']} (ID: {hid}) / 年齢:{h['age']} / 勝利:{h['wins']} / 疲労:{h['fatigue']} / "
            f"SPD:{s['speed']} STA:{s['stamina']} TEM:{s['temper']} GRW:{s['growth']} / "
            f"芝:{s.get('turf_apt', 'N/A')} ダ:{s.get('dirt_apt', 'N/A')}" 
        )
    await ctx.reply("\n".join(lines))

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
    if horse.get("fatigue", 0) >= 8:
        await ctx.reply("この馬は疲労が高すぎます。今週は休ませましょう。")
        return

    current_week = data["season"]["week"]
    pending = data.get("pending_entries", {})
    week_key = str(current_week)
    
    if week_key not in pending:
        pending[week_key] = []
    
    if horse_id in pending[week_key]:
        await ctx.reply("すでに今週のレースにエントリー済みです。")
        return

    # 🐴 修正箇所: 同一オーナーのエントリー数チェック
    owner_entries = [hid for hid in pending[week_key] if data['horses'][hid]['owner'] == uid]
    if len(owner_entries) >= MAX_ENTRIES_PER_WEEK:
         await ctx.reply(f"今週のエントリーは**{MAX_ENTRIES_PER_WEEK}頭**が上限です。すでに{len(owner_entries)}頭がエントリー済みです。")
         return


    pending[week_key].append(horse_id)
    data["pending_entries"] = pending
    await save_data(data)

    await ctx.reply(f"出走登録完了！ 今週({current_week}週)のGⅠに **{horse['name']}** をエントリーしました。")

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
    
    old = horse.get("fatigue", 0)
    horse["fatigue"] = max(0, old - 3)
    await save_data(data)
    await ctx.reply(f"**{horse['name']}** を休養させました。疲労 {old} → {horse['fatigue']}")

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
        await ctx.reply("カテゴリは 'prize' か 'wins' を指定してください。例) `!rank prize`")
        return

    if category == "prize":
        board = {}
        for uid, o in data["owners"].items():
            board[uid] = o.get("balance", 0)
        sorted_board = sorted(board.items(), key=lambda x: x[1], reverse=True)
        text = "\n"
        for i, (uid, amt) in enumerate(sorted_board[:10]):
            try:
                user = await bot.fetch_user(int(uid))
                username = user.display_name
            except (discord.NotFound, discord.HTTPException):
                username = f"Unknown User ({uid})"
            text += f"{i+1}. {username}: {amt}\n"
        await ctx.reply("賞金ランキング TOP10:\n" + (text if text else "該当者なし"))
    else:
        board = {}
        for uid, o in data["owners"].items():
            board[uid] = o.get("wins", 0)
        sorted_board = sorted(board.items(), key=lambda x: x[1], reverse=True)
        text = "\n"
        for i, (uid, wins) in enumerate(sorted_board[:10]):
            try:
                user = await bot.fetch_user(int(uid))
                username = user.display_name
            except (discord.NotFound, discord.HTTPException):
                username = f"Unknown User ({uid})"
            text += f"{i+1}. {username}: {wins}勝\n"
        await ctx.reply("勝利数ランキング TOP10:\n" + (text if text else "該当者なし"))

@bot.command(name="schedule", help="今週のGⅠ情報を表示します")
async def schedule(ctx):
    if not os.path.exists(DATA_FILE):
        await ctx.reply("データがまだ初期化されていません。`!newhorse` コマンドを実行してデータを初期化してください。")
        return
        
    data = await load_data()
    week_str = str(data["season"]["week"])
    
    race = data["schedule"].get(week_str)
    
    if not race:
        await ctx.reply(f"今週({data['season']['week']}週)はGⅠはありません。")
        return
    await ctx.reply(f"今週({data['season']['week']}週)のGⅠ: **{race['name']}** / 距離:{race['distance']}m / トラック:{race['track']}")

@bot.command(name="season", help="シーズン情報を表示します")
async def season(ctx):
    data = await load_data()
    await ctx.reply(f"シーズン: {data['season']['year']}年 / 第{data['season']['week']}週")

@bot.command(name="racehistory", help="馬の過去のレース結果を表示します: 例) !racehistory H12345")
async def racehistory(ctx, horse_id: str):
    data = await load_data()
    horse = data["horses"].get(horse_id)

    if not horse:
        await ctx.reply("そのIDの馬は存在しません。")
        return

    if not horse.get("history"):
        await ctx.reply(f"**{horse['name']}** はまだレースに出走していません。")
        return

    lines = [f"**{horse['name']}** のレース履歴:"]
    for r in horse["history"]:
        lines.append(
            f" - {r['year']}年 {r['week']}週 {r['race']} ({r['pos']}着) "
            f"賞金:{r['prize']} (スコア:{r['score']:.2f})"
        )
    await ctx.reply("\n".join(lines))


# --------------- レース処理関数（タスクとforceraceで共通利用） ---------------

async def run_race_logic(data, is_forced=False):
    """
    レースを実行し、結果をデータに保存する。
    is_forced=True の場合は週を進めない
    """
    current_week = data["season"]["week"]
    current_week_str = str(current_week)
    
    race_info = data["schedule"].get(current_week_str)
    entries = data.get("pending_entries", {}).get(current_week_str, [])
    entries_count = len(entries) 
    
    channel = None
    channel_id = data.get("announce_channel")
    if channel_id:
        channel = bot.get_channel(channel_id)

    if race_info and entries_count >= 2:
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

        data["races"].append({
            "year": data["season"]["year"],
            "week": current_week,
            "name": race_info["name"],
            "distance": race_info["distance"],
            "track": race_info["track"],
            "results": results
        })

        data.get("pending_entries", {}).pop(current_week_str, None)

        if channel:
            await announce_race_results(data, race_info, results, current_week, data['season']['year'], channel, entries_count)
        
        race_held = True

    elif race_info and entries_count < 2:
        if channel:
            await channel.send(f"⚠️ 今週のGⅠ「{race_info['name']}」はエントリー馬が2頭未満のため開催されませんでした。")
        race_held = False
        
    else:
        race_held = False

    if not is_forced:
        data["season"]["week"] += 1
        if data["season"]["week"] > 30:
            data["season"]["week"] = 1
            data["season"]["month"] += 1
            if data["season"]["month"] > 12:
                data["season"]["month"] = 1
                data["season"]["year"] += 1

    await save_data(data)
    return race_held, race_info, entries_count

# --------------- レース開催タスク（毎日19:00 JSTに実行） ---------------

@tasks.loop(time=RACE_TIME_JST)
async def daily_race_task():
    await bot.wait_until_ready()
    data = await load_data()
    
    await run_race_logic(data, is_forced=False) 

@daily_race_task.before_loop
async def before_daily_race_task():
    await bot.wait_until_ready()

# --------------- 事前告知タスク（毎日18:00 JSTに実行） ---------------

@tasks.loop(time=PRE_ANNOUNCE_TIME_JST)
async def daily_pre_announcement_task():
    await bot.wait_until_ready()
    data = await load_data()

    channel_id = data.get("announce_channel")
    if not channel_id:
        return
        
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    current_week = data["season"]["week"]
    race_info = data["schedule"].get(str(current_week))
    entries = data.get("pending_entries", {}).get(str(current_week), [])
    
    if race_info:
        await channel.send(
            f"🔔 **【出走締切間近のお知らせ】** 🔔\n"
            f"現在のシーズン: {data['season']['year']}年 第{current_week}週\n"
            f"本日19:00 (JST) 開催のGⅠ「**{race_info['name']}**」の出走登録は間もなく締め切られます！\n"
            f"現在のエントリー数: **{len(entries)}**頭\n"
            f"出走登録は `!entry <ID>` コマンドで！"
        )
        
@daily_pre_announcement_task.before_loop
async def before_daily_pre_announcement_task():
    await bot.wait_until_ready()

# --------------- 管理系 ---------------

@bot.command(name="forcerace", help="[管理] 今週のレースを即時開催します（週は進めない）")
@commands.has_permissions(administrator=True)
async def forcerace(ctx):
    data = await load_data()
    
    await ctx.reply("今週のレース開催を試みます（週は進行しません）。")
    
    race_held, race_info, entries_count = await run_race_logic(data, is_forced=True)
    
    if race_held:
        await ctx.send("レース処理が完了しました。結果は告知チャンネルをご確認ください。")
    elif race_info:
        await ctx.send("エントリー馬が2頭未満のためレースは開催されませんでした。")
    else:
        await ctx.send("今週はレースが予定されていませんでした。")


# --------------- 起動 ---------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    daily_race_task.start()
    daily_pre_announcement_task.start() 

if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    bot.run(token)
