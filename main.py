import os
import json
import random
import asyncio
import calendar
from datetime import datetime, timezone, timedelta, time 

import discord
from discord.ext import commands, tasks
from supabase import create_client

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

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_KEY is not set")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

DATA_KEY = "racing_data"

JST = timezone(timedelta(hours=9))

# 2段階認証用の待機状態を保持 (ファイルには保存しないインメモリデータ)
PENDING_RESETS = {}

# 自動レース時刻と事前告知時刻
RACE_TIME_JST = time(hour=19, minute=0, tzinfo=JST)
PRE_ANNOUNCE_TIME_JST = time(hour=18, minute=0, tzinfo=JST) 

# Bot馬用のオーナーID (DiscordのUIDとは異なる、集計用の特殊ID)
BOT_OWNER_ID = "0" 

# --------------- ユーティリティ ---------------

# 最大保有頭数 
MAX_HORSES_PER_OWNER = 5
# 1週間に同一オーナーがエントリーできる最大頭数（ここでは日毎に適用）
MAX_ENTRIES_PER_WEEK = 4 
# GⅠの最低出走頭数（これに満たない場合Bot馬を補充）
MIN_G1_FIELD = 18 # <-- ユーザーの要望により18に変更
# GⅠが開催される最大の日数（週数）
MAX_G1_DAY = 30 

async def load_data():
    default_data = {
        "horses": {},
        "owners": {},
        "races": [],
        "schedule": default_schedule(),
        "rankings": {"prize": {}, "wins": {}, "stable": {}},
        "announce_channel": None,
        "pending_entries": {}
    }

    today = datetime.now(JST)
    default_data["season"] = {
        "year": today.year,
        "month": today.month,
        "day": today.day
    }

    # Supabaseからデータを取得
    res = supabase.table("kv_store").select("value").eq("key", DATA_KEY).execute()

    if not res.data:
        # データがない場合はデフォルトデータを挿入
        supabase.table("kv_store").insert({
            "key": DATA_KEY,
            "value": default_data
        }).execute()
        return default_data

    data = res.data[0]["value"]

    # 既存互換処理
    if "pending_entries" not in data:
        data["pending_entries"] = {}
    if "announce_channel" not in data:
        data["announce_channel"] = None

    return data


async def save_data(data):
    # Supabaseにデータを保存（upsertで更新）
    supabase.table("kv_store").upsert({
        "key": DATA_KEY,
        "value": data
    }).execute()


def default_schedule():
    """レーススケジュール定義（キーは文字列。第1週〜第30週に固定のGⅠを割り当てる）"""
    # 30個のGⅠを、シーズンの1日から30日に対応させる
    return {
        "1":  {"name": "GⅠ 京都金杯", "distance": 1600, "track": "芝"},
        "2":  {"name": "GⅠ 中山金杯", "distance": 2000, "track": "芝"},
        "3":  {"name": "GⅠ フェアリーS", "distance": 1600, "track": "芝"},
        "4":  {"name": "GⅠ 京成杯", "distance": 2000, "track": "芝"},
        "5":  {"name": "GⅠ 根岸S", "distance": 1400, "track": "ダート"},
        "6":  {"name": "GⅠ 東京新聞杯", "distance": 1600, "track": "芝"},
        "7":  {"name": "GⅠ 共同通信杯", "distance": 1800, "track": "芝"},
        "8":  {"name": "GⅠ フェブラリーS", "distance": 1600, "track": "ダート"},
        "9":  {"name": "GⅠ 高松宮記念", "distance": 1200, "track": "芝"},
        "10": {"name": "GⅠ 大阪杯", "distance": 2000, "track": "芝"},
        "11": {"name": "GⅠ 桜花賞", "distance": 1600, "track": "芝"},
        "12": {"name": "GⅠ 皐月賞", "distance": 2000, "track": "芝"},
        "13": {"name": "GⅠ 天皇賞（春）", "distance": 3200, "track": "芝"},
        "14": {"name": "GⅠ NHKマイルC", "distance": 1600, "track": "芝"},
        "15": {"name": "GⅠ 日本ダービー", "distance": 2400, "track": "芝"},
        "16": {"name": "GⅠ 安田記念", "distance": 1600, "track": "芝"},
        "17": {"name": "GⅠ 宝塚記念", "distance": 2200, "track": "芝"},
        "18": {"name": "GⅠ スプリンターズS", "distance": 1200, "track": "芝"},
        "19": {"name": "GⅠ 秋華賞", "distance": 2000, "track": "芝"},
        "20": {"name": "GⅠ 菊花賞", "distance": 3000, "track": "芝"},
        "21": {"name": "GⅠ 天皇賞（秋）", "distance": 2000, "track": "芝"},
        "22": {"name": "GⅠ エリザベス女王杯", "distance": 2200, "track": "芝"},
        "23": {"name": "GⅠ マイルCS", "distance": 1600, "track": "芝"},
        "24": {"name": "GⅠ ジャパンカップ", "distance": 2400, "track": "芝"},
        "25": {"name": "GⅠ チャンピオンズC", "distance": 1800, "track": "ダート"},
        "26": {"name": "GⅠ 阪神JF", "distance": 1600, "track": "芝"},
        "27": {"name": "GⅠ 朝日杯FS", "distance": 1600, "track": "芝"},
        "28": {"name": "GⅠ 東京大賞典", "distance": 2000, "track": "ダート"},
        "29": {"name": "GⅠ ホープフルS", "distance": 2000, "track": "芝"},
        "30": {"name": "GⅠ 有馬記念", "distance": 2500, "track": "芝"},
    }

