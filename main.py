from table2ascii import table2ascii as t2a, PresetStyle
import os
import json
import random
import asyncio
import calendar
import threading
from datetime import datetime, timezone, timedelta, time 
from flask import Flask 
from threading import Thread
import discord
from discord.ext import commands, tasks
from supabase import create_client

def cut_horse_name(name: str, max_width: float = 10.0) -> str:
    """
    馬名を 10 文字相当までに制限する関数（英字/数字は 0.8 文字換算）
    """
    width = 0.0
    result = []

    for ch in name:
        # 半角英字・数字は 0.8
        if ch.isascii() and ch.isalnum():
            w = 0.8
        else:
            w = 1.0

        # 制限を超えると終了
        if width + w > max_width:
            break

        result.append(ch)
        width += w

    return "".join(result)

# ---------------- Flask (Render Health Check 用) ----------------

app = Flask(__name__)

@app.route("/")
def health():
    return "ok", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --------------- 基本設定 ---------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

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

# ------------------ 新しい定数 ------------------
# 成長力（GRW）からステータスへの変換率 (1 GRW = 1 Stat)
GRW_CONVERSION_RATE = 1 
# 一度に消費できる最大GRW量
MAX_TRAIN_AMOUNT = 5 
# ------------------------------------------------

# --------------- ユーティリティ ---------------

# 最大保有頭数 
MAX_HORSES_PER_OWNER = 5
# GⅠの最低出走頭数（これに満たない場合Bot馬を補充）
MIN_G1_FIELD = 18 
# GⅠが開催される最大の日数（週数）
MAX_G1_DAY = 30 
# 馬券のハウスマージン（控除率）
HOUSE_MARGIN = 0.9 # 10%控除

