from table2ascii import table2ascii as t2a, PresetStyle
import os
import json
import random
import asyncio
import calendar
import threading
from datetime import datetime, timezone, timedelta, time 
from flask import Flask
from table2ascii import table2ascii as t2a, PresetStyle
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
# 1週間に同一オーナーがエントリーできる最大頭数（ここでは日毎に適用）
MAX_ENTRIES_PER_WEEK = 4 
# GⅠの最低出走頭数（これに満たない場合Bot馬を補充）
MIN_G1_FIELD = 18 
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


def calculate_odds(horse):
    """
    勝利数をもとに固定オッズ計算
    """
    base = 6.0
    wins = horse.get("wins", 0)
    odds = base / (wins + 1)
    return round(max(1.2, odds), 1)


def default_schedule():
    """レーススケジュール定義（キーは文字列。第1週〜第30週に固定のGⅠを割り当てる）"""
    # 30個のGⅠを、シーズンの1日から30日に対応させる
    return { # <-- ここを正しくインデントする
        # --------------------- 年末年始（ダート・海外） ---------------------
        "1":  {"name": "GⅠ 東京大賞典", "distance": 2000, "track": "ダート"},
        "2":  {"name": "GⅠ 川崎記念", "distance": 2100, "track": "ダート"}, # 地方GⅠ追加
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
        "favorite": False,
        "rest_used_day": -1 
    }


def calc_race_score(horse, distance, track):
    s = horse["stats"]
    speed = s["speed"]
    stamina = s["stamina"]
    temper = s["temper"] # TEMPER (気性)
    growth = s["growth"] # GROWTH (成長力/バフ)
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

    # 根幹能力以外の補正 (GRW/TEMによる基本スコア補正)
    if track == "ダート":
        # TEMのダート補正を維持
        condition_factor = 0.95 + (temper / 100) * 0.1 
    else:
        # 芝ではGRWの補正を維持
        condition_factor = 1.0 + (growth / 100) * 0.15

    # 疲労とコンディション計算
    fatigue = horse.get("fatigue", 0)
    cond = max(0.75, 1.0 - (fatigue * 0.02))
    
    # --- ★ TEMPER (気性) によるランダム幅の調整 ★ ---
    # TEMが高いほど下限が上がり、下振れを防ぐ。上限は固定で大穴の可能性を維持。
    tem_stab_factor = (temper / 100) * 0.15 
    lower_bound = 0.85 + tem_stab_factor
    
    # 上限は1.15のまま維持
    rand = random.uniform(lower_bound, 1.15)
    # --- ★ 調整ここまで ★ ---

    score = base * apt_factor * condition_factor * rand * cond
    return score

def prize_pool_for_g1(race_name):
    """GⅠレース名に基づき、賞金プールを決定する"""
    
    # 高額賞金レース
    if "サウジカップ" in race_name or "ドバイWC" in race_name or "ジ・エベレスト" in race_name or "有馬記念" in race_name or "ジャパン" in race_name:
        total = 500_000 # 海外トップクラス
        
    # 海外主要・地方交流レース
    elif "凱旋門賞" in race_name or "キングジョージ6世" in race_name or "BCクラシック" in race_name or "チャンピオンズマイル" in race_name:
        total = 300_000 # 日本GⅠよりやや高額
        
    # 日本のGⅠレース（デフォルト）
    else:
        total = 200_000 
        
    # GⅠの配分率は変わらず、5着まで
    payout_rate = [0.55, 0.2, 0.12, 0.08, 0.05]
    
    return total, payout_rate

def prize_pool_for_lower():
    """下級レースの賞金設定"""
    total = 17000 
    return total, [10000/17000, 5000/17000, 2000/17000] # 10000, 5000, 2000

def progress_growth(horse):
    g = horse["stats"]["growth"]
    # レース後の成長力を 2-5 に強化
    horse["stats"]["growth"] = min(100, g + random.randint(2, 5))

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