def new_horse_id(data):
    """プレイヤー馬のID生成"""
    base = "H" + str(random.randint(10000, 99999))
    while base in data["horses"]:
        base = "H" + str(random.randint(10000, 99999))
    return base

def new_bot_horse_id(existing_ids):
    """Bot馬のID生成（重複しないように確認）"""
    base = "B" + str(random.randint(10000, 99999))
    while base in existing_ids:
        base = "B" + str(random.randint(10000, 99999))
    return base

def generate_bot_horse(existing_ids):
    """Bot馬を生成する"""
    horse_id = new_bot_horse_id(existing_ids)
    
    stats = {
        "speed": random.randint(70, 100),
        "stamina": random.randint(70, 100),
        "temper": random.randint(60, 95),
        "growth": random.randint(60, 95),
        "turf_apt": random.randint(60, 95), 
        "dirt_apt": random.randint(60, 95), 
    }
    
    bot_names = [
        "キョウカイノホシ", "アイビスフライト", "シルバーファントム", 
        "レジェンドブルー", "グランドマスター", "ウィニングラン", 
        "エンペラーゲイツ", "シャドウキング", "フューチャーワン", "カチウマ"
    ]
    
    return {
        "id": horse_id,
        "name": random.choice(bot_names) + str(random.randint(1, 9)),
        "owner": BOT_OWNER_ID, 
        "stats": stats,
        "age": random.randint(3, 5),
        "fatigue": 0,
        "wins": 0,
        "history": [],
        "favorite": False 
    }


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

def prize_pool_for_lower():
    """下級レースの賞金設定"""
    total = 17000 
    return total, [10000/17000, 5000/17000, 2000/17000] # 10000, 5000, 2000

def progress_growth(horse):
    g = horse["stats"]["growth"]
    horse["stats"]["growth"] = min(100, g + random.randint(1, 3))

def generate_commentary(race_info, results, entries_count):
    if entries_count < 2:
        return ""
    
    winner = results[0]
    second = results[1] if len(results) > 1 else None
    
    if winner['owner'] == BOT_OWNER_ID:
        commentary = [
             f"無敵の強さ！ 協会生産の**{winner['horse_name']}**が他馬を寄せ付けず圧勝！ プレイヤー勢は歯が立ちませんでした！",
             f"ゴール板前で、Botの刺客**{winner['horse_name']}**が驚異的な末脚を炸裂！ 悔しい協会側の勝利です！",
        ]
    elif race_info['name'].startswith("GⅠ"):
        commentary = [
            f"さあ、ゴール！ 激しい叩き合いを制したのは、見事な走りを見せた**{winner['horse_name']}**だ！",
            f"最後の直線！ **{winner['horse_name']}**が力強い末脚で一気に抜け出し、優勝の栄冠に輝きました！",
        ]
    else: 
        commentary = [
            f"最終レース、**{winner['horse_name']}**が混戦を抜け出し、見事一発逆転を決めました！",
            f"力の違いを見せつけた**{winner['horse_name']}**が、最後の賞金を獲得しました！",
        ]
    
    if second and winner['score'] - second['score'] < 5 and race_info['name'].startswith("GⅠ"):
        commentary.append(
            f"大接戦！ ほとんど差がありませんでしたが、僅かに**{winner['horse_name']}**の鼻がゴール板を先に通過！ {second['horse_name']}は惜しくも2着！"
        )
    
    if race_info['track'] == 'ダート':
        commentary.append(f"砂塵を巻き上げてのダート戦、**{winner['horse_name']}**が他馬を圧倒しました！")
    elif race_info['distance'] >= 2400:
        commentary.append(f"長距離戦を制したのは、スタミナと根性が光った**{winner['horse_name']}**！")

    return random.choice(commentary)

async def announce_race_results(data, race_info, results, day, month, year, channel, entries_count):
    commentary = generate_commentary(race_info, results, entries_count) 
    
    # 日をそのまま週として表示
    week_display = day
    
    if race_info['name'].startswith("GⅠ"):
         title = f"🎉 レース結果速報 - {year}年 {month}月 第{week_display}週 🎉"
         race_line = f"**【{race_info['name']}】** 距離:{race_info['distance']}m / 馬場:{race_info['track']} / **{entries_count}頭立て**"
    else:
         title = f"📢 下級レース結果 - {year}年 {month}月 第{week_display}週"
         race_line = f"**【{race_info['name']}】** 距離:{race_info['distance']}m / 馬場:{race_info['track']} / **{entries_count}頭立て**"
    
    msg_lines = [
        title,
        race_line,
        "---------------------",
        f"🎙️ *{commentary}*", 
        "---------------------",
    ]
    
    prize_count = 5 if race_info['name'].startswith("GⅠ") else 3

    for r in results:
        owner_display = ""
        if r['owner'] == BOT_OWNER_ID:
            owner_display = "**協会生産**"
        else:
            owner_display = f"<@{r['owner']}>"
        
        # 馬番を表示
        line = f"**{r['pos']}着** ({r['post_position']}番) **{r['horse_name']}** (オーナー:{owner_display})"
        
        if r['pos'] <= prize_count:
             line += f" 賞金:{r['prize']} (スコア:{r['score']:.2f})"
        
        msg_lines.append(line)
        
    await channel.send("\n".join(msg_lines))