async def load_data():
    default_data = {
        "horses": {},
        "owners": {},
        "races": [],
        "schedule": default_schedule(),
        "rankings": {"prize": {}, "wins": {}, "stable": {}},
        "announce_channel": None,
        "pending_entries": {},
        "pending_bets": {} # ★ 追加
    }

    today = datetime.now(JST)
    default_data["season"] = {
        "year": today.year,
        "month": today.month,
        "day": today.day,
        "last_race_day": 0 # 最終レース実行日を記録
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
    if "season" not in data:
        data["season"] = default_data["season"]
    if "pending_bets" not in data: # ★ 追加
        data["pending_bets"] = {}
        
    return data


async def save_data(data):
    # Supabaseにデータを保存（upsertで更新）
    supabase.table("kv_store").upsert({
        "key": DATA_KEY,
        "value": data
    }).execute()


def default_schedule():
    """レーススケジュール定義（地方・海外GⅠを組み込み、現実のローテーションに近づける）"""
    # 30個のGⅠを、シーズンの1日から30日に対応させる
    return {
        # --------------------- 年末年始（ダート・海外） ---------------------
        "1":  {"name": "GⅠ 東京大賞典", "distance": 2000, "track": "ダート"},
        "2":  {"name": "GⅠ 川崎記念", "distance": 2100, "track": "ダート"}, 
        "3":  {"name": "GⅠ サウジカップ", "distance": 1800, "track": "ダート"},
        "4":  {"name": "GⅠ ドバイWC", "distance": 2000, "track": "ダート"},
        
        # --------------------- 春のクラシック・短距離 ---------------------
        "5":  {"name": "GⅠ 高松宮記念", "distance": 1200, "track": "芝"},
        "6":  {"name": "GⅠ 桜花賞", "distance": 1600, "track": "芝"},
        "7":  {"name": "GⅠ 皐月賞", "distance": 2000, "track": "芝"},
        "8":  {"name": "GⅠ 天皇賞（春）", "distance": 3200, "track": "芝"},
        "9":  {"name": "GⅠ NHKマイルC", "distance": 1600, "track": "芝"},
        "10": {"name": "GⅠ 日本ダービー", "distance": 2400, "track": "芝"},
        "11": {"name": "GⅠ 安田記念", "distance": 1600, "track": "芝"},
        
        # --------------------- 初夏・夏（交流・欧州） ---------------------
        "12": {"name": "GⅠ 帝王賞", "distance": 2000, "track": "ダート"},
        "13": {"name": "GⅠ 宝塚記念", "distance": 2200, "track": "芝"},
        "14": {"name": "GⅠ キングジョージ6世&クイーンエリザベスS", "distance": 2400, "track": "芝"},
        
        # --------------------- 秋のGⅠシーズン ---------------------
        "15": {"name": "GⅠ スプリンターズS", "distance": 1200, "track": "芝"},
        "16": {"name": "GⅠ 凱旋門賞", "distance": 2400, "track": "芝"},
        "17": {"name": "GⅠ 秋華賞", "distance": 2000, "track": "芝"},
        "18": {"name": "GⅠ ジ・エベレスト", "distance": 1200, "track": "芝"},
        "19": {"name": "GⅠ 菊花賞", "distance": 3000, "track": "芝"},
        "20": {"name": "GⅠ 天皇賞（秋）", "distance": 2000, "track": "芝"},
        "21": {"name": "GⅠ エリザベス女王杯", "distance": 2200, "track": "芝"},
        "22": {"name": "GⅠ マイルCS", "distance": 1600, "track": "芝"},
        "23": {"name": "GⅠ ジャパンカップ", "distance": 2400, "track": "芝"},
        "24": {"name": "GⅠ チャンピオンズC", "distance": 1800, "track": "ダート"},
        "25": {"name": "GⅠ チャンピオンズマイル", "distance": 1600, "track": "芝"},
        "26": {"name": "GⅠ BCクラシック", "distance": 2000, "track": "ダート"},
        "27": {"name": "GⅠ 阪神JF", "distance": 1600, "track": "芝"},
        "28": {"name": "GⅠ 朝日杯FS", "distance": 1600, "track": "芝"},
        "29": {"name": "GⅠ ホープフルS", "distance": 2000, "track": "芝"},
        "30": {"name": "GⅠ 有馬記念", "distance": 2500, "track": "芝"},
    }

def new_horse_id(data):
    """プレイヤー馬のID生成"""
    base = "H" + str(random.randint(10000, 99999))
    while base in data["horses"]:
        base = "H" + str(random.randint(10000, 99999))
    return base

def new_bot_horse_id(data):
    """Bot馬のID生成"""
    base = "B" + str(random.randint(1000, 9999))
    while base in data["horses"]:
        base = "B" + str(random.randint(1000, 9999))
    return base

def prize_pool_for_g1(race_name):
    """GⅠレース名に基づき、賞金プールを決定する"""
    
    # 高額賞金レース (500,000)
    if "サウジカップ" in race_name or "ドバイWC" in race_name or "ジ・エベレスト" in race_name or "有馬記念" in race_name or "ジャパン" in race_name:
        total = 500_000
        
    # 海外主要レース (300,000)
    elif "凱旋門賞" in race_name or "キングジョージ6世" in race_name or "BCクラシック" in race_name or "チャンピオンズマイル" in race_name:
        total = 300_000
        
    # 日本の中央GⅠおよびその他のレース（地方交流含む） (200,000)
    else:
        total = 200_000 
        
    # GⅠの配分率は変わらず、5着まで
    payout_rate = [0.55, 0.2, 0.12, 0.08, 0.05]
    
    return total, payout_rate

def prize_pool_for_lower():
    """下級レースの賞金プールを決定する"""
    total = 30_000
    payout_rate = [0.6, 0.25, 0.1, 0.05] # 4着まで
    return total, payout_rate

def _clean_pending_entry(data, horse_id):
    """pending_entriesから特定の馬IDをクリーンアップ"""
    g1_day_key = str(data["season"]["day"])
    if g1_day_key in data["pending_entries"]:
        data["pending_entries"][g1_day_key]["entries"] = [
            h for h in data["pending_entries"][g1_day_key].get("entries", []) if h != horse_id
        ]
        
# ------------------ 新しいオッズ計算ロジック ------------------

def calculate_expected_score(horse, race_info):
    """馬の最終的なレーススコアを計算する（乱数なし）"""
    stats = horse["stats"]
    
    # 距離適性ボーナス/ペナルティ
    dist_bonus = 0
    distance = race_info["distance"]
    if distance <= 1400: # 短距離
        dist_bonus = (stats["SPD"] * 1.5) + (stats["POW"] * 1.0)
    elif distance <= 2000: # マイル・中距離
        dist_bonus = (stats["SPD"] * 1.0) + (stats["STAM"] * 1.0) + (stats["POW"] * 1.0)
    else: # 長距離
        dist_bonus = (stats["SPD"] * 0.8) + (stats["STAM"] * 1.5) + (stats["POW"] * 0.7)
        
    # 馬場適性ボーナス/ペナルティ
    track_bonus = 0
    if race_info["track"] == "芝":
        track_bonus = stats["SPD"] * 0.2
    else: # ダート
        track_bonus = stats["POW"] * 0.2
        
    # 疲労ペナルティ
    fatigue_penalty = horse.get("fatigue", 0) * 10
    
    # スコア計算
    base_score = dist_bonus + track_bonus
    
    # 最終スコア = (基本スコア - 疲労ペナルティ)
    final_score = base_score - fatigue_penalty
    
    return max(1, final_score) # 0以下にならないように最低1にする


def calculate_fixed_odds(data_source, entries_list, race_info, house_margin=HOUSE_MARGIN):
    """
    出走馬リストとレース情報に基づき、固定オッズを計算する。
    オッズは (1 / 確率) * (1 / ハウスマージン) で計算される。
    """
    total_expected_score = 0
    horse_scores = {}
    
    # 全馬の期待スコアを計算
    for hid in entries_list:
        horse = data_source.get(hid)
        if horse:
            score = calculate_expected_score(horse, race_info)
            horse_scores[hid] = score
            total_expected_score += score
    
    # オッズを計算
    fixed_odds = {}
    for hid, score in horse_scores.items():
        if total_expected_score > 0:
            # 勝利確率: score / total_score
            # 控除後オッズ: 1 / (確率 * house_margin)
            
            odds = 1.0 / (score / total_expected_score * house_margin)
            fixed_odds[hid] = max(1.1, odds) # オッズは最低1.1倍
        else:
            fixed_odds[hid] = 999.0
            
    return fixed_odds
# ------------------------------------------------------------------

# --------------- コマンド定義 ---------------

@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    # 定期実行タスクの開始
    check_time.start()
    await bot.change_presence(activity=discord.Game(name="!help | 競馬ライフ"))


@bot.command(name="register", help="競走馬を登録します。!register [馬名]")
async def register_horse(ctx, *, name: str):
    data = await load_data()
    owner_id = str(ctx.author.id)
    
    # オーナーの馬頭数チェック
    owner_horses = data["owners"].get(owner_id, {}).get("horses", [])
    if len(owner_horses) >= MAX_HORSES_PER_OWNER:
        return await ctx.reply(f"馬の最大保有頭数（{MAX_HORSES_PER_OWNER}頭）に達しています。")

    # 馬名重複チェック
    if any(h.get("name") == name for h in data["horses"].values()):
        return await ctx.reply("その馬名は既に登録されています。別の名前を付けてください。")

    # 初期ステータス生成
    initial_stats = {
        "SPD": random.randint(30, 60),
        "STAM": random.randint(30, 60),
        "POW": random.randint(30, 60),
        "GRW": 20, # 初期成長力
        "fatigue": 0,
        "age": 2 # 初期年齢
    }

    horse_id = new_horse_id(data)
    
    data["horses"][horse_id] = {
        "id": horse_id,
        "name": name,
        "owner": owner_id,
        "stats": initial_stats,
        "money": 0,
        "history": [],
        "age": 2
    }

    # オーナー情報を更新 (balanceを追加)
    if owner_id not in data["owners"]:
        data["owners"][owner_id] = {"horses": [], "name": ctx.author.display_name, "balance": 100000} # ★ 初期残高 100,000円
    
    data["owners"][owner_id]["horses"].append(horse_id)
    data["owners"][owner_id]["name"] = ctx.author.display_name

    await save_data(data)
    await ctx.reply(f"🐴 **{name}** が競走馬として登録されました！\n"
                    f"初期ステータス: SPD:{initial_stats['SPD']}, STAM:{initial_stats['STAM']}, POW:{initial_stats['POW']}\n"
                    f"オーナー残高: ¥{data['owners'][owner_id]['balance']:,}\n"
                    f"レースに出走したり、`!train`で成長させましょう。")


@bot.command(name="list", help="あなたの所有馬一覧を表示します")
async def list_horses(ctx):
    data = await load_data()
    owner_id = str(ctx.author.id)
    
    owner_horses_ids = data["owners"].get(owner_id, {}).get("horses", [])
    balance = data["owners"].get(owner_id, {}).get("balance", 0)

    if not owner_horses_ids:
        return await ctx.reply("所有馬がいません。`!register [馬名]`で登録しましょう。")

    response = [f"**{ctx.author.display_name}厩舎の所有馬（{len(owner_horses_ids)}頭）**",
                f"💰 **残高**: ¥{balance:,}"]
    
    for hid in owner_horses_ids:
        horse = data["horses"].get(hid)
        if horse:
            stats = horse["stats"]
            money = horse["money"]
            wins = sum(1 for history in horse.get("history", []) if history["rank"] == 1)
            
            response.append(
                f"**{horse['name']} (ID: {hid})** - {horse['age']}歳 | {wins}勝 | 疲労:{stats['fatigue']}\n"
                f"  獲得賞金: ¥{money:,} | GRW: {stats['GRW']}\n"
                f"  SPD:{stats['SPD']} STAM:{stats['STAM']} POW:{stats['POW']}"
            )
    
    await ctx.reply("\n".join(response))


@bot.command(name="train", help="GRWを消費してステータスを強化します。!train [馬ID] [ステータス] [量]")
async def train(ctx, horse_id: str, stat_name: str, amount: int):
    data = await load_data()
    owner_id = str(ctx.author.id)
    
    horse = data["horses"].get(horse_id)
    
    if not horse or horse["owner"] != owner_id:
        return await ctx.reply("その馬IDの馬は存在しないか、あなたが所有していません。")
        
    stat_name = stat_name.upper()
    
    if stat_name not in ["SPD", "STAM", "POW"]:
        return await ctx.reply("ステータス名は 'SPD', 'STAM', 'POW' のいずれかを指定してください。")
        
    if not 1 <= amount <= MAX_TRAIN_AMOUNT:
        return await ctx.reply(f"強化量は1から{MAX_TRAIN_AMOUNT}の間で指定してください。")
        
    grw_cost = amount
    
    if horse["stats"]["GRW"] < grw_cost:
        return await ctx.reply(f"GRW（成長力）が不足しています。現在のGRW: {horse['stats']['GRW']}。")
        
    # 強化処理
    horse["stats"]["GRW"] -= grw_cost
    horse["stats"][stat_name] += amount * GRW_CONVERSION_RATE
    horse["stats"]["fatigue"] += 1 # 疲労増加
    
    await save_data(data)
    await ctx.reply(f"🐎 **{horse['name']}** の {stat_name} を +{amount} 強化しました。\n"
                    f"  現在のステータス: {stat_name}:{horse['stats'][stat_name]} | GRW:{horse['stats']['GRW']} | 疲労:{horse['stats']['fatigue']}")


@bot.command(name="rest", help="馬を休ませて疲労を回復させます。!rest [馬ID] [量 (最大5)]")
async def rest_horse(ctx, horse_id: str, amount: int):
    data = await load_data()
    owner_id = str(ctx.author.id)
    
    horse = data["horses"].get(horse_id)
    
    if not horse or horse["owner"] != owner_id:
        return await ctx.reply("その馬IDの馬は存在しないか、あなたが所有していません。")
        
    if not 1 <= amount <= 5:
        return await ctx.reply("回復量は1から5の間で指定してください。")

    # 疲労回復処理
    current_fatigue = horse["stats"].get("fatigue", 0)
    new_fatigue = max(0, current_fatigue - amount)
    
    recovery_amount = current_fatigue - new_fatigue
    
    if recovery_amount == 0:
        return await ctx.reply(f"**{horse['name']}** は疲労がありません（現在の疲労: 0）。")

    horse["stats"]["fatigue"] = new_fatigue
    
    await save_data(data)
    await ctx.reply(f"🛌 **{horse['name']}** を {recovery_amount} 回復させました。\n"
                    f"  現在の疲労: {horse['stats']['fatigue']}")


@bot.command(name="g1", help="今日のGⅠレース情報を表示します")
async def g1_info(ctx):
    data = await load_data()
    day_key = str(data["season"]["day"])
    race_info = data["schedule"].get(day_key)
    
    if not race_info:
        return await ctx.reply(f"本日（第{day_key}週）はGⅠレースの開催予定はありません。（下級レースが開催されます）")

    total_prize, _ = prize_pool_for_g1(race_info['name'])
    
    entries_count = len(data["pending_entries"].get(day_key, {}).get("entries", []))
    
    response = (f"🏆 **本日開催 GⅠレース情報**\n"
                f"  **レース名**: {race_info['name']}\n"
                f"  **距離/馬場**: {race_info['distance']}m / {race_info['track']}\n"
                f"  **賞金総額**: ¥{total_prize:,}（1着: ¥{int(total_prize * 0.55):,}）\n"
                f"  **現在登録頭数**: {entries_count}頭\n"
                f"  `!entry [馬ID]` で出走登録できます。 | `!bet [馬ID] [金額]` で馬券を購入できます。")
    
    await ctx.reply(response)


@bot.command(name="entry", help="今日のGⅠレースに出走登録します。!entry [馬ID]")
async def entry_g1(ctx, horse_id: str):
    data = await load_data()
    owner_id = str(ctx.author.id)
    day_key = str(data["season"]["day"])
    race_info = data["schedule"].get(day_key)
    
    if not race_info:
        return await ctx.reply("本日GⅠレースは開催されません。`!g1`で確認してください。")
        
    horse = data["horses"].get(horse_id)
    if not horse or horse["owner"] != owner_id:
        return await ctx.reply("その馬IDの馬は存在しないか、あなたが所有していません。")
        
    entries_key = day_key
    
    # 登録リスト初期化
    if entries_key not in data["pending_entries"]:
        data["pending_entries"][entries_key] = {"race": race_info, "entries": []}
    
    # 既に登録済みかチェック
    if horse_id in data["pending_entries"][entries_key]["entries"]:
        return await ctx.reply(f"**{horse['name']}** は既に **{race_info['name']}** に登録されています。")

    # 疲労チェック
    if horse["stats"].get("fatigue", 0) > 4:
        return await ctx.reply(f"**{horse['name']}** は疲労度が高すぎます（疲労:{horse['stats']['fatigue']}）。GⅠレースへの登録は疲労5以上ではできません。`!rest`で回復させてください。")

    # 登録処理
    data["pending_entries"][entries_key]["entries"].append(horse_id)
    
    await save_data(data)
    await ctx.reply(f"✅ **{horse['name']}** が **{race_info['name']}** に出走登録されました！\n"
                    f"オッズを確認して `!bet [馬ID] [金額]` で馬券を購入しましょう。")


@bot.command(name="unentry", help="今日のGⅠレースの出走登録を取り消します。!unentry [馬ID]")
async def unentry_g1(ctx, horse_id: str):
    data = await load_data()
    owner_id = str(ctx.author.id)
    day_key = str(data["season"]["day"])
    race_info = data["schedule"].get(day_key)
    
    if not race_info:
        return await ctx.reply("本日GⅠレースは開催されません。")
        
    horse = data["horses"].get(horse_id)
    if not horse or horse["owner"] != owner_id:
        return await ctx.reply("その馬IDの馬は存在しないか、あなたが所有していません。")
        
    entries_key = day_key
    
    # 登録リストから削除
    if entries_key in data["pending_entries"] and horse_id in data["pending_entries"][entries_key].get("entries", []):
        data["pending_entries"][entries_key]["entries"].remove(horse_id)
        await save_data(data)
        return await ctx.reply(f"❌ **{horse['name']}** の **{race_info['name']}** への出走登録を取り消しました。")

    return await ctx.reply(f"**{horse['name']}** は今日の **{race_info['name']}** に登録されていません。")


@bot.command(name="schedule", help="本日と翌日のGⅠレーススケジュールを表示します")
async def schedule(ctx):
    data = await load_data()
    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]
    
    header = [
        f"📅 **GⅠレーススケジュール** ({current_year}年{current_month}月)",
        f"現在のシーズン日: **第{current_day}週/30週**",
        "---"
    ]
    
    schedule_lines = []
    
    # 本日と翌日（2日分）のみをチェック
    days_to_check = [current_day, current_day + 1]
    
    for day in days_to_check:
        day_key = str(day)
        race_info = data["schedule"].get(day_key)
        
        if day > MAX_G1_DAY:
             # シーズン終了後の処理
             schedule_lines.append(f"**第{day}週**: シーズン終了のためGⅠ開催はありません。")
             break
        
        if race_info:
            status = "本日開催" if day == current_day else "明日開催予定"
            total_prize, _ = prize_pool_for_g1(race_info['name'])
            schedule_lines.append(
                f"**第{day}週**: {race_info['name']} ({race_info['distance']}m/{race_info['track']}) - **{status}** (賞金総額: ¥{total_prize:,})"
            )
        elif day == current_day:
            schedule_lines.append(f"**第{day}週 (本日)**: GⅠ開催はありません。（定刻に下級レースを実行します）")
        elif day == current_day + 1:
            schedule_lines.append(f"**第{day}週 (明日)**: GⅠ開催はありません。（定刻に下級レースを実行します）")


    if not schedule_lines and current_day > MAX_G1_DAY:
        header.append(f"✅ 第{MAX_G1_DAY}週までのGⅠレースは全て終了しました。")
    
    await ctx.reply("\n".join(header + schedule_lines))