@bot.command(name="bet", help="出走馬に賭けます （例: !bet H12345 1000）")
async def bet(ctx, horse_id: str, amount: int):
    data = await load_data()
    user_id = str(ctx.author.id)

    # 出走リスト取得
    day = str(data["season"]["day"])
    entries = data.get("pending_entries", {}).get(day, [])
    if horse_id not in entries:
        await ctx.reply("指定された馬は本日の出走リストにありません。")
        return

    # 所持金チェック
    users = data.setdefault("users", {})
    user = users.setdefault(user_id, {"money": 0})
    money = user.get("money", 0)

    if amount <= 0:
        await ctx.reply("賭け金は 1 以上で指定してください。")
        return

    if money < amount:
        await ctx.reply(f"所持金が不足しています（現在: {money}）")
        return

    # 既存の bets を取得（なければ初期化）
    bets = data.setdefault("bets", {}).setdefault(day, {})
    if user_id in bets:
        await ctx.reply("本日のレースには既に賭けています。")
        return

    horse = data["horses"].get(horse_id)
    if not horse:
        await ctx.reply("その馬は存在しません。")
        return

    odds_val = calculate_odds(horse)

    # 賭けを登録して所持金を減らす
    bets[user_id] = {
        "horse_id": horse_id,
        "amount": amount,
        "odds": odds_val
    }
    user["money"] -= amount

    await save_data(data)

    payout = int(amount * odds_val)

    await ctx.reply(
        f"🎫 **賭けを受け付けました！**\n"
        f"馬名: {horse['name']}\n"
        f"賭け金: {amount}\n"
        f"オッズ: {odds_val} 倍\n"
        f"的中時の払戻: {payout}"
    )

@bot.command(name="odds", help="本日の出走馬オッズ一覧を表示します")
async def odds(ctx):
    data = await load_data()

    day = str(data["season"]["day"])
    entries = data.get("pending_entries", {}).get(day, [])
    if not entries:
        await ctx.reply("本日の出走馬がいません。")
        return

    odds_table = []
    for hid in entries:
        horse = data["horses"].get(hid)
        if not horse:
            continue
        odds_val = calculate_odds(horse)
        odds_table.append([hid, horse["cut_horse_name"], horse.get("wins", 0), odds_val])

    if not odds_table:
        await ctx.reply("オッズを表示する出走馬がいません。")
        return

    ascii_table = t2a(
        header=["馬ID", "馬名", "勝利数", "オッズ"],
        body=odds_table,
        style=PresetStyle.thin_compact
    )

    await ctx.reply("🏇 **本日のオッズ**\n```" + ascii_table + "```")