# データ整合性を保つためのヘルパー関数
def _clean_pending_entry(data, horse_id):
    """
    指定された馬IDを、すべてのpending_entriesリストから削除します。
    馬を引退させる際に呼び出し、参照エラーを防ぎます。
    """
    cleaned = False
    if "pending_entries" in data:
        # pending_entriesは {day_key: [horse_id, ...]} の形式
        for day_key in list(data["pending_entries"].keys()):
            if horse_id in data["pending_entries"][day_key]:
                data["pending_entries"][day_key].remove(horse_id)
                cleaned = True
            # エントリーリストが空になったらキー自体を削除
            if not data["pending_entries"][day_key]:
                del data["pending_entries"][day_key]
    return cleaned

# 一括エントリー処理のコアロジック
async def _perform_bulk_entry(ctx, data, target_horses, entry_type):
    uid = str(ctx.author.id)
    current_day = data["season"]["day"]
    current_day_str = str(current_day)
    
    # 1. GⅠ開催日チェック
    if current_day > MAX_G1_DAY:
         await ctx.reply(f"本日({current_day}日)はGⅠ開催日ではないため、エントリーできません。")
         return

    pending = data.get("pending_entries", {})
    if current_day_str not in pending:
        pending[current_day_str] = []
        
    # 2. 処理対象となる馬のリストを作成 (疲労 < 8 の馬のみ)
    eligible_horses = []
    for hid in target_horses:
        horse = data["horses"].get(hid)
        if horse and horse["owner"] == uid:
            # 疲労チェック
            if horse.get("fatigue", 0) >= 8:
                continue
            
            eligible_horses.append(hid)
            
    # 3. 上限チェック (厳格: 5頭以上当てはまる場合は拒否)
    if len(eligible_horses) > MAX_ENTRIES_PER_WEEK:
        horse_names = [data["horses"][hid]["name"] for hid in eligible_horses]
        await ctx.reply(
            f"⚠️ **一括登録失敗**: あなたの厩舎には出走可能な馬が**{len(eligible_horses)}頭**います。\n"
            f"一括登録の上限**{MAX_ENTRIES_PER_WEEK}頭**を超過しているため、登録をキャンセルしました。\n"
            f"**対象馬**: {', '.join(horse_names)}"
        )
        return
        
    # 4. 登録処理
    registered_count = 0
    already_entered_count = 0
    
    for hid in eligible_horses:
        if hid in pending[current_day_str]:
            already_entered_count += 1
            continue
            
        # 登録実行
        pending[current_day_str].append(hid)
        registered_count += 1

    data["pending_entries"] = pending
    await save_data(data)
    
    # 5. 結果報告
    if registered_count == 0 and already_entered_count == 0:
        await ctx.reply(f"ℹ️ {entry_type}に該当し、出走可能な馬（疲労8未満）はいませんでした。")
    elif registered_count == 0 and already_entered_count > 0:
         await ctx.reply(f"✅ {entry_type}に該当する馬は全てすでに本日のレースにエントリー済みです (**{already_entered_count}頭**)。")
    else:
        status_msg = f"✅ {entry_type}の馬**{registered_count}頭**を本日のレースに出走登録しました。"
        if already_entered_count > 0:
             status_msg += f" (うち{already_entered_count}頭は既に登録済みでした)"
        await ctx.reply(status_msg)


# ----------------- コマンド -----------------