@bot.command(name="entries", help="今日のGⅠレースの出馬表を表示します")
async def show_entries(ctx):
    data = await load_data()
    day_key = str(data["season"]["day"])
    race_info = data["schedule"].get(day_key)

    if not race_info:
        return await ctx.reply("本日GⅠレースは開催されません。")
        
    entries_list = data["pending_entries"].get(day_key, {}).get("entries", [])
    
    if not entries_list:
        return await ctx.reply(f"現在、**{race_info['name']}** に登録されている馬はいません。")
    
    # 出走馬データの準備
    field = []
    for hid in entries_list:
        horse = data["horses"].get(hid)
        if horse:
            stats = horse["stats"]
            wins = sum(1 for h in horse.get("history", []) if h["rank"] == 1)
            field.append({
                "name": horse["name"],
                "owner_name": data["owners"].get(horse["owner"], {}).get("name", "Unknown Owner"),
                "age": horse["age"],
                "wins": wins,
                "spd": stats["SPD"],
                "stam": stats["STAM"],
                "pow": stats["POW"],
                "fatigue": stats["fatigue"]
            })

    # スコアに基づきソート (ここでは単純に総合力+ランダムでソート)
    random.shuffle(field)

    # Markdownテーブルの作成
    table_data = []
    
    # ヘッダー
    table_data.append(["馬名", "オーナー", "齢", "勝", "SPD", "STAM", "POW", "疲労"])

    for i, horse in enumerate(field):
        table_data.append([
            cut_horse_name(horse["name"]),
            cut_horse_name(horse["owner_name"]),
            str(horse["age"]),
            str(horse["wins"]),
            str(horse["spd"]),
            str(horse["stam"]),
            str(horse["pow"]),
            str(horse["fatigue"])
        ])

    # テーブル表示
    table_string = t2a(
        header=table_data[0],
        body=table_data[1:],
        style=PresetStyle.thin_compact
    )

    response = (f"📋 **{race_info['name']}** 出馬表 ({race_info['distance']}m/{race_info['track']})\n"
                f"現在登録頭数: {len(field)}頭\n"
                f"```\n{table_string}\n```")
    
    await ctx.reply(response)