@bot.command(name="nextday", help="【管理者】日付を1日進めます（レース処理なし）")
async def next_day(ctx):
    if not is_admin(ctx):
        await ctx.reply("このコマンドは管理者専用です。")
        return

    data = await load_data()

    before = (
        data["season"]["year"],
        data["season"]["month"],
        data["season"]["day"]
    )

    # 未処理データの掃除（任意だが推奨）
    current_day_str = str(data["season"]["day"])
    data.get("pending_entries", {}).pop(current_day_str, None)
    data.get("bets", {}).pop(current_day_str, None)

    # 日付を進める（既存関数を利用）
    await advance_day(data)

    after = (
        data["season"]["year"],
        data["season"]["month"],
        data["season"]["day"]
    )

    await save_data(data)

    await ctx.reply(
        f"📅 **日付を進めました**\n"
        f"{before[0]}年{before[1]}月{before[2]}日 → "
        f"{after[0]}年{after[1]}月{after[2]}日"
    )

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
        "favorite": False,
        "rest_used_day": -1 
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
    
    # pending_entriesから馬IDを削除
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
        # pending_entriesから馬IDを削除
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
        reply_msg.append("なし")
        
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
        
        # レース出走回数を計算
        race_count = len(h.get("history", []))

        lines.append(
            f"{fav_icon} - {h['name']} (ID: {hid}) / 年齢:{h['age']} / **レース数:{race_count}** / 勝利:{h['wins']} / 疲労:{h['fatigue']} / "
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
    
# 【既存】出走登録取り消しコマンド
@bot.command(name="unentry", help="本日のレースへの出走登録を取り消します: 例) !unentry H12345")
async def unentry(ctx, horse_id: str):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)
    current_day = data["season"]["day"]
    day_key = str(current_day)

    if not horse:
        await ctx.reply("そのIDの馬は存在しません。")
        return
    if horse["owner"] != uid:
        await ctx.reply("これはあなたの馬ではありません。")
        return
        
    pending = data.get("pending_entries", {})
    
    if day_key not in pending or horse_id not in pending[day_key]:
        await ctx.reply(f"**{horse['name']}** は本日(第{current_day}週)のレースにエントリーされていません。")
        return
        
    # エントリーを取り消し
    pending[day_key].remove(horse_id)
    
    # エントリーリストが空になったらキー自体を削除
    if not pending[day_key]:
         del pending[day_key]

    data["pending_entries"] = pending
    await save_data(data)
    
    await ctx.reply(f"✅ **{horse['name']}** の本日(第{current_day}週)のレースへの出走登録を取り消しました。")

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
            schedule_lines.append(
                f"**第{day}週**: {race_info['name']} ({race_info['distance']}m/{race_info['track']}) - **{status}**"
            )
        elif day == current_day:
            schedule_lines.append(f"**第{day}週 (本日)**: GⅠ開催はありません。（定刻に下級レースを実行します）")
        elif day == current_day + 1:
            schedule_lines.append(f"**第{day}週 (明日)**: GⅠ開催はありません。（定刻に下級レースを実行します）")


    if not schedule_lines and current_day > MAX_G1_DAY:
        header.append(f"✅ 第{MAX_G1_DAY}週までのGⅠレースは全て終了しました。")
    
    await ctx.reply("\n".join(header + schedule_lines))

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
        await ctx.reply(
            f"{current_year}年{current_month}月 第{current_day}日（第{current_day}週）はGⅠ開催日ではありません。"
        )
        return

    race_info = data["schedule"].get(current_day_str)
    if not race_info:
        await ctx.reply(
            f"本日({current_day}日)はGⅠはありません。（スケジュールに定義されていません）"
        )
        return

    entries_list = data.get("pending_entries", {}).get(current_day_str, [])
    if not entries_list:
        await ctx.reply(
            f"本日のGⅠ「**{race_info['name']}**」にエントリーされている馬はいません。"
        )
        return

    entries_data = []
    post_position = 1

    for hid in entries_list:
        horse = data["horses"].get(hid)
        if not horse or horse["owner"] == BOT_OWNER_ID:
            continue

        try:
            user = bot.get_user(int(horse["owner"])) or await bot.fetch_user(int(horse["owner"]))
            owner_name = user.display_name
        except:
            owner_name = "不明"

        entries_data.append([
            post_position,
            hid,
            cut_horse_name(horse["name"]),
            owner_name,
            horse.get("fatigue", 0),
            horse.get("wins", 0),
        ])
        post_position += 1

    if not entries_data:
        await ctx.reply("本日のGⅠにエントリーされているプレイヤー馬はいません。")
        return

    ascii_table = t2a(
        header=["馬番", "ID", "馬名", "オーナー", "疲労", "勝利"],
        body=entries_data,
        style=PresetStyle.thin_compact
    )

    header_text = (
        f"🏆 **{current_year}年{current_month}月 第{current_day}週 GⅠ出馬表**\n"
        f"{race_info['name']} / {race_info['distance']}m / {race_info['track']}\n"
    )

    await ctx.reply(header_text + "```" + ascii_table + "```")

@bot.command(name="rest", help="馬を休養させて疲労を回復します（1日1回）: 例) !rest H12345")
async def rest(ctx, horse_id: str):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)
    current_day = data["season"]["day"]
    
    if not horse:
        await ctx.reply("そのIDの馬は存在しません。")
        return
    if horse["owner"] != uid:
        await ctx.reply("これはあなたの馬ではありません。")
        return
    
    # ----------------- 1日1回制限チェック -----------------
    if horse.get("rest_used_day") == current_day:
        await ctx.reply(f"**{horse['name']}** は本日(第{current_day}週)既に休養しています。1日に1回までしか休養できません。")
        return
    # ---------------------------------------------------

    old = horse.get("fatigue", 0)
    horse["fatigue"] = max(0, old - 3)
    horse["rest_used_day"] = current_day 
    await save_data(data)
    await ctx.reply(f"**{horse['name']}** を休養させました。疲労 {old} → {horse['fatigue']}")