@bot.command(name="resetdata", help="[管理] データファイルを初期化します（2段階認証が必要です）")
@commands.has_permissions(administrator=True)
async def resetdata(ctx):
    global PENDING_RESETS
    
    user_id = ctx.author.id
    
    if user_id in PENDING_RESETS:
        await ctx.reply("既にリセット確認待ちです。`!confirmreset` で確定するか、しばらく待ってキャンセルしてください。")
        return

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

    # Supabaseのデータを削除
    supabase.table("kv_store").delete().eq("key", DATA_KEY).execute()
    
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
        await ctx.reply(f"最大保有頭数**{MAX_HORSES_PER_OWNER}頭**を超えています。`!retire <ID>` または `!massretire` で馬を引退させてください。")
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
        "history": [],
        "favorite": False 
    }

    data["horses"][horse_id] = horse
    data["owners"][uid]["horses"].append(horse_id)
    await save_data(data)
    
    s = stats
    await ctx.reply(
        f"新馬抽選完了！\nID: {horse_id} / 名前: {name}\n"
        f"ステータス: SPD {s['speed']} / STA {s['stamina']} / TEM {s['temper']} / GRW {s['growth']}\n"
        f"適性: 芝 {s['turf_apt']} / ダート {s['dirt_apt']}\n"
        f"お気に入り登録: {horse['favorite']}"
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
    
    # 【バグ修正】pending_entriesから馬IDを削除
    _clean_pending_entry(data, horse_id) 
    
    data["owners"][uid]["horses"].remove(horse_id)
    del data["horses"][horse_id]
    
    await save_data(data)
    await ctx.reply(f"馬 **{horse['name']} (ID: {horse_id})** を引退させ、厩舎から削除しました。")


@bot.command(name="massretire", help="お気に入り以外の馬を全て引退させます (🚨要確認)")
async def massretire(ctx):
    data = await load_data()
    uid = str(ctx.author.id)
    owner = data["owners"].get(uid)
    
    if not owner or not owner["horses"]:
        await ctx.reply("あなたの厩舎には馬がいません。")
        return

    to_retire = []
    to_keep = []
    
    # お気に入りでない馬を選別
    for hid in owner["horses"]:
        horse = data["horses"].get(hid)
        if horse and not horse.get("favorite", False):
            to_retire.append(hid)
        elif horse:
            to_keep.append(hid)

    if not to_retire:
        await ctx.reply("お気に入り登録されている馬しかいません。削除対象の馬がいません。")
        return
        
    # 削除実行
    for hid in to_retire:
        # 【バグ修正】pending_entriesから馬IDを削除
        _clean_pending_entry(data, hid) 
        if hid in data["horses"]:
             del data["horses"][hid]
    
    # オーナーの馬リストを更新
    data["owners"][uid]["horses"] = to_keep
    
    await save_data(data)
    
    keep_names = [data["horses"][hid]["name"] for hid in to_keep]
    
    reply_msg = [
        f"✅ **{len(to_retire)}頭**の馬を引退させました。",
        "---",
        f"現在厩舎に残っている馬 (**{len(to_keep)}頭**) (お気に入り):"
    ]
    if keep_names:
        reply_msg.append(", ".join(keep_names))
    else:
        reply.append("なし")
        
    await ctx.reply("\n".join(reply_msg))

@bot.command(name="favorite", help="馬をお気に入りに登録します (全削除除外対象): 例) !favorite H12345")
async def favorite(ctx, horse_id: str):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)
    
    if not horse or horse["owner"] != uid:
        await ctx.reply("そのIDの馬は存在しないか、あなたの馬ではありません。")
        return
    
    horse["favorite"] = True
    await save_data(data)
    await ctx.reply(f"**{horse['name']}** をお気に入りに登録しました。`!massretire` の対象から除外されます。")

@bot.command(name="unfavorite", help="馬のお気に入り登録を解除します: 例) !unfavorite H12345")
async def unfavorite(ctx, horse_id: str):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)
    
    if not horse or horse["owner"] != uid:
        await ctx.reply("そのIDの馬は存在しないか、あなたの馬ではありません。")
        return
    
    horse["favorite"] = False
    await save_data(data)
    await ctx.reply(f"**{horse['name']}** のお気に入り登録を解除しました。`!massretire` の対象となります。")


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
        fav_icon = "⭐" if h.get("favorite", False) else " "
        lines.append(
            f"{fav_icon} - {h['name']} (ID: {hid}) / 年齢:{h['age']} / 勝利:{h['wins']} / 疲労:{h['fatigue']} / "
            f"SPD:{s['speed']} STA:{s['stamina']} TEM:{s['temper']} GRW:{s['growth']} / "
            f"芝:{s.get('turf_apt', 'N/A')} ダ:{s.get('dirt_apt', 'N/A')}" 
        )
    await ctx.reply("\n".join(lines))

@bot.command(name="entry", help="本日のGⅠに出走登録します: 例) !entry H12345")
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

    current_day = data["season"]["day"]
    
    if current_day > MAX_G1_DAY:
         await ctx.reply(f"本日({current_day}日)はGⅠ開催日ではないため、エントリーできません。")
         return
         
    pending = data.get("pending_entries", {})
    day_key = str(current_day)
    
    if day_key not in pending:
        pending[day_key] = []
    
    if horse_id in pending[day_key]:
        await ctx.reply("すでに本日のレースにエントリー済みです。")
        return

    owner_entries = [hid for hid in pending[day_key] if data['horses'].get(hid) and data['horses'][hid]['owner'] == uid]
    if len(owner_entries) >= MAX_ENTRIES_PER_WEEK:
         await ctx.reply(f"本日のエントリーは**{MAX_ENTRIES_PER_WEEK}頭**が上限です。すでに{len(owner_entries)}頭がエントリー済みです。")
         return


    pending[day_key].append(horse_id)
    data["pending_entries"] = pending
    await save_data(data)

    await ctx.reply(f"出走登録完了！ 本日(第{current_day}週)のGⅠに **{horse['name']}** をエントリーしました。")
    
# 【新規追加】お気に入り馬の一括エントリーコマンド
@bot.command(name="entryfav", help="お気に入り馬を本日のGⅠに一括登録します")
async def entryfav(ctx):
    data = await load_data()
    uid = str(ctx.author.id)
    owner_horses = data["owners"].get(uid, {}).get("horses", [])
    
    # お気に入り馬のみを抽出
    favorite_horses = [
        hid for hid in owner_horses 
        if data["horses"].get(hid) and data["horses"][hid].get("favorite", False)
    ]
    
    await _perform_bulk_entry(ctx, data, favorite_horses, "お気に入り")

# 【新規追加】全頭の一括エントリーコマンド
@bot.command(name="entryall", help="全頭を本日のGⅠに一括登録します（疲労8未満）")
async def entryall(ctx):
    data = await load_data()
    uid = str(ctx.author.id)
    all_horses = data["owners"].get(uid, {}).get("horses", [])
    
    await _perform_bulk_entry(ctx, data, all_horses, "全頭")


