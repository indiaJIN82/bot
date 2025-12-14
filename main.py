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

# --------------- Keep Alive Setup (省略) ---------------
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
MIN_G1_FIELD = 10 
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

    res = supabase.table("kv_store").select("value").eq("key", DATA_KEY).execute()

    if not res.data:
        supabase.table("kv_store").insert({
            "key": DATA_KEY,
            "value": default_data
        }).execute()
        return default_data

    data = res.data[0]["value"]

    if "pending_entries" not in data:
        data["pending_entries"] = {}
    if "announce_channel" not in data:
        data["announce_channel"] = None

    return data


async def save_data(data):
    supabase.table("kv_store").upsert({
        "key": DATA_KEY,
        "value": data
    }).execute()


def default_schedule():
    # ... [default_schedule の内容は省略] ...
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
# ... [new_horse_id, new_bot_horse_id, generate_bot_horse, calc_race_score, prize_pool_for_g1, prize_pool_for_lower, progress_growth, generate_commentary, announce_race_results の内容は省略] ...

def _clean_pending_entry(data, horse_id):
    """
    指定された馬IDを、すべてのpending_entriesリストから削除します。
    """
    cleaned = False
    if "pending_entries" in data:
        for day_key in list(data["pending_entries"].keys()):
            if horse_id in data["pending_entries"][day_key]:
                data["pending_entries"][day_key].remove(horse_id)
                cleaned = True
            if not data["pending_entries"][day_key]:
                del data["pending_entries"][day_key]
    return cleaned

# 【新規追加】一括エントリー処理のコアロジック
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
            
    # 3. 上限チェック (ユーザー要求: 5頭以上当てはまる場合は拒否)
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

# ... [resetdata, confirmreset, setannounce, newhorse, retire, massretire の内容は省略] ...

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
    
    for hid in owner["horses"]:
        horse = data["horses"].get(hid)
        if horse and not horse.get("favorite", False):
            to_retire.append(hid)
        elif horse:
            to_keep.append(hid)

    if not to_retire:
        await ctx.reply("お気に入り登録されている馬しかいません。削除対象の馬がいません。")
        return
        
    for hid in to_retire:
        _clean_pending_entry(data, hid) 
        if hid in data["horses"]:
             del data["horses"][hid]
    
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
        reply_msg.append("なし")
        
    await ctx.reply("\n".join(reply_msg))

# ... [favorite, unfavorite, myhorses の内容は省略] ...

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

# ... [entries, rest, balance, rank, schedule, season, racehistory, run_lower_race_logic, run_race_logic, daily_race_task, daily_pre_announcement_task, forcerace の内容は省略] ...

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