@bot.command(name="train", help="GRWを消費してステータスを恒久的に強化します: 例) !train H12345 speed 3")
async def train(ctx, horse_id: str, stat_name: str, amount: int):
    data = await load_data()
    uid = str(ctx.author.id)
    horse = data["horses"].get(horse_id)
    
    # 1. 馬の存在とオーナー権限のチェック
    if not horse or horse["owner"] != uid:
        await ctx.reply("そのIDの馬は存在しないか、あなたの馬ではありません。")
        return

    # 2. ステータス名のチェックと変換
    stat_name = stat_name.lower()
    allowed_stats_map = {
        "speed": "speed", "spd": "speed",
        "stamina": "stamina", "sta": "stamina",
        "temper": "temper", "tem": "temper",
        "turf": "turf_apt", "芝": "turf_apt",
        "dirt": "dirt_apt", "ダート": "dirt_apt"
    }
    
    if stat_name not in allowed_stats_map:
        await ctx.reply("⚠️ **エラー**: 強化できるステータスは `speed`, `stamina`, `temper`, `turf`(芝), `dirt`(ダート) のいずれかです。")
        return
        
    target_stat = allowed_stats_map[stat_name]

    # 3. 消費量のチェック
    if not (1 <= amount <= MAX_TRAIN_AMOUNT):
        await ctx.reply(f"⚠️ **エラー**: 消費するGRWの量は1から{MAX_TRAIN_AMOUNT}ポイントの間で指定してください。")
        return

    # 4. GRWの残高チェック
    current_grw = horse["stats"].get("growth", 0)
    if current_grw < amount:
        await ctx.reply(f"⚠️ **エラー**: **{horse['name']}** の現在のGRWは {current_grw} です。{amount}ポイントを消費するにはGRWが不足しています。")
        return
        
    # 5. 実行
    
    # GRW消費 
    horse["stats"]["growth"] = max(0, current_grw - amount) 
    
    # ステータス増加 (変換レート1:1)
    amount_to_add = amount * GRW_CONVERSION_RATE 
    
    old_stat_value = horse["stats"].get(target_stat, 0)
    new_stat_value = min(100, old_stat_value + amount_to_add)
    
    horse["stats"][target_stat] = new_stat_value
    
    # 疲労増加
    old_fatigue = horse.get("fatigue", 0)
    horse["fatigue"] = min(10, old_fatigue + 1)
    
    await save_data(data)
    
    # 6. 結果報告
    await ctx.reply(
        f"✅ **{horse['name']}** を調教しました！\n"
        f"消費GRW: **{amount}** (残り: {horse['stats']['growth']})\n"
        f"強化ステータス: **{target_stat.upper().replace('_APT', '').replace('TURF', '芝').replace('DIRT', 'ダート')}** {old_stat_value} → **{new_stat_value}**\n"
        f"疲労が1ポイント増加しました ({old_fatigue} → {horse['fatigue']})"
    )


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
        
        title = "👑 賞金ランキング 👑"
        sorted_board = sorted(board.items(), key=lambda item: item[1], reverse=True)[:10]

    else: # wins
        board = {}
        for uid, o in data["owners"].items():
            if uid == BOT_OWNER_ID: continue
            board[uid] = o.get("wins", 0)
            
        title = "🏆 勝利数ランキング 🏆"
        sorted_board = sorted(board.items(), key=lambda item: item[1], reverse=True)[:10]

    # ランキング表示の整形
    rank_lines = [title, "----------------------------"]
    
    for i, (uid, value) in enumerate(sorted_board):
        try:
            user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
            name = user.display_name
        except:
            name = "引退したオーナー"

        if category == "prize":
            value_str = f"{value:,}円"
        else:
            value_str = f"{value}勝"
            
        rank_lines.append(f"**{i+1}位.** {name} ({value_str})")

    await ctx.reply("\n".join(rank_lines))


# 起動時の処理
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} | PID={os.getpid()}")


# ----------------- タスクスケジューラ -----------------

@tasks.loop(minutes=1)
async def race_scheduler():
    now = datetime.now(JST)
    current_time_jst = now.time()
    current_day = now.day # 月の日付を「日」として使用

    # 1. レース告知 (RACE_TIME_JSTの1時間前)
    if PRE_ANNOUNCE_TIME_JST.hour == current_time_jst.hour and PRE_ANNOUNCE_TIME_JST.minute == current_time_jst.minute:
        await check_and_announce_race()
        
    # 2. レース実行 (RACE_TIME_JST)
    if RACE_TIME_JST.hour == current_time_jst.hour and RACE_TIME_JST.minute == current_time_jst.minute:
        await run_race_and_advance_day()