@bot.command(name="entries", help="本日のGⅠレースの出馬表を表示します")
async def entries(ctx):
    data = await load_data()
    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]
    current_day_str = str(current_day)
    
    if current_day > MAX_G1_DAY:
        await ctx.reply(f"{current_year}年{current_month}月 第{current_day}日（第{current_day}週）はGⅠ開催日ではありません。")
        return
        
    race_info = data["schedule"].get(current_day_str)
    
    if not race_info:
        await ctx.reply(f"本日({current_day}日)はGⅠはありません。（スケジュールに定義されていません）")
        return
    
    entries_list = data.get("pending_entries", {}).get(current_day_str, [])
    
    if not entries_list:
        await ctx.reply(f"本日のGⅠ「**{race_info['name']}**」にエントリーされている馬はいません。`!entry <ID>` で登録してください！")
        return

    # GⅠレース情報
    header = [
        f"**🏆 {current_year}年{current_month}月 第{current_day}週 のGⅠ出馬表: {race_info['name']}**",
        f"距離: {race_info['distance']}m / 馬場: {race_info['track']}",
        "------------------------------------"
    ]
    
    entries_data = []
    
    # 登録順に馬番を割り振る
    post_position_counter = 1
    for hid in entries_list:
        horse = data["horses"].get(hid)
        if not horse:
            # エントリーリストに存在するがhorsesに存在しないIDは無視 (過去のバグ馬ID対策)
            continue
            
        # Bot馬はentriesコマンドでは表示しない
        if horse["owner"] == BOT_OWNER_ID:
             continue
        
        # オーナー名を取得
        owner_name = "不明なオーナー"
        try:
            owner_user = bot.get_user(int(horse["owner"])) or await bot.fetch_user(int(horse["owner"]))
            owner_name = owner_user.display_name
        except:
            pass
            
        entries_data.append({
            "name": horse["name"],
            "id": hid,
            "owner": owner_name,
            "fatigue": horse.get("fatigue", 0),
            "wins": horse.get("wins", 0),
            "post_position": post_position_counter # 登録順に馬番を付与
        })
        post_position_counter += 1

    if not entries_data:
        await ctx.reply(f"本日のGⅠ「**{race_info['name']}**」にエントリーされているプレイヤー馬はいません。`!entry <ID>` で登録してください！")
        return
        
    # 馬番順にソートして表示
    entries_data.sort(key=lambda x: x["post_position"])

    # 表示をテーブル形式で整形 (Markdownのテーブル記法を使用)
    body = [""]
    # ヘッダー
    body.append(f"| {'馬番':<3} | {'ID':<6} | {'馬名':<10} | {'オーナー':<15} | {'疲労':<4} |")
    # 整形ライン
    body.append("|:---:|:-----|:-----------|:-----------------|:----:|")
    
    for entry in entries_data:
        body.append(
            f"| {entry['post_position']:<3} | {entry['id']} | {entry['name']} | {entry['owner']} | {entry['fatigue']} |"
        )
        
    await ctx.reply("\n".join(header + body))

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
            if uid == BOT_OWNER_ID: continue
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
            if uid == BOT_OWNER_ID: continue
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

@bot.command(name="schedule", help="本日のGⅠ情報を表示します")
async def schedule(ctx):
        
    data = await load_data()
    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]
    day_str = str(current_day)
    
    if current_day > MAX_G1_DAY:
        await ctx.reply(f"{current_year}年{current_month}月 第{current_day}日（第{current_day}週）はGⅠ開催日ではありません。")
        return
        
    race = data["schedule"].get(day_str)
    
    if not race:
        await ctx.reply(f"本日({current_day}日)はGⅠはありません。（スケジュールに定義されていません）")
        return
        
    # 日をそのまま週として表示
    await ctx.reply(
        f"本日({current_year}年{current_month}月 第{current_day}週)のGⅠ: "
        f"**{race['name']}** / 距離:{race['distance']}m / トラック:{race['track']}"
    )

@bot.command(name="season", help="シーズン情報を表示します")
async def season(ctx):
    data = await load_data()
    # 日をそのまま週として表示
    current_day = data['season']['day']
    current_month = data['season']['month']
    current_year = data['season']['year']
    await ctx.reply(f"シーズン: {current_year}年 {current_month}月 / 第{current_day}週")

@bot.command(name="racehistory", help="馬の過去のレース結果を表示します: 例) !racehistory H12345")
async def racehistory(ctx, horse_id: str):
    data = await load_data()
    horse = data["horses"].get(horse_id)

    if not horse:
        await ctx.reply("そのIDの馬は存在しません。")
        return
    
    if horse["owner"] == BOT_OWNER_ID:
        await ctx.reply("このコマンドでは協会生産馬の履歴は確認できません。")
        return

    if not horse.get("history"):
        await ctx.reply(f"**{horse['name']}** はまだレースに出走していません。")
        return

    lines = [f"**{horse['name']}** のレース履歴:"]
    for r in horse["history"]:
        # 履歴データには month と day が含まれるようになった
        day = r.get('day', 'N/A')
        month = r.get('month', 'N/A')
        year = r.get('year', 'N/A')
        lines.append(
            f" - {year}年 {month}月 第{day}週 {r['race']} ({r['pos']}着) "
            f"賞金:{r['prize']} (スコア:{r['score']:.2f})"
        )
    await ctx.reply("\n".join(lines))