@bot.command(name="bet", help="本日のGⅠの単勝（1着）に賭けます: 例) !bet H12345 100")
async def bet(ctx, horse_id: str, amount: int):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)
    current_day_str = str(data["season"]["day"])

    # 1. GⅠ開催日チェック
    race_info = data["schedule"].get(current_day_str)
    if not race_info:
        await ctx.reply(f"本日(第{data['season']['day']}週)はGⅠ開催日ではないため、馬券は購入できません。")
        return
    
    # 2. 馬の存在とエントリーチェック 
    if not horse or horse["owner"] == BOT_OWNER_ID:
        await ctx.reply("そのIDの馬は存在しないか、Bot馬には賭けられません。")
        return
    
    entries_list = data.get("pending_entries", {}).get(current_day_str, {}).get("entries", [])
    if horse_id not in entries_list:
        await ctx.reply(f"**{horse['name']}** は本日のGⅠ「{race_info['name']}」にエントリーされていません。")
        return
        
    # 3. オッズ計算用のフィールドを準備
    
    full_field_for_odds = []
    player_horses_for_odds = []
    for hid in entries_list:
        h = data["horses"].get(hid)
        if h and h["owner"] != BOT_OWNER_ID:
            player_horses_for_odds.append(h)
            full_field_for_odds.append(hid)
            
    # オッズ計算用の仮Bot馬を生成 (DBには保存しない)
    temp_bot_horses = {}
    num_bot_horses_needed = max(0, MIN_G1_FIELD - len(player_horses_for_odds))
    min_s, max_s = (70, 110) # GⅠ設定
    for i in range(num_bot_horses_needed):
        bot_hid = f"TEMP_B_{i}" 
        temp_bot_horses[bot_hid] = {
            "stats": {
                "SPD": random.randint(min_s, max_s),
                "STAM": random.randint(min_s, max_s),
                "POW": random.randint(min_s, max_s),
                "fatigue": 0
            },
            "fatigue": 0
        }
        full_field_for_odds.append(bot_hid)
        
    # オッズ計算用のデータ辞書を準備
    odds_data_source = data["horses"].copy()
    odds_data_source.update(temp_bot_horses)
    
    # 4. オッズ計算
    fixed_odds_map = calculate_fixed_odds(odds_data_source, full_field_for_odds, race_info)
    current_odds = fixed_odds_map.get(horse_id)

    if current_odds is None:
        await ctx.reply("オッズ計算中にエラーが発生しました（馬がエントリーリストに見つかりません）。")
        return

    # 5. 金額チェック
    if amount <= 0:
        await ctx.reply("賭け金は正の値を指定してください。")
        return
    
    # 6. オーナーの残高チェックと馬券の購入
    # オーナー情報が存在しない場合も初期化
    if uid not in data["owners"]:
        data["owners"][uid] = {"horses": [], "name": ctx.author.display_name, "balance": 0} 
    
    owner = data["owners"][uid]
    
    if owner.get("balance", 0) < amount:
        await ctx.reply(f"残高が不足しています。現在の残高: {owner.get('balance', 0):,}円。")
        return
    
    # 資金の引き落とし
    data["owners"][uid]["balance"] -= amount

    # 賭けの登録 (オッズを記録)
    bets = data.get("pending_bets", {})
    if current_day_str not in bets:
        bets[current_day_str] = {}
        
    if horse_id not in bets[current_day_str]:
        bets[current_day_str][horse_id] = []

    bets[current_day_str][horse_id].append({
        "user_id": uid, 
        "amount": amount,
        "odds": current_odds # ★ 確定オッズを記録
    })
    data["pending_bets"] = bets
    
    await save_data(data)
    await ctx.reply(f"✅ **{horse['name']}** の単勝に **{amount:,}円** 賭けました。\n"
                    f"  **確定オッズ**: {current_odds:.1f}倍\n"
                    f"  残高: {data['owners'][uid]['balance']:,}円")