async def check_and_announce_race():
    data = await load_data()
    channel_id = data["announce_channel"]
    current_day = data["season"]["day"]
    current_day_str = str(current_day)
    
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    race_info = data["schedule"].get(current_day_str)
    
    if not race_info:
        # GⅠ期間外
        await channel.send(f"🏇 **【レース告知】** ⏱️ 本日（第{current_day}週）はGⅠレースの開催はありませんが、定刻に日付更新と下級レースを行います。")
        return
        
    entries_count = len(data.get("pending_entries", {}).get(current_day_str, []))
    
    if entries_count == 0:
        await channel.send(
            f"🏇 **【レース告知】** 📢\n"
            f"まもなく開催！ GⅠ「**{race_info['name']}**」 ({race_info['distance']}m/{race_info['track']})\n"
            f"現在のエントリー数は**0頭**です。出走したい馬は今すぐ `!entry <ID>` で登録してください！\n"
            f"締め切りはレース開始時刻（{RACE_TIME_JST.hour:02}:{RACE_TIME_JST.minute:02} JST）です！"
        )
    else:
        await channel.send(
            f"🏇 **【レース告知】** 📢\n"
            f"まもなく開催！ GⅠ「**{race_info['name']}**」 ({race_info['distance']}m/{race_info['track']})\n"
            f"現在のエントリー数は**{entries_count}頭**です。エントリー締め切りまであと**1時間**！"
        )