@bot.command(name="allraces", help="過去の全レース結果の概要を最新15件表示します")
async def allraces(ctx):
    data = await load_data()

    races = data.get("races", [])
    if not races:
        await ctx.reply("過去に開催されたレースの記録はありません。")
        return

    # 最新の15件を取得し、新しい順に並べ替える
    latest_races = races[-15:][::-1]

    lines = ["**🏆 過去のレース結果 (最新15件)**", "------------------------------------"]

    for race in latest_races:
        year = race.get('year', 'N/A')
        month = race.get('month', 'N/A')
        day = race.get('day', 'N/A')
        race_name = race['name']
        
        # 確実に結果が存在することを確認
        if not race.get("results"):
            continue 

        # 1着馬の情報
        winner = race['results'][0]
        winner_id = winner['owner']
        winner_name = winner['horse_name']
        
        owner_display = "協会生産"
        if winner_id != BOT_OWNER_ID:
            try:
                # オーナーのユーザー名を取得
                owner_user = bot.get_user(int(winner_id)) or await bot.fetch_user(int(winner_id))
                owner_display = owner_user.display_name
            except Exception:
                owner_display = f"不明なオーナー ({winner_id})"

        
        lines.append(
            f"📅 {year}/{month} 第{day}週: **{race_name}** - 🥇{winner_name} (オーナー: {owner_display})"
        )

    await ctx.reply("\n".join(lines))

# 【新規追加】特定の日の全レース結果を表示するコマンド
@bot.command(name="raceresults", help="過去のレース全結果を表示します: 例) !raceresults 2024 1 1 (2024年1月 第1週のレース)")
async def raceresults(ctx, year: int, month: int, day: int):
    data = await load_data()
    
    # 指定された年、月、日のレース結果を検索
    found_races = [
        r for r in data["races"] 
        if r.get("year") == year and r.get("month") == month and r.get("day") == day
    ]
    
    if not found_races:
        await ctx.reply(f"{year}年{month}月 第{day}週 に開催されたレースの結果は見つかりませんでした。\n(レースは開催日と開催順に記録されます)")
        return
    
    response_lines = []
        
    for race in found_races:
        race_info = {
            "name": race["name"],
            "distance": race["distance"],
            "track": race["track"]
        }
        results = race["results"]
        entries_count = len(results)
        
        # 結果表示のヘッダー
        msg_lines = [
            "========================",
            f"**🏆 {race_info['name']} 結果 ({year}年{month}月 第{day}週)**",
            f"距離: {race_info['distance']}m / 馬場: {race_info['track']} / **{entries_count}頭立て**",
            "------------------------"
        ]
        
        # 賞金が付く順位を決定 (GⅠは5着まで、下級レースは3着まで)
        # GⅠは名前に 'GⅠ' が含まれることで判定
        prize_count = 5 if race_info['name'].startswith("GⅠ") else 3

        for r in results:
            owner_display = ""
            if r['owner'] == BOT_OWNER_ID:
                owner_display = "**協会生産**"
            else:
                # オーナーのDiscord表示名を取得
                try:
                    owner_user = bot.get_user(int(r['owner'])) or await bot.fetch_user(int(r['owner']))
                    owner_display = owner_user.display_name
                except:
                    owner_display = f"ID:{r['owner']}" # 取得できない場合はIDを表示
            
            line = f"**{r['pos']}着** ({r['post_position']}番) **{r['horse_name']}** (オーナー:{owner_display})"
            
            # race_historyにはscoreが保存されているが、race_resultsには保存されていないため、prizeのみ表示
            if r.get('prize', 0) > 0:
                 line += f" 賞金:{r['prize']}" 
            
            msg_lines.append(line)
        
        response_lines.extend(msg_lines)
        response_lines.append("\n") # レース間に空白行を追加
    
    # 最後の空行を削除
    if response_lines and response_lines[-1] == "\n":
        response_lines.pop()

    await ctx.reply("\n".join(response_lines))

# ----------------- 下級レース処理関数 -----------------