@bot.command(name="odds", help="本日のGⅠレースの現在の想定固定単勝オッズを表示します")
async def odds(ctx):
    data = await load_data()
    current_day_str = str(data["season"]["day"])
    
    race_info = data["schedule"].get(current_day_str)
    if not race_info:
        return await ctx.reply(f"本日(第{data['season']['day']}週)はGⅠ開催日ではないため、オッズはありません。")
        
    entries_list = data.get("pending_entries", {}).get(current_day_str, {}).get("entries", [])

    if not entries_list:
        return await ctx.reply(f"GⅠ「**{race_info['name']}**」にはまだエントリー馬がいません。")

    # オッズ計算用のフィールドを準備 (Bot馬の仮生成は !bet のロジックと同じ)
    full_field_for_odds = []
    player_horses_for_odds = []
    for hid in entries_list:
        h = data["horses"].get(hid)
        if h and h["owner"] != BOT_OWNER_ID:
            player_horses_for_odds.append(h)
            full_field_for_odds.append(hid)
            
    # オッズ計算用の仮Bot馬を生成 (DBには保存しない)
    temp_bot_horses = {}
    num_bot_horses_needed = max(0, MIN_G1_FIELD - len(player_horses_for_odds))
    min_s, max_s = (70, 110) # GⅠ設定
    for i in range(num_bot_horses_needed):
        bot_hid = f"TEMP_B_{i}" 
        temp_bot_horses[bot_hid] = {
            "stats": {
                "SPD": random.randint(min_s, max_s),
                "STAM": random.randint(min_s, max_s),
                "POW": random.randint(min_s, max_s),
                "fatigue": 0
            },
            "fatigue": 0
        }
        full_field_for_odds.append(bot_hid)
        
    # オッズ計算用のデータ辞書を準備
    odds_data_source = data["horses"].copy()
    odds_data_source.update(temp_bot_horses)
    
    # 3. オッズ計算
    fixed_odds_map = calculate_fixed_odds(odds_data_source, full_field_for_odds, race_info)

    # 4. 表示用のデータ整形
    odds_data = []
    for hid in entries_list:
        horse = data["horses"].get(hid)
        if horse and horse["owner"] != BOT_OWNER_ID:
            odds_display = f"{fixed_odds_map.get(hid, 999.0):.1f}"
            odds_data.append([
                cut_horse_name(horse["name"]),
                odds_display
            ])

    # オッズの低い順（人気順）にソート
    odds_data.sort(key=lambda x: float(x[1]))
    
    # テーブル表示
    ascii_table = t2a(
        header=["馬名", "オッズ"],
        body=odds_data,
        style=PresetStyle.thin_compact
    )
    
    header_text = (
        f"📊 **GⅠ「{race_info['name']}」想定固定単勝オッズ**\n"
        "オッズは馬の能力に基づき、随時更新されます。購入時のオッズが適用されます。\n"
        "---"
    )

    await ctx.reply(header_text + "```" + ascii_table + "```")