async def run_race_and_advance_day():
    data = await load_data()
    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]
    current_day_str = str(current_day)
    channel_id = data["announce_channel"]

    if not channel_id:
        print("Announce channel not set. Skipping race execution.")
        await advance_day(data)
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"Channel with ID {channel_id} not found. Skipping race execution.")
        await advance_day(data)
        return

    race_info = data["schedule"].get(current_day_str)
    
    is_g1 = bool(race_info)
    
    if not is_g1:
        # GⅠのない日は下級レースを実施（固定レース情報）
        race_info = {"name": "下級レース", "distance": random.choice([1200, 1600, 2000, 2400]), "track": random.choice(["芝", "ダート"])}
        entries_list = []
        # 下級レースでは、疲労が少ない全ての馬が自動でエントリーされる（疲労1未満）
        for hid, horse in data["horses"].items():
            if horse["owner"] != BOT_OWNER_ID and horse.get("fatigue", 0) < 1:
                entries_list.append(hid)
    else:
        # GⅠがある日
        entries_list = data.get("pending_entries", {}).get(current_day_str, [])
        
        # GⅠの出走頭数が少ない場合、Bot馬を補充
        if len(entries_list) < MIN_G1_FIELD:
            for _ in range(MIN_G1_FIELD - len(entries_list)):
                bot_horse = generate_bot_horse(data["horses"])
                data["horses"][bot_horse["id"]] = bot_horse
                entries_list.append(bot_horse["id"])


    
    if not entries_list:
        if is_g1:
            await channel.send(f"本日(第{current_day}週)のGⅠ「**{race_info['name']}**」は、出走馬がいなかったためレースは中止されました。")
        else:
             await channel.send(f"本日(第{current_day}週)の下級レースは、出走可能な馬がいなかったため中止されました。")
             
        await advance_day(data)
        return


    # ------------------ レース実行ロジック ------------------
    
    all_entries = []
    # 馬番をランダムに割り振るためにシャッフル
    random.shuffle(entries_list) 
    
    post_position = 1
    for horse_id in entries_list:
        horse = data["horses"].get(horse_id)
        if not horse:
            continue
            
        score = calc_race_score(horse, race_info["distance"], race_info["track"])
        
        all_entries.append({
            "horse_id": horse_id,
            "horse_name": horse["name"],
            "owner": horse["owner"],
            "score": score,
            "post_position": post_position
        })
        post_position += 1
        
        # 疲労増加と年齢上昇の準備
        if horse["owner"] != BOT_OWNER_ID:
            horse["fatigue"] = min(10, horse.get("fatigue", 0) + 2)
            progress_growth(horse)
            # レース後のGRWバフの効果は即時反映されるため、個別の記録は不要
            
    # スコアでソートし、順位を決定
    all_entries.sort(key=lambda x: x["score"], reverse=True)
    
    winner_id = all_entries[0]["horse_id"]
    
    results = []
   # レース名に応じて賞金プールを決定
    prize_config = prize_pool_for_g1(race_info['name']) if is_g1 else prize_pool_for_lower()
    
    for i, entry in enumerate(all_entries):
        pos = i + 1
        prize = 0
        if pos <= len(prize_config[1]):
            prize = int(prize_config[0] * prize_config[1][i])
        
        entry["pos"] = pos
        entry["prize"] = prize
        results.append(entry)
        
        # 賞金と勝利数の更新
        owner_id = entry["owner"]
        if owner_id != BOT_OWNER_ID:
            if owner_id not in data["owners"]:
                data["owners"][owner_id] = {"horses": [], "balance": 0, "wins": 0}

            data["owners"][owner_id]["balance"] = data["owners"][owner_id].get("balance", 0) + prize
            
            if pos == 1:
                data["horses"][entry["horse_id"]]["wins"] = data["horses"][entry["horse_id"]].get("wins", 0) + 1
                data["owners"][owner_id]["wins"] = data["owners"][owner_id].get("wins", 0) + 1
                
        # レース履歴の追加
        if entry["owner"] != BOT_OWNER_ID:
             data["horses"][entry["horse_id"]]["history"].append({
                 "race": race_info["name"],
                 "pos": pos,
                 "prize": prize,
                 "date": f"{current_year}年{current_month}月{current_day}日"
             })

    # 処理例
    bets = data.get("bets", {}).get(current_day_str, {})
    
    for uid, b in bets.items():
        if b["horse_id"] == winner_id:
            payout = int(b["amount"] * b["odds"])
            data["users"].setdefault(uid, {"money":0})
            data["users"][uid]["money"] += payout

    # ------------------ 結果告知とデータ更新 ------------------
    await announce_race_results(data, race_info, results, current_day, current_month, current_year, channel, len(entries_list))
    
    # 処理が完了したエントリーリストをクリア
    if is_g1 and current_day_str in data["pending_entries"]:
        del data["pending_entries"][current_day_str] 

    # ベットもクリア
    if current_day_str in data.get("bets", {}):
        del data["bets"][current_day_str]
    
    # 日付を進める
    await advance_day(data)


async def advance_day(data):
    """日付を1日進める処理（自動引退チェックを含む）"""
    
    # シーズンを進行
    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]
    
    new_day = current_day + 1
    new_month = current_month
    new_year = current_year
    
    # 30日でシーズン終了
    if new_day > 30:
        new_day = 1
        new_month += 1
        
    if new_month > 12:
        new_month = 1
        new_year += 1
        
    data["season"]["day"] = new_day
    data["season"]["month"] = new_month
    data["season"]["year"] = new_year
    
    horses_to_retire_info = [] # Stores (horse_id, owner_id, horse_name)

    # 全馬の rest_used_day をリセットし、引退チェック
    for horse_id, horse in list(data["horses"].items()): # イテレーション中に削除するためコピーを使用
        
        if horse["owner"] == BOT_OWNER_ID:
            # Bot馬は引退させない
            continue

        horse["rest_used_day"] = -1
        
        # 馬齢の更新 (シーズン開始日: 1月1日に固定)
        if new_month == 1 and new_day == 1:
             horse["age"] += 1

        # --- 自動引退チェック ---
        should_retire = False
        
        # 1. 50レース出走
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
             channel = bot.get_channel(channel_id)
             if channel:
                 await channel.send(
                     f"🚨 **引退通知**: 本日、規定により以下の**{len(retired_names)}頭**の競走馬が引退しました。\n"
                     f"引退馬: {', '.join(retired_names)}"
                 )
             else:
                 print(f"Warning: Announce channel with ID {channel_id} not found.")

    await save_data(data)
    print(f"Date advanced to: {new_year}/{new_month}/{new_day}")


# 起動
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user}")

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ["DISCORD_TOKEN"])