async def run_lower_race_logic(data, horses_not_entered, current_day, current_month, current_year, channel):
    """
    GⅠに出走しなかった馬を対象に下級レースを自動開催する
    """
    
    entries = [hid for hid in horses_not_entered if data["horses"].get(hid) and data["horses"][hid]["owner"] != BOT_OWNER_ID]
    entries_count = len(entries)
    
    if entries_count < 2:
        if channel:
             await channel.send(f"ℹ️ {current_year}年{current_month}月 第{current_day}週 の下級レースはエントリー馬が2頭未満のため開催されませんでした。")
        return

    # 下級レースのランダムな設定
    random_distance = random.choice([1200, 1400, 1600, 1800, 2000, 2200, 2400])
    random_track = random.choice(["芝", "ダート"])
    
    race_info = {
        "name": "一発逆転！京都ファイナルレース", 
        "distance": random_distance,
        "track": random_track
    }
    
    total, ratios = prize_pool_for_lower() 

    field = []
    # 馬番割り振りとデータ整形 (エントリー順に1から割り振る)
    for idx, hid in enumerate(entries):
        horse = data["horses"].get(hid)
        score = calc_race_score(horse, race_info["distance"], race_info["track"])
        
        field.append({
            "id": hid, "name": horse["name"], "owner": horse["owner"], 
            "score": score, "post_position": idx + 1 # 1から始まる馬番を割り振り
        })

    field.sort(key=lambda x: x["score"], reverse=True) # スコアで着順を決定

    results = []
    for idx, entry in enumerate(field):
        pos = idx + 1
        hid = entry["id"]
        owner = entry["owner"]
        score = entry["score"]
        hname = entry["name"]
        
        prize = 0
        if idx == 0: prize = 10000
        elif idx == 1: prize = 5000
        elif idx == 2: prize = 2000

        # オーナーデータ更新
        o = data["owners"].get(owner)
        if o:
            o["balance"] = o.get("balance", 0) + prize
            if pos == 1:
                o["wins"] = o.get("wins", 0) + 1

        # 馬データ更新
        h = data["horses"].get(hid)
        if h:
            if pos == 1:
                h["wins"] = h.get("wins", 0) + 1
            h["fatigue"] = min(10, h.get("fatigue", 0) + random.randint(1, 3)) 
            progress_growth(h)
            
            # 履歴に year, month, day を保存
            h["history"].append({
                "year": current_year,
                "month": current_month,
                "day": current_day,
                "race": race_info["name"],
                "pos": pos,
                "score": round(score, 2),
                "prize": prize
            })

        results.append({
            "pos": pos, 
            "horse_id": hid, 
            "horse_name": hname,
            "owner": owner, 
            "score": round(score, 2), 
            "prize": prize,
            "post_position": entry["post_position"] # 割り振った馬番を使用
        })

    # レース記録に year, month, day を保存
    data["races"].append({
        "year": current_year,
        "month": current_month,
        "day": current_day,
        "name": race_info["name"],
        "distance": random_distance,
        "track": random_track,
        "results": results
    })

    if channel:
        # 告知関数に day, month, year を渡す
        await announce_race_results(data, race_info, results, current_day, current_month, current_year, channel, entries_count)

# --------------- レース処理関数（タスクとforceraceで共通利用） ---------------