# ------------------ レース実行ロジック ------------------

def calculate_score(horse, race_info):
    """馬の最終的なレーススコアを計算する（単純化したロジック）"""
    stats = horse["stats"]
    
    # 距離適性ボーナス/ペナルティ
    dist_bonus = 0
    distance = race_info["distance"]
    if distance <= 1400: # 短距離
        dist_bonus = (stats["SPD"] * 1.5) + (stats["POW"] * 1.0)
    elif distance <= 2000: # マイル・中距離
        dist_bonus = (stats["SPD"] * 1.0) + (stats["STAM"] * 1.0) + (stats["POW"] * 1.0)
    else: # 長距離
        dist_bonus = (stats["SPD"] * 0.8) + (stats["STAM"] * 1.5) + (stats["POW"] * 0.7)
        
    # 馬場適性ボーナス/ペナルティ
    track_bonus = 0
    if race_info["track"] == "芝":
        track_bonus = stats["SPD"] * 0.2
    else: # ダート
        track_bonus = stats["POW"] * 0.2
        
    # 疲労ペナルティ
    fatigue_penalty = stats.get("fatigue", 0) * 10
    
    # スコア計算
    base_score = dist_bonus + track_bonus
    
    # 乱数による変動（レースの不確実性）
    random_factor = random.uniform(0.9, 1.1) 
    
    # 最終スコア = (基本スコア - 疲労ペナルティ) * ランダム係数
    final_score = (base_score - fatigue_penalty) * random_factor
    
    return max(0, final_score)


def generate_bot_horse(data, race_info, min_stats=50, max_stats=100):
    """Bot馬を生成する"""
    horse_id = new_bot_horse_id(data)
    name = f"ライバル{random.randint(100, 999)}"
    
    # 距離に応じてステータスを調整
    distance = race_info["distance"]
    
    spd = random.randint(min_stats, max_stats)
    stam = random.randint(min_stats, max_stats)
    pow_ = random.randint(min_stats, max_stats)
    
    # レース適性に合わせて少しブースト
    if distance <= 1400: # 短距離
        spd = min(120, spd + random.randint(0, 10))
    elif distance <= 2000: # 中距離
        stam = min(120, stam + random.randint(0, 10))
    else: # 長距離
        stam = min(120, stam + random.randint(0, 15))

    bot_horse = {
        "id": horse_id,
        "name": name,
        "owner": BOT_OWNER_ID,
        "stats": {
            "SPD": spd,
            "STAM": stam,
            "POW": pow_,
            "GRW": 0,
            "fatigue": 0,
            "age": random.randint(3, 5) # Bot馬はランダムな年齢
        },
        "money": 0,
        "history": [],
        "is_bot": True
    }
    
    data["horses"][horse_id] = bot_horse
    return horse_id, bot_horse