async def run_race_logic(data, is_forced=False):
    """
    GⅠレースを実行し、その後下級レースを実行する
    """
    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]
    current_day_str = str(current_day)
    
    # GⅠは1日から30日に開催（31日はGⅠなし）
    if current_day <= MAX_G1_DAY:
        race_info = data["schedule"].get(current_day_str)
    else:
        race_info = None # GⅠ無し

    
    channel = None
    channel_id = data.get("announce_channel")
    if channel_id:
        channel = bot.get_channel(channel_id)

    # ------------------ 1. GⅠレースの実行準備 ------------------
    
    g1_entries_raw = data.get("pending_entries", {}).get(current_day_str, [])
    
    # 存在しない馬IDを削除（古いバグが残したゴミデータ対策）
    g1_entries = [hid for hid in g1_entries_raw if hid in data["horses"]]
    data["pending_entries"][current_day_str] = g1_entries 
    
    player_entries_count = len(g1_entries) 
    
    g1_held = False
    
    if race_info: # GⅠが予定されている日（1〜30日）
        bot_horses_to_add = []
        
        # Bot馬の補充が必要な数の計算
        num_bot_horses = max(0, MIN_G1_FIELD - player_entries_count)
        
        # プレイヤー馬のIDを結合してBot馬のIDの重複を避ける
        existing_ids = set(data["horses"].keys()) 
        
        for _ in range(num_bot_horses):
            bot_horse = generate_bot_horse(existing_ids)
            bot_horses_to_add.append(bot_horse)
            # Bot馬を data["horses"] に追加
            data["horses"][bot_horse["id"]] = bot_horse 
            existing_ids.add(bot_horse["id"])
        
        total_entries_count = player_entries_count + len(bot_horses_to_add)
        
        if total_entries_count >= 2:
            
            total, ratios = prize_pool_for_g1()
            field = []
            current_post_position = 1 # 馬番のカウンタ
            
            # プレイヤー馬のデータを取得し、馬番を割り振る (登録順)
            for hid in g1_entries:
                horse = data["horses"].get(hid)
                # ここで再度のチェックは不要（既にフィルタリング済み）
                score = calc_race_score(horse, race_info["distance"], race_info["track"])
                field.append({
                    "id": hid, "name": horse["name"], "owner": horse["owner"], 
                    "score": score, "post_position": current_post_position
                })
                current_post_position += 1
                
            # Bot馬のデータを追加し、馬番を割り振る (プレイヤー馬の次から)
            for horse in bot_horses_to_add:
                score = calc_race_score(horse, race_info["distance"], race_info["track"])
                field.append({
                    "id": horse["id"], "name": horse["name"], "owner": horse["owner"], 
                    "score": score, "post_position": current_post_position
                })
                current_post_position += 1


            # ------------------ 1-1. GⅠレースの実行 ------------------

            field.sort(key=lambda x: x["score"], reverse=True) # スコアで着順を決定

            results = []
            for idx, entry in enumerate(field):
                pos = idx + 1
                hid = entry["id"]
                owner = entry["owner"]
                score = entry["score"]
                hname = entry["name"]
                
                prize = 0
                if idx < len(ratios):
                    prize = int(total * ratios[idx])
                
                if owner != BOT_OWNER_ID:
                    # プレイヤー馬の処理
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
                        
                        # 履歴に year, month, day を保存
                        h["history"].append({
                            "year": current_year,
                            "month": current_month,
                            "day": current_day,
                            "race": race_info["name"],
                            "pos": pos,
                            "score": round(score, 2),
                            "prize": prize
                        })
                else:
                    # Bot馬の処理（疲労と成長のみ）
                    h = data["horses"].get(hid)
                    if h:
                        h["fatigue"] = min(10, h.get("fatigue", 0) + random.randint(2, 4))
                        progress_growth(h)


                results.append({
                    "pos": pos, 
                    "horse_id": hid, 
                    "horse_name": hname,
                    "owner": owner, 
                    "score": round(score, 2), 
                    "prize": prize,
                    "post_position": entry["post_position"] # 割り振った馬番を使用
                })

            # レース記録に year, month, day を保存
            data["races"].append({
                "year": current_year,
                "month": current_month,
                "day": current_day,
                "name": race_info["name"],
                "distance": race_info["distance"],
                "track": race_info["track"],
                "results": results
            })

            # エントリーリストをクリア
            data.get("pending_entries", {}).pop(current_day_str, None)

            if channel:
                # 告知関数に day, month, year を渡す
                await announce_race_results(data, race_info, results, current_day, current_month, current_year, channel, total_entries_count)
            
            g1_held = True

        elif race_info and total_entries_count < 2:
            if channel:
                await channel.send(f"⚠️ {current_year}年{current_month}月 第{current_day}週 のGⅠ「{race_info['name']}」はプレイヤー馬とBot馬を合わせても2頭未満のため開催されませんでした。")
    
    # ------------------ 2. 下級レースの実行 ------------------
    
    entered_player_horses_id = set(g1_entries) 
    all_player_horses_id = set([hid for hid, h in data["horses"].items() if h["owner"] != BOT_OWNER_ID]) 
    
    # GⅠにエントリーしなかったプレイヤー馬
    horses_not_entered = list(all_player_horses_id - entered_player_horses_id)
    
    # 下級レース実行関数に day, month, year を渡す
    await run_lower_race_logic(data, horses_not_entered, current_day, current_month, current_year, channel)

    # ------------------ 3. 日の進行 ------------------

    if not is_forced:
        data["season"]["day"] += 1
        
        current_year = data["season"]["year"]
        current_month = data["season"]["month"]
        
        # 該当年月の最大日数を取得 (閏年対応)
        try:
             max_days = calendar.monthrange(current_year, current_month)[1]
        except ValueError:
             # 月のデータがおかしい場合（例: 0や13）、現在の月で強制的に28日で進行させる（初期化ミスの可能性）
             max_days = 28
        
        # 日の進行と月/年のリセットロジック
        if data["season"]["day"] > max_days:
            data["season"]["day"] = 1
            data["season"]["month"] += 1
            if data["season"]["month"] > 12:
                data["season"]["month"] = 1
                data["season"]["year"] += 1

    await save_data(data)
    return g1_held, race_info, total_entries_count

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

    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]
    
    race_info = data["schedule"].get(str(current_day))
    entries = data.get("pending_entries", {}).get(str(current_day), [])
    
    # GⅠが開催される日（1日〜30日）のみ告知
    if race_info and current_day <= MAX_G1_DAY:
        # 告知メッセージにも MIN_G1_FIELD の値（18）を反映
        await channel.send(
            f"🔔 **【出走締切間近のお知らせ】** 🔔\n"
            f"現在のシーズン: {current_year}年 {current_month}月 第{current_day}週\n"
            f"本日19:00 (JST) 開催のGⅠ「**{race_info['name']}**」の出走登録は間もなく締め切られます！\n"
            f"現在のプレイヤーエントリー数: **{len(entries)}**頭 ({MIN_G1_FIELD}頭に満たない場合はBot馬が補充されます)\n"
            f"出走登録は `!entry <ID>` コマンドで！"
        )
    elif current_day == 31:
        await channel.send(
             f"🔔 **【下級レース開催のお知らせ】** 🔔\n"
             f"現在のシーズン: {current_year}年 {current_month}月 第{current_day}週\n"
             f"本日({current_day}日)はGⅠ開催はありませんが、下級レースが開催されます。\n"
             f"GⅠにエントリーしていない馬は自動的に出走します。"
        )

        
@daily_pre_announcement_task.before_loop
async def before_daily_pre_announcement_task():
    await bot.wait_until_ready()

# --------------- 管理系 ---------------

@bot.command(name="forcerace", help="[管理] 本日のレースを即時開催します（日は進めない）")
@commands.has_permissions(administrator=True)
async def forcerace(ctx):
    data = await load_data()
    
    await ctx.reply("本日のレース開催を試みます（日は進行しません）。")
    
    race_held, race_info, total_entries_count = await run_race_logic(data, is_forced=True)
    
    if race_held:
        await ctx.send("GⅠおよび下級レースの処理が完了しました。結果は告知チャンネルをご確認ください。")
    elif race_info is None and data["season"]["day"] <= MAX_G1_DAY:
        await ctx.send("GⅠエントリー馬が2頭未満でした。下級レースの結果と合わせて告知チャンネルをご確認ください。")
    else:
        # Day 31 or other non-GⅠ day
        await ctx.send("本日はGⅠが予定されていませんでした。下級レースの結果と合わせて告知チャンネルをご確認ください。")


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