async def run_race_and_advance_day():
    data = await load_data()
    current_day = data["season"]["day"]
    day_key = str(current_day)

    # GⅠレース情報の確認
    race_info = data["schedule"].get(day_key)
    is_g1 = race_info is not None

    entries_list = []
    
    if not is_g1:
        # GⅠのない日は下級レースを実施（固定レース情報）
        race_info = {"name": "下級レース", "distance": random.choice([1200, 1600, 2000, 2400]), "track": random.choice(["芝", "ダート"])}
        entries_list = []
        # 下級レースでは、疲労が少ない全ての馬が自動でエントリーされる（疲労1 *未満*、つまり疲労0のみ）
        for hid, horse in data["horses"].items():
            if horse["owner"] != BOT_OWNER_ID and horse.get("fatigue", 0) < 1: 
                entries_list.append(hid)
        
        # プレイヤー馬がいない場合は何もしない
        if not entries_list:
            await advance_day(data)
            await save_data(data)
            return
            
        # 下級レースは最大18頭（参加プレイヤー馬+Bot馬）
        num_bot_horses = 18 - len(entries_list)
        
    else:
        # GⅠレースの場合
        pending_data = data["pending_entries"].get(day_key)
        if pending_data:
            entries_list = pending_data["entries"]
        
        # 最低出走頭数(MIN_G1_FIELD)までBot馬を補充
        num_bot_horses = max(0, MIN_G1_FIELD - len(entries_list))
        
        # プレイヤー馬が登録されていない場合は、レースは開催しない
        if not entries_list:
            await advance_day(data)
            await save_data(data)
            return

    # Bot馬の生成とエントリー
    for _ in range(num_bot_horses):
        # GⅠレースは強力なBot馬を、下級レースは平均的なBot馬を生成
        min_s, max_s = (70, 110) if is_g1 else (50, 80)
        bot_hid, _ = generate_bot_horse(data, race_info, min_s, max_s)
        entries_list.append(bot_hid)

    # レースのシミュレーション
    field_scores = []
    for hid in entries_list:
        horse = data["horses"][hid]
        score = calculate_score(horse, race_info)
        field_scores.append((score, hid, horse['name'], horse['owner']))

    # スコアでソートし、順位を決定
    field_scores.sort(key=lambda x: x[0], reverse=True)
    
    # 賞金プールを決定
    prize_config = prize_pool_for_g1(race_info['name']) if is_g1 else prize_pool_for_lower()
    total_prize, payout_rate = prize_config

    # 結果の処理と通知メッセージの準備
    results = []
    announcement = [f"📢 **{race_info['name']}** ({race_info['distance']}m/{race_info['track']}) 結果発表！\n"]
    
    # 着順に応じて賞金を付与
    for rank, (score, hid, name, owner_id) in enumerate(field_scores, 1):
        prize = 0
        if rank <= len(payout_rate):
            prize = int(total_prize * payout_rate[rank-1])
            
        # 馬とオーナーのデータを更新
        horse = data["horses"][hid]
        if horse["owner"] != BOT_OWNER_ID:
            horse["money"] += prize
        
        # 疲労とGRWの更新
        if not horse.get("is_bot"):
            horse["stats"]["fatigue"] += 2 # 出走による疲労
            horse["stats"]["GRW"] += random.randint(1, 3) # 出走によるGRW獲得
        
        # 履歴の保存
        horse["history"].append({
            "race": race_info["name"],
            "rank": rank,
            "prize": prize,
            "day": current_day
        })
        
        # ランキングの更新
        if owner_id != BOT_OWNER_ID:
            data["rankings"]["prize"][owner_id] = data["rankings"]["prize"].get(owner_id, 0) + prize
            if rank == 1:
                data["rankings"]["wins"][owner_id] = data["rankings"]["wins"].get(owner_id, 0) + 1
            data["rankings"]["stable"][owner_id] = data["owners"][owner_id]["name"]
        
        results.append((rank, name, owner_id, prize))
        
        # 通知メッセージの作成
        owner_name = data["owners"].get(owner_id, {}).get("name", "BOT")
        if rank <= 5:
            announcement.append(f"  **{rank}着**: {name} ({owner_name}) - ¥{prize:,}")

    announcement.append("\n詳細な結果は`!list`や`!history`で確認できます。")
    
    # --- ★ 配当計算と支払い (固定オッズ方式に変更) ★ ---
    
    bet_pool_key = day_key
    bets_on_race = data.get("pending_bets", {}).get(bet_pool_key, {})
    payout_announcements = []

    if is_g1 and bets_on_race: # GⅠかつ賭けが存在する場合
        
        # 1. 勝ち馬を特定
        winner_entry = field_scores[0]
        winner_id = winner_entry[1]
        winner_name = winner_entry[2]
        
        # 2. 勝ち馬に賭けられたベットを取得
        winning_bets = bets_on_race.get(winner_id, [])
        total_payout = 0
        total_user_stake = 0
        
        if winning_bets:
            payout_announcements.append(f"\n***💰 馬券配当結果 (固定オッズ) 💰***")
            payout_announcements.append(f"  勝ち馬: **{winner_name}**")
            
            # 3. 各ベットに対し、記録された固定オッズで支払い
            for bet in winning_bets:
                user_id = bet["user_id"]
                user_stake = bet["amount"]
                recorded_odds = bet["odds"] # ★ 記録されたオッズを使用
                
                # 支払い額 = 賭け金 * オッズ
                payout = int(user_stake * recorded_odds)
                
                # 支払い
                if user_id not in data["owners"]:
                    # オーナー情報がなければ作成
                    data["owners"][user_id] = {"horses": [], "name": f"User_{user_id}", "balance": 0} 
                
                data["owners"][user_id]["balance"] += payout
                
                total_payout += payout
                total_user_stake += user_stake

                try:
                    user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
                    user_name = user.display_name
                except:
                    user_name = f"User_{user_id}"

                payout_announcements.append(
                    f"  **{user_name}** ({user_stake:,}円 @{recorded_odds:.1f}倍): {payout:,}円獲得！"
                )

            payout_announcements.append(f"（総払い戻し: {total_payout:,}円 / 総賭け金: {total_user_stake:,}円）")
        
        else:
            # 勝ち馬に誰も賭けていない場合
            total_pool_all_bets = sum(sum(b['amount'] for b in bet_list) for bet_list in bets_on_race.values())
            payout_announcements.append("\n***💰 馬券配当結果 💰***")
            payout_announcements.append(f"  勝ち馬（{winner_name}）に賭けたユーザーはいませんでした。（総賭け金 {total_pool_all_bets:,}円はBotの収益となります）")
            
    # ------------------ 結果告知とデータ更新 ------------------

    channel_id = data.get("announce_channel")
    channel = None
    if channel_id:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if channel:
            await channel.send("\n".join(announcement))
    
    # 配当メッセージを別送
    if payout_announcements and channel:
        await channel.send("\n".join(payout_announcements))
    
    # レース後の処理: GⅠエントリーをクリーンアップ
    if is_g1 and day_key in data["pending_entries"]:
        del data["pending_entries"][day_key]
        
    # 処理が完了した賭けをクリア
    if bet_pool_key in data["pending_bets"]:
        del data["pending_bets"][bet_pool_key]
    
    # 日付を進める
    await advance_day(data)
    await save_data(data)


async def advance_day(data):
    """シーズンを進めるロジックと引退処理"""
    season = data["season"]
    current_day = season["day"]

    # 1. 引退処理
    await check_and_retire_horses(data)

    # 2. 日付を進める
    current_day += 1

    if current_day > MAX_G1_DAY:
        # シーズン終了：次の年に
        season["year"] += 1
        season["month"] = 1 # 仮に1月1日とする
        season["day"] = 1 # 1日目からスタート
        
        # 全馬の年齢を更新
        for horse in data["horses"].values():
            horse["age"] = horse.get("age", 2) + 1
            
        # シーズン終了告知は、レース実行ループの外で別途行うべきだが、ここでは単純化のため割愛
        
    else:
        # 月日の更新は一旦簡易的に日を増やすのみ（複雑化を避けるため）
        # 実際の月日を計算する
        target_date = datetime(season["year"], 1, 1, tzinfo=JST) + timedelta(weeks=current_day - 1)
        season["month"] = target_date.month
        season["day"] = current_day # 内部的なシーズン日を維持

    data["season"] = season


async def check_and_retire_horses(data):
    """引退条件（50レース以上 or 6歳以上）を満たした馬を引退させる"""
    horses_to_retire_info = []

    for horse_id, horse in data["horses"].items():
        if horse.get("is_bot"):
            continue # Bot馬は自動引退処理から除外

        should_retire = False
        
        # 1. 50レース以上
        race_count = len(horse.get("history", []))
        if race_count >= 50:
            should_retire = True
            
        # 2. 6歳以上
        if horse.get("age", 0) >= 6:
            should_retire = True

        if should_retire:
            horses_to_retire_info.append((horse_id, horse["owner"], horse["name"]))

    
    # 実際の引退処理
    retired_names = []
    for horse_id, owner_id, horse_name in horses_to_retire_info:
        # pending_entriesから馬IDを削除
        _clean_pending_entry(data, horse_id) 
        
        if owner_id in data["owners"] and horse_id in data["owners"][owner_id]["horses"]:
            data["owners"][owner_id]["horses"].remove(horse_id)
        
        # data["horses"]から削除
        if horse_id in data["horses"]:
            del data["horses"][horse_id]
            retired_names.append(horse_name)

    # 引退馬の告知
    if retired_names:
        channel_id = data.get("announce_channel")
        if channel_id:
             channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
             if channel:
                 await channel.send(
                     f"🚨 **引退通知**: 本日、規定により以下の**{len(retired_names)}頭**の競走馬が引退しました。\n"
                     f"引退馬: {', '.join(retired_names)}"
                 )
             else:
                 print(f"Warning: Announce channel with ID {channel_id} not found.")

    await save_data(data)

# ------------------ 定期タスク ------------------

@tasks.loop(minutes=1)
async def check_time():
    now_jst = datetime.now(JST)
    
    # 告知時間チェック (時刻比較時、タイムゾーン情報(tzinfo)を取り除くことでTypeErrorを回避)
    current_time_naive = now_jst.time().replace(tzinfo=None)
    pre_announce_time_naive = PRE_ANNOUNCE_TIME_JST.replace(tzinfo=None)
    race_time_naive = RACE_TIME_JST.replace(tzinfo=None)
    
    # 告知時間チェック
    if current_time_naive >= pre_announce_time_naive and current_time_naive < race_time_naive:
        await check_pre_announce()

    # レース時間チェック
    if current_time_naive >= race_time_naive:
        # 既にレースが実行されていないかチェック（レース実行は1日に1回のみ）
        data = await load_data()
        last_race_day = data["season"].get("last_race_day", 0)
        current_day = data["season"]["day"]

        # 同一シーズン日でレースが未実行なら実行
        if current_day != last_race_day:
            data["season"]["last_race_day"] = current_day
            await save_data(data)
            await run_race_and_advance_day()


async def check_pre_announce():
    """レース1時間前に告知を行う"""
    data = await load_data()
    day_key = str(data["season"]["day"])
    race_info = data["schedule"].get(day_key)
    channel_id = data.get("announce_channel")
    
    # 既に告知済みか確認するフラグ（簡易的なインメモリフラグ）
    if hasattr(check_pre_announce, 'announced_day') and check_pre_announce.announced_day == day_key:
        return

    if channel_id and race_info:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if channel:
            total_prize, _ = prize_pool_for_g1(race_info['name'])
            entries_count = len(data["pending_entries"].get(day_key, {}).get("entries", []))

            message = (
                f"🚨 **レース予告**（{RACE_TIME_JST.hour}:00 JST 開催予定）\n"
                f"🏆 本日のGⅠレース: **{race_info['name']}** ({race_info['distance']}m/{race_info['track']})\n"
                f"  賞金総額: ¥{total_prize:,} | 現在登録頭数: {entries_count}頭\n"
                f"  まだ間に合います！`!entry [馬ID]` で急いで登録、`!bet [馬ID] [金額]` で馬券を購入しましょう。"
            )
            await channel.send(message)
            
            # 告知済みフラグをセット
            check_pre_announce.announced_day = day_key


# ------------------ データ管理コマンド ------------------

@bot.command(name="setchannel", help="レース結果を通知するチャンネルを設定します")
@commands.has_permissions(administrator=True)
async def set_channel(ctx):
    data = await load_data()
    data["announce_channel"] = ctx.channel.id
    await save_data(data)
    await ctx.reply("✅ このチャンネルをレース結果の通知チャンネルに設定しました。")


# main.py の 1259行目付近

@bot.command(name="resetdata", help="⚠️全てのデータをリセットします（要確認）")
@commands.has_permissions(administrator=True)
async def reset_data(ctx):
    token = str(random.randint(1000, 9999))
    PENDING_RESETS[ctx.author.id] = token
    await ctx.reply(f"⚠️ **警告**: この操作は全ての馬、オーナー、レース履歴を消去します。\n"
                    f"続行する場合は、10秒以内に `!confirmreset {token}` と送信してください。")

    await asyncio.sleep(10)
    # ここで途切れていた部分を完成させる
    if ctx.author.id in PENDING_RESETS and PENDING_RESETS[ctx.author.id] == token:
        del PENDING_RESETS[ctx.author.id]
        await ctx.author.send("リセット待機時間が経過しました。リセットは実行されません。")
        await ctx.reply("リセット待機時間が経過しました。")

@bot.command(name="confirmreset", help="!resetdataの確認")
@commands.has_permissions(administrator=True)
async def confirm_reset(ctx, token: str):
    if ctx.author.id in PENDING_RESETS and PENDING_RESETS[ctx.author.id] == token:
        # リセット実行
        data = await load_data()
        
        # データベース内のデータをデフォルトデータで上書き
        default_data = {
            "horses": {},
            "owners": {},
            "races": [],
            "schedule": default_schedule(),
            "rankings": {"prize": {}, "wins": {}, "stable": {}},
            "announce_channel": data.get("announce_channel"), # 通知チャンネル設定は残す
            "pending_entries": {},
            "pending_bets": {}
        }
        
        # シーズン情報もリセット
        today = datetime.now(JST)
        default_data["season"] = {
            "year": today.year,
            "month": today.month,
            "day": today.day,
            "last_race_day": 0
        }

        await save_data(default_data)
        del PENDING_RESETS[ctx.author.id]
        
        await ctx.reply("✅ **データリセットが完了しました。** 全ての馬、オーナー、レース履歴が消去されました。")
    else:
        await ctx.reply("トークンが無効か、リセット待機時間が経過しました。")

# ------------------ 実行 ------------------

def keep_alive():
    # Flaskサーバーを別スレッドで起動
    t = threading.Thread(target=run_flask)
    t.start()

if __name__ == "__main__":
    from flask import Flask # ★ Flaskのインポート位置を修正
    keep_alive() 
    
    # Discord Bot Tokenは環境変数から取得
    BOT_TOKEN = os.getenv("DISCORD_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
        
    bot.run(BOT_TOKEN)
