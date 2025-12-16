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

# 環境変数から設定を取得
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DATABASE_TABLE = "racing_data" # データ保存用テーブル名
BOT_OWNER_ID = os.environ.get("BOT_OWNER_ID", "999999999999999999") # botオーナーのID

# Supabaseクライアント初期化
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------- データ操作 ----------------

async def load_data():
    """データベースからデータを読み込む"""
    try:
        response = supabase.from_(DATABASE_TABLE).select("*").single().execute()
        # response.data が None でないかチェック
        if response.data and 'data' in response.data:
            return response.data['data']
        # データがない場合は初期データを返す
        return {
            "horses": {},
            "owners": {},
            "races": [], # 過去のレース全結果
            "pending_entries": {},
            "bets": {},
            "season": {"year": 2024, "month": 1, "day": 1},
            "next_id": 1,
            "announce_channel": None,
            "config": {"race_interval_hours": 24}
        }
    except Exception as e:
        print(f"Error loading data from Supabase: {e}")
        # エラー発生時も初期データを返す
        return {
            "horses": {},
            "owners": {},
            "races": [], # 過去のレース全結果
            "pending_entries": {},
            "bets": {},
            "season": {"year": 2024, "month": 1, "day": 1},
            "next_id": 1,
            "announce_channel": None,
            "config": {"race_interval_hours": 24}
        }

async def save_data(data):
    """データベースにデータを保存する"""
    try:
        # id=1 のレコードを upsert (挿入または更新)
        supabase.from_(DATABASE_TABLE).upsert({"id": 1, "data": data}).execute()
    except Exception as e:
        print(f"Error saving data to Supabase: {e}")


# ---------------- ユーティリティ関数 ----------------

def new_horse_id(data):
    """新しい馬IDを生成する"""
    next_id = data["next_id"]
    data["next_id"] += 1
    return f"H{next_id:05d}"

def calculate_odds(horse):
    """馬のオッズを計算する（簡易版: SPとGRWの合計値に基づく）"""
    base_skill = horse.get("SP", 0) + horse.get("GRW", 0)
    # スキルが低いほどオッズが高くなるように調整
    # SP+GRW=300を基準に、オッズを1.5〜50倍の範囲で変動させる
    if base_skill <= 100:
        return 50.0
    if base_skill >= 500:
        return 1.5

    # 100〜500の間で線形補間
    # 傾き: (1.5 - 50.0) / (500 - 100) = -48.5 / 400 = -0.12125
    odds = 50.0 - 0.12125 * (base_skill - 100)
    return round(max(1.5, min(50.0, odds)), 2)

def calculate_race_score(horse, race_distance, race_track):
    """レースのスコアを計算する (SP, ST, CND, 距離適性、疲労、GRWバフを考慮)"""
    sp = horse.get("SP", 0)
    st = horse.get("ST", 0)
    cnd = horse.get("CND", 0)
    fatigue = horse.get("fatigue", 0)
    grw_buff = horse.get("grw_buff", 0)

    # 距離適性 (例: 短距離: 1000-1400, マイル: 1600, 中距離: 1800-2400, 長距離: 2500-)
    dist_pref = horse.get("dist_pref", "Medium")
    
    distance_factor = 1.0
    
    if dist_pref == "Short": # 短距離適性 (1000m-1400m)
        if 1500 <= race_distance <= 2000: distance_factor = 0.95
        elif race_distance > 2000: distance_factor = 0.85
    elif dist_pref == "Mile": # マイル適性 (1600m)
        if race_distance < 1400: distance_factor = 0.9
        elif race_distance > 2000: distance_factor = 0.9
    elif dist_pref == "Medium": # 中距離適性 (1800m-2400m)
        if race_distance < 1600: distance_factor = 0.9
        elif race_distance > 2800: distance_factor = 0.8
    elif dist_pref == "Long": # 長距離適性 (2500m-)
        if race_distance < 2000: distance_factor = 0.85
        elif 1400 <= race_distance < 1800: distance_factor = 0.9
        elif race_distance < 1400: distance_factor = 0.8
        
    # トラック適性 (芝/ダート)
    track_pref = horse.get("track_pref", "Turf")
    track_factor = 1.0
    if track_pref == "Turf" and race_track == "Dirt":
        track_factor = 0.9
    elif track_pref == "Dirt" and race_track == "Turf":
        track_factor = 0.9

    # スキルとコンディション
    base_score = (sp * 0.45) + (st * 0.3) + (cnd * 0.25)
    
    # 疲労による減点 (疲労10で-15%)
    fatigue_penalty = (fatigue / 10.0) * 0.15 
    
    # 距離/トラック適性による補正
    score_after_adapt = base_score * distance_factor * track_factor
    
    # 最終スコア
    final_score = score_after_adapt * (1 - fatigue_penalty) + grw_buff
    
    # ランダム要素の追加 (±5%)
    random_factor = random.uniform(0.95, 1.05)
    final_score *= random_factor
    
    return int(max(0, final_score))

def is_g1(race_name):
    """レース名がG1か判定する"""
    return race_name.endswith("G1")

def prize_pool_for_g1(race_name):
    """G1レースの賞金総額と配分率"""
    if race_name in ["日本ダービー(G1)", "ジャパンカップ(G1)"]:
        # ダービー/JC: 1着 3億円, 総額 5.5億円
        total = 550_000_000
        # 1着 54.5%, 2着 22.7%, 3着 13.6%, 4着 5.5%, 5着 3.6% (概算)
        shares = [0.545, 0.227, 0.136, 0.055, 0.036]
        return total, shares
    else:
        # その他のG1: 1着 1.8億円, 総額 3.3億円
        total = 330_000_000
        # 1着 54.5%, 2着 22.7%, 3着 13.6%, 4着 5.5%, 5着 3.6% (概算)
        shares = [0.545, 0.227, 0.136, 0.055, 0.036]
        return total, shares

def prize_pool_for_lower():
    """G1以外のレースの賞金総額と配分率 (例: OP, G3など)"""
    # 1着 2000万円, 総額 4000万円
    total = 40_000_000
    # 1着 50%, 2着 20%, 3着 15%, 4着 10%, 5着 5%
    shares = [0.50, 0.20, 0.15, 0.10, 0.05]
    return total, shares

def get_race_info(current_year, current_month, current_day):
    """現在の日付からレース情報を取得する (簡易版)"""
    
    race_schedule = {
        # 月をキー、日をサブキー
        1: {1: ("中山金杯(G3)", 2000, "Turf"), 5: ("京都金杯(G3)", 1600, "Turf")},
        4: {1: ("大阪杯(G1)", 2000, "Turf")},
        5: {1: ("天皇賞(春)(G1)", 3200, "Turf"), 5: ("日本ダービー(G1)", 2400, "Turf")},
        11: {1: ("ジャパンカップ(G1)", 2400, "Turf"), 5: ("チャンピオンズC(G1)", 1800, "Dirt")},
        12: {5: ("有馬記念(G1)", 2500, "Turf")}
        # 他にも適当なレースを追加
    }
    
    # 毎週末（例: 毎月1日と5日）には平場のオープンレースを開催
    if current_day % 5 == 1:
         race_name, distance, track = "OPクラス(芝)", 1800, "Turf"
    elif current_day % 5 == 5:
         race_name, distance, track = "OPクラス(ダ)", 1600, "Dirt"
    else:
         return None
         
    # 特定の日付にG1レースがあるか確認
    if current_month in race_schedule and current_day in race_schedule[current_month]:
        race_name, distance, track = race_schedule[current_month][current_day]

    return {"name": race_name, "distance": distance, "track": track}

def get_next_race_date(data):
    """次のレースまでの残り時間と日付を計算する"""
    
    # タイムスタンプを UTC で取得
    now_utc = datetime.now(timezone.utc)
    
    # 最後にレースが実行された時刻 (データに保存されていない場合は現在時刻を使用)
    last_race_time_str = data.get("last_race_time")
    
    if last_race_time_str:
        last_race_time = datetime.fromisoformat(last_race_time_str).replace(tzinfo=timezone.utc)
    else:
        last_race_time = now_utc
    
    # 次のレースの予定時刻
    interval_hours = data["config"].get("race_interval_hours", 24)
    next_race_time = last_race_time + timedelta(hours=interval_hours)
    
    # 残り時間
    time_remaining = next_race_time - now_utc
    
    if time_remaining.total_seconds() <= 0:
        return True, "今すぐ", None, None, None
    
    # 残り時間のフォーマット
    days = time_remaining.days
    hours, remainder = divmod(time_remaining.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    time_str = ""
    if days > 0: time_str += f"{days}日"
    if hours > 0: time_str += f"{hours}時間"
    if minutes > 0: time_str += f"{minutes}分"
    if not time_str: time_str = f"{seconds}秒"
    
    # 次のレースの日付
    next_year = data["season"]["year"]
    next_month = data["season"]["month"]
    next_day = data["season"]["day"]
    
    return False, time_str, next_year, next_month, next_day

def progress_growth(horse):
    """トレーニングレベルに基づく馬の成長処理"""
    
    # 成長判定
    if horse.get("age", 0) >= 3 and horse.get("wins", 0) >= 1:
        # 3歳以上で1勝以上
        # 成長フェーズ: Early, Peak, Late
        age = horse.get("age", 0)
        
        # 4歳まで: Early
        if age <= 4:
            growth_mult = 1.0 # 通常成長
        # 5歳: Peak
        elif age == 5:
            growth_mult = 0.5 # 成長鈍化
        # 6歳以上: Late
        else:
            growth_mult = 0.1 # ほぼ停止
            
        # トレーニングレベルに応じた成長量 (例: Lvl 1: +1, Lvl 10: +10)
        level_mult = horse.get("train_lvl", 1) / 5.0 
        
        # 成長の対象（SP, ST, CND）をランダムに選択
        stat_to_grow = random.choice(["SP", "ST", "CND"])
        
        growth_amount = int(random.random() * level_mult * growth_mult) + 1
        
        horse[stat_to_grow] = horse.get(stat_to_grow, 0) + growth_amount
        
        # GRW（成長係数）の減少
        horse["GRW"] = max(10, horse.get("GRW", 100) - 1) # 最低10

def _clean_pending_entry(data, horse_id):
    """pending_entriesから特定の馬IDを削除するヘルパー関数"""
    for day_str in list(data.get("pending_entries", {}).keys()):
        data["pending_entries"][day_str] = [
            h_id for h_id in data["pending_entries"][day_str] if h_id != horse_id
        ]
        # 空になった日を削除
        if not data["pending_entries"][day_str]:
            del data["pending_entries"][day_str]

# ---------------- コマンド ----------------

@bot.command(name="register", help="馬のオーナーとして登録します")
async def register(ctx):
    data = await load_data()
    user_id = str(ctx.author.id)

    if user_id in data.get("owners", {}):
        await ctx.reply("あなたは既にオーナーとして登録されています。")
        return

    # 新規オーナーの初期設定
    data.setdefault("owners", {})[user_id] = {
        "horses": [], 
        "balance": 100000, # 初期所持金 (賞金と統合)
        "wins": 0
    }
    
    await save_data(data)
    await ctx.reply("オーナーとして登録されました。初期所持金として100,000が与えられました。")

@bot.command(name="buyhorse", help="新しい馬を購入します")
async def buyhorse(ctx, name: str):
    data = await load_data()
    user_id = str(ctx.author.id)

    if user_id not in data.get("owners", {}):
        await ctx.reply("オーナー登録を先に行ってください (!register)")
        return
        
    owner = data["owners"][user_id]
    
    if len(owner["horses"]) >= 5:
        await ctx.reply("所有できる馬は5頭までです。")
        return

    # 馬の購入費用
    cost = 10000 
    if owner["balance"] < cost:
        await ctx.reply(f"所持金が不足しています (購入費用: {cost})")
        return

    # 初期ステータスの生成 (SP, ST, CND, GRWをランダムに決定)
    sp = random.randint(50, 150)
    st = random.randint(50, 150)
    cnd = random.randint(50, 150)
    grw = random.randint(50, 150) # 成長係数
    
    # 距離適性のランダム決定
    dist_pref = random.choice(["Short", "Mile", "Medium", "Long"])
    # トラック適性のランダム決定
    track_pref = random.choice(["Turf", "Dirt"])
    
    new_id = new_horse_id(data)
    
    # 馬データ
    data["horses"][new_id] = {
        "id": new_id,
        "name": name,
        "owner": user_id,
        "SP": sp,
        "ST": st,
        "CND": cnd,
        "GRW": grw,
        "dist_pref": dist_pref,
        "track_pref": track_pref,
        "age": 2,
        "wins": 0,
        "fatigue": 0,
        "train_lvl": 1,
        "grw_buff": 0,
        "history": [] # レース履歴
    }
    
    # オーナーの馬リストに追加
    owner["horses"].append(new_id)
    # 費用を差し引く
    owner["balance"] -= cost
    
    await save_data(data)
    
    await ctx.reply(
        f"🐎 **{name}** ({new_id}) を購入しました！\n"
        f"オーナー: {ctx.author.display_name}\n"
        f"初期ステータス: SP:{sp}, ST:{st}, CND:{cnd}, GRW:{grw}\n"
        f"適性: 距離={dist_pref}, トラック={track_pref}"
    )

@bot.command(name="hlist", help="所有馬リストを表示します")
async def hlist(ctx):
    data = await load_data()
    user_id = str(ctx.author.id)

    owner = data.get("owners", {}).get(user_id)
    if not owner or not owner["horses"]:
        await ctx.reply("所有馬はいません。!buyhorse で購入してください。")
        return

    table_data = []
    
    for horse_id in owner["horses"]:
        horse = data["horses"].get(horse_id)
        if horse:
            # Horse Name (ID) | Age | Wins | SP | ST | CND | GRW | Fatigue
            table_data.append([
                f"{horse['name']} ({horse_id})",
                horse.get("age", 2),
                horse.get("wins", 0),
                horse.get("SP", 0),
                horse.get("ST", 0),
                horse.get("CND", 0),
                horse.get("GRW", 0),
                horse.get("fatigue", 0)
            ])
            
    output = t2a(
        header=["馬名 (ID)", "歳", "勝", "SP", "ST", "CND", "GRW", "疲労"],
        body=table_data,
        style=PresetStyle.thin_border
    )
    
    await ctx.send(f"```\n{output}\n```")

@bot.command(name="status", help="馬の詳細ステータスを表示します: 例) !status H00001")
async def status(ctx, horse_id: str):
    data = await load_data()
    horse = data["horses"].get(horse_id)

    if not horse:
        await ctx.reply("指定された馬は存在しません。")
        return
        
    owner_id = horse["owner"]
    owner_user = bot.get_user(int(owner_id))
    owner_name = owner_user.display_name if owner_user else f"Unknown Owner ({owner_id})"

    embed = discord.Embed(
        title=f"🐎 {horse['name']} ({horse_id})",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="オーナー", value=owner_name, inline=True)
    embed.add_field(name="年齢", value=horse.get("age", 2), inline=True)
    embed.add_field(name="勝利数", value=horse.get("wins", 0), inline=True)
    
    embed.add_field(name="--- 基本ステータス ---", value="\u200b", inline=False)
    embed.add_field(name="SP (スピード)", value=horse.get("SP", 0), inline=True)
    embed.add_field(name="ST (スタミナ)", value=horse.get("ST", 0), inline=True)
    embed.add_field(name="CND (コンディション)", value=horse.get("CND", 0), inline=True)
    
    embed.add_field(name="GRW (成長係数)", value=horse.get("GRW", 0), inline=True)
    embed.add_field(name="疲労", value=horse.get("fatigue", 0), inline=True)
    embed.add_field(name="トレーニングLvl", value=horse.get("train_lvl", 1), inline=True)
    
    embed.add_field(name="--- 適性 ---", value="\u200b", inline=False)
    embed.add_field(name="距離適性", value=horse.get("dist_pref", "Medium"), inline=True)
    embed.add_field(name="トラック適性", value=horse.get("track_pref", "Turf"), inline=True)

    await ctx.send(embed=embed)
    
@bot.command(name="history", help="馬のレース履歴を表示します: 例) !history H00001")
async def racehistory(ctx, horse_id: str):
    data = await load_data()
    horse = data["horses"].get(horse_id)

    if not horse:
        await ctx.reply("指定された馬は存在しません。")
        return

    history = horse.get("history", [])
    if not history:
        await ctx.reply(f"馬 **{horse['name']}** のレース履歴はありません。")
        return

    msg_lines = [f"**🐎 {horse['name']} ({horse_id}) のレース履歴:**"]
    
    # データを最新のものから表示
    for entry in history[::-1]:
        date_str = f"{entry.get('year', '?')}年{entry.get('month', '?')}月{entry.get('day', '?')}日"
        line = f"・[{date_str}] {entry['race']}：**{entry['pos']}着**"
        if entry.get('prize', 0) > 0:
            line += f" (賞金: {entry['prize']:,}円)"
        msg_lines.append(line)

    await ctx.send("\n".join(msg_lines))
    
@bot.command(name="raceresults", help="過去のレース全結果を表示します: 例) !raceresults 2024 1 1")
async def raceresults(ctx, year: int = None, month: int = None, day: int = None):
    data = await load_data()
    
    # 年月日の指定がない場合は最新のレースを探す
    if year is None or month is None or day is None:
        last_race = data.get("races", [])[-1] if data.get("races") else None
        if not last_race:
            await ctx.reply("過去のレース結果がありません。")
            return
        year, month, day = last_race["year"], last_race["month"], last_race["day"]
        
    # 指定された年、月、日のレース結果を検索
    found_races = [
        r for r in data.get("races", []) 
        if r.get("year") == year and r.get("month") == month and r.get("day") == day
    ]

    if not found_races:
        await ctx.reply(f"{year}年{month}月{day}日のレース結果は見つかりませんでした。")
        return

    msg_lines = [f"**🗓️ {year}年{month}月{day}日のレース結果**"]
    
    for race_record in found_races:
        race_name = race_record["name"]
        distance = race_record["distance"]
        track = race_record["track"]
        results = race_record["results"]

        msg_lines.append(f"\n--- **{race_name}** ({distance}m {track}) ---")
        
        # 結果を順位順に表示
        table_data = []
        for entry in results:
            horse_id = entry["horse_id"]
            pos = entry["pos"]
            prize = entry["prize"]
            
            # 馬の情報を取得
            horse_data = data["horses"].get(horse_id)
            horse_name = horse_data["name"] if horse_data else "Unknown Horse"
            
            table_data.append([
                pos,
                horse_name,
                horse_id,
                f"{prize:,}" if prize > 0 else "-"
            ])
            
        output = t2a(
            header=["着順", "馬名", "ID", "賞金"],
            body=table_data,
            style=PresetStyle.thin_border
        )
        msg_lines.append(f"```\n{output}\n```")

    await ctx.send("\n".join(msg_lines))

@bot.command(name="balance", help="現在の所持金（賞金）を表示します")
async def balance(ctx):
    data = await load_data()
    user_id = str(ctx.author.id)

    owner = data.get("owners", {}).get(user_id)
    if not owner:
        await ctx.reply("オーナー登録を先に行ってください (!register)")
        return

    balance = owner.get("balance", 0)
    
    await ctx.reply(f"💰 **現在の所持金（賞金）**: {balance:,}円")

@bot.command(name="enter", help="次のレースに出走登録します: 例) !enter H00001")
async def enter(ctx, horse_id: str):
    data = await load_data()
    user_id = str(ctx.author.id)
    current_day = str(data["season"]["day"])

    horse = data["horses"].get(horse_id)
    if not horse or horse["owner"] != user_id:
        await ctx.reply("指定された馬は存在しないか、あなたが所有していません。")
        return

    # 既に登録されているかチェック
    entries = data.setdefault("pending_entries", {}).setdefault(current_day, [])
    if horse_id in entries:
        await ctx.reply(f"馬 **{horse['name']}** は既に出走登録されています。")
        return
        
    # 疲労チェック (疲労5以上は不可)
    if horse.get("fatigue", 0) >= 5:
        await ctx.reply(f"馬 **{horse['name']}** は疲労度が高い（{horse['fatigue']}）ため、出走登録できません。!train で休養させてください。")
        return

    entries.append(horse_id)
    await save_data(data)
    
    await ctx.reply(f"馬 **{horse['name']}** を本日のレースに出走登録しました！")

@bot.command(name="entrylist", help="本日の出走登録リストを表示します")
async def entrylist(ctx):
    data = await load_data()
    current_day = str(data["season"]["day"])
    entries = data.get("pending_entries", {}).get(current_day, [])

    if not entries:
        await ctx.reply("本日の出走登録はありません。")
        return
        
    race_info = get_race_info(data["season"]["year"], data["season"]["month"], data["season"]["day"])
    
    if not race_info:
        await ctx.reply("本日はレース開催日ではありません。")
        return
        
    # オッズ計算とリスト作成
    table_data = []
    
    for horse_id in entries:
        horse = data["horses"].get(horse_id)
        if horse:
            odds = calculate_odds(horse)
            table_data.append([
                horse_id,
                horse["name"],
                horse.get("age", 2),
                horse.get("wins", 0),
                odds
            ])
            
    # オッズ順にソート (低い方が人気)
    table_data.sort(key=lambda x: x[4])
            
    output = t2a(
        header=["ID", "馬名", "歳", "勝", "オッズ"],
        body=table_data,
        style=PresetStyle.thin_border
    )

    await ctx.send(
        f"**🏆 本日のレース: {race_info['name']} ({race_info['distance']}m {race_info['track']})**\n"
        f"```\n{output}\n```"
    )

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

    # 所持金チェックを data["owners"][user_id]["balance"] で行う
    owners = data.setdefault("owners", {})
    owner = owners.setdefault(user_id, {"balance": 0, "horses": [], "wins": 0})
    balance = owner.get("balance", 0)

    if amount <= 0:
        await ctx.reply("賭け金は 1 以上で指定してください。")
        return

    if balance < amount:
        await ctx.reply(f"所持金（賞金）が不足しています（現在: {balance:,}円）")
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
    # 所持金減算を data["owners"][user_id]["balance"] で行う
    owner["balance"] -= amount

    await save_data(data)

    payout = int(amount * odds_val)

    await ctx.reply(
        f"🎫 **賭けを受け付けました！**\n"
        f"馬名: {horse['name']}\n"
        f"賭け金: {amount:,}円\n"
        f"オッズ: {odds_val} 倍\n"
        f"的中時の払戻: {payout:,}円"
    )

@bot.command(name="train", help="所有馬を休養させ、疲労を回復させます: 例) !train H00001")
async def train(ctx, horse_id: str):
    data = await load_data()
    user_id = str(ctx.author.id)

    horse = data["horses"].get(horse_id)
    if not horse or horse["owner"] != user_id:
        await ctx.reply("指定された馬は存在しないか、あなたが所有していません。")
        return
        
    # コスト
    cost = 5000
    owner = data["owners"].get(user_id)
    if owner["balance"] < cost:
        await ctx.reply(f"所持金が不足しています（費用: {cost}円）")
        return

    # 疲労を半分にする
    old_fatigue = horse.get("fatigue", 0)
    new_fatigue = max(0, old_fatigue // 2)

    # GRWバフを付与 (次回レースのスコアに加算)
    grw_value = horse.get("GRW", 0)
    # GRWが高いほど、バフ量も大きい (例: GRW=100ならバフ+10)
    grw_buff = int(grw_value * 0.1) 
    
    horse["fatigue"] = new_fatigue
    horse["grw_buff"] = grw_buff
    owner["balance"] -= cost # 費用を差し引く
    
    await save_data(data)

    await ctx.reply(
        f"🐴 **{horse['name']}** を訓練/休養させました！\n"
        f"疲労度: {old_fatigue} -> {new_fatigue}\n"
        f"次回レースで成長係数バフ (+{grw_buff}) が適用されます。"
    )

@bot.command(name="setchannel", help="レース結果を通知するチャンネルを設定します（管理者専用）")
async def setchannel(ctx):
    if str(ctx.author.id) != BOT_OWNER_ID:
        await ctx.reply("このコマンドはボットオーナーのみ実行可能です。")
        return

    data = await load_data()
    data["announce_channel"] = ctx.channel.id
    await save_data(data)
    await ctx.reply(f"このチャンネル（**{ctx.channel.name}**）をレース結果通知チャンネルに設定しました。")

@bot.command(name="time", help="現在のゲーム内日付と次のレースまでの時間を表示します")
async def game_time(ctx):
    data = await load_data()
    
    year = data["season"]["year"]
    month = data["season"]["month"]
    day = data["season"]["day"]
    
    is_ready, time_left, next_y, next_m, next_d = get_next_race_date(data)
    
    race_info = get_race_info(year, month, day)
    race_status = "❌ レースなし"
    if race_info:
        race_status = f"✅ 本日開催: **{race_info['name']}** ({race_info['distance']}m {race_info['track']})"

    await ctx.reply(
        f"**🗓️ 現在のゲーム内日付**: {year}年{month}月{day}日\n"
        f"{race_status}\n"
        f"⏰ **次のレース実行まで**: {time_left}"
    )

# ---------------- 自動実行タスク ----------------

async def run_race_and_advance_day():
    data = await load_data()
    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]
    current_day_str = str(current_day)
    channel_id = data["announce_channel"]

    # チャンネルの取得
    channel = bot.get_channel(channel_id) if channel_id else None
    
    # レース情報の取得
    race_info = get_race_info(current_year, current_month, current_day)
    
    if not race_info:
        # レースがない日の場合、日付のみ進める
        await advance_day(data)
        return # レースがないのでここで終了

    # 出走登録馬の取得
    entry_ids = data.get("pending_entries", {}).get(current_day_str, [])
    
    # ボットが所有するダミー馬を追加（最低6頭にする）
    while len(entry_ids) < 6:
        # 適当なダミー馬IDとデータ
        dummy_id = f"BOT{len(entry_ids):02d}"
        data["horses"][dummy_id] = {
            "id": dummy_id,
            "name": f"CPUホース{len(entry_ids)}",
            "owner": BOT_OWNER_ID,
            "SP": random.randint(100, 200),
            "ST": random.randint(100, 200),
            "CND": random.randint(100, 200),
            "GRW": 100,
            "dist_pref": random.choice(["Short", "Mile", "Medium", "Long"]),
            "track_pref": random.choice(["Turf", "Dirt"]),
            "age": random.randint(3, 5),
            "wins": 0,
            "fatigue": 0,
            "train_lvl": 1,
            "grw_buff": 0,
            "history": []
        }
        entry_ids.append(dummy_id)

    all_entries = []
    
    for horse_id in entry_ids:
        horse = data["horses"].get(horse_id)
        if horse:
            # レーススコアの計算
            score = calculate_race_score(
                horse, 
                race_info["distance"], 
                race_info["track"]
            )
            
            # GRWバフの消費 (使用したら0に戻す)
            horse["grw_buff"] = 0
            
            # エントリー情報
            all_entries.append({
                "horse_id": horse_id,
                "name": horse["name"],
                "owner": horse["owner"],
                "score": score
            })

    # ------------------ レース実行ロジック ------------------
    
    # 疲労増加と年齢上昇の準備
    for horse_id in entry_ids:
        horse = data["horses"].get(horse_id)
        if horse:
            if horse["owner"] != BOT_OWNER_ID:
                # 疲労増加 (レース出走で+2)
                horse["fatigue"] = min(10, horse.get("fatigue", 0) + 2)
                # 成長処理
                progress_growth(horse)
            
    # スコアでソートし、順位を決定
    all_entries.sort(key=lambda x: x["score"], reverse=True)
    
    winner_id = all_entries[0]["horse_id"]
    
    results = []
   # レース名に応じて賞金プールを決定
    is_g1_race = is_g1(race_info['name'])
    prize_config = prize_pool_for_g1(race_info['name']) if is_g1_race else prize_pool_for_lower()
    
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
            owners = data.setdefault("owners", {})
            if owner_id not in owners:
                 owners[owner_id] = {"horses": [], "balance": 0, "wins": 0}

            # 賞金の加算 (data["owners"][owner_id]["balance"] を使用)
            owners[owner_id]["balance"] = owners[owner_id].get("balance", 0) + prize
            
            if pos == 1:
                # 馬の勝利数
                data["horses"][entry["horse_id"]]["wins"] = data["horses"][entry["horse_id"]].get("wins", 0) + 1
                # オーナーの勝利数
                owners[owner_id]["wins"] = owners[owner_id].get("wins", 0) + 1
                
        # レース履歴の追加 (オーナー馬のみ)
        if entry["owner"] != BOT_OWNER_ID:
             data["horses"][entry["horse_id"]]["history"].append({
                 "race": race_info["name"],
                 "pos": pos,
                 "prize": prize,
                 # racehistoryが!raceresultsと互換性を持つよう、年月日を分割して保存
                 "year": current_year,
                 "month": current_month,
                 "day": current_day
             })

    # ★ 修正: !raceresults のためにレース結果全体を data["races"] に保存する
    race_record = {
        "year": current_year,
        "month": current_month,
        "day": current_day,
        "name": race_info["name"],
        "distance": race_info["distance"],
        "track": race_info["track"],
        "results": results # 全ての順位の結果を含む
    }
    data["races"].append(race_record)

    # 賭けの精算 (data["owners"]["uid"]["balance"] を使用)
    bets = data.get("bets", {}).get(current_day_str, {})
    
    bet_payouts = {}
    
    for uid, b in bets.items():
        if b["horse_id"] == winner_id:
            payout = int(b["amount"] * b["odds"])
            # data["owners"] を使用して残高を更新
            owners = data.setdefault("owners", {})
            owner = owners.setdefault(uid, {"balance": 0, "horses": [], "wins": 0})
            owner["balance"] += payout
            bet_payouts[uid] = payout
            
    # 賭けデータのリセット
    if current_day_str in data["bets"]:
        del data["bets"][current_day_str]
        

    # ------------------ 結果告知とデータ更新 ------------------

    # 結果を整形
    race_result_msg = [
        f"🎉 **レース結果: {race_info['name']}** ({race_info['distance']}m {race_info['track']}) - {current_year}年{current_month}月{current_day}日",
        "```"
    ]
    
    # 順位表
    table_data = []
    for entry in all_entries:
        owner_name = "CPU"
        if entry['owner'] != BOT_OWNER_ID:
            owner_user = bot.get_user(int(entry['owner']))
            owner_name = owner_user.display_name if owner_user else f"Owner ({entry['owner']})"

        table_data.append([
            entry['pos'],
            entry['name'],
            owner_name,
            f"{entry['prize']:,}" if entry['prize'] > 0 else "-"
        ])

    race_result_msg.append(
        t2a(
            header=["着順", "馬名", "オーナー", "賞金"],
            body=table_data,
            style=PresetStyle.thin_border
        )
    )
    race_result_msg.append("```")
    
    # 賭けの精算結果
    if bet_payouts:
        bet_msg = ["\n**💰 賭けの精算**"]
        for uid, payout in bet_payouts.items():
            user = bot.get_user(int(uid))
            user_name = user.display_name if user else f"User ({uid})"
            bet_msg.append(f"・{user_name}: **{payout:,}円** の払戻し")
        race_result_msg.extend(bet_msg)
        
    # 告知チャンネルへ送信
    if channel:
        await channel.send("\n".join(race_result_msg))
    else:
        print("Warning: Announce channel not set.")

    # レース後の処理（日付進行と引退判定）
    await advance_day(data)
    
async def advance_day(data):
    """日付を進め、引退判定を行う"""
    
    current_day = data["season"]["day"]
    current_month = data["season"]["month"]
    current_year = data["season"]["year"]

    # 昨日の出走登録を削除
    if str(current_day) in data["pending_entries"]:
        del data["pending_entries"][str(current_day)]
        
    # 日付を進める
    current_day += 1
    
    # 月末判定
    days_in_month = calendar.monthrange(current_year, current_month)[1]
    if current_day > days_in_month:
        current_day = 1
        current_month += 1
        
    # 年末判定
    if current_month > 12:
        current_month = 1
        current_year += 1
        
        # 年度が変わったら年齢を上げる
        for horse_id, horse in data["horses"].items():
            horse["age"] = horse.get("age", 2) + 1

    data["season"]["day"] = current_day
    data["season"]["month"] = current_month
    data["season"]["year"] = current_year
    
    # ------------------ 引退判定 ------------------
    
    horses_to_retire_info = []
    
    for horse_id, horse in list(data["horses"].items()):
        # BOT所有馬は引退させない
        if horse["owner"] == BOT_OWNER_ID:
            continue
            
        should_retire = False
        
        # 1. 勝利数0で5歳以上
        if horse.get("age", 0) >= 5 and horse.get("wins", 0) == 0:
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

    # 最終更新時刻を保存
    data["last_race_time"] = datetime.now(timezone.utc).isoformat()
    
    await save_data(data)
    
@tasks.loop(minutes=1)
async def check_for_race():
    """1分ごとにレース実行時刻をチェックする"""
    await bot.wait_until_ready()
    
    # bot.is_ready() の代わりに、データのロードを試みる
    try:
        data = await load_data()
    except Exception:
        # DB接続エラーなどでロードできなかった場合はスキップ
        return

    # 次のレースまでの時間をチェック
    is_ready, _, _, _, _ = get_next_race_date(data)
    
    if is_ready:
        print(f"[{datetime.now()}] Race time reached. Running race and advancing day...")
        await run_race_and_advance_day()
    else:
        # レースがない日の場合は、単に日付を進めるチェックのみ行う
        current_day = data["season"]["day"]
        current_month = data["season"]["month"]
        current_year = data["season"]["year"]
        
        # レースがない日かどうかチェック
        race_info = get_race_info(current_year, current_month, current_day)
        
        if not race_info and (datetime.fromisoformat(data.get("last_race_time", datetime.now(timezone.utc).isoformat())).replace(tzinfo=timezone.utc) + timedelta(hours=data["config"].get("race_interval_hours", 24)) <= datetime.now(timezone.utc)):
            print(f"[{datetime.now()}] Advance day on non-race day...")
            await advance_day(data)
            
@bot.event
async def on_ready():
    print(f"Bot is ready. Logged in as {bot.user}")
    print(f"Starting check_for_race loop...")
    if not check_for_race.is_running():
        check_for_race.start()

# ---------------- メイン処理 ----------------

# Flaskを別スレッドで実行
threading.Thread(target=run_flask).start()

# Botの実行
# TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
# if TOKEN:
#     bot.run(TOKEN)
# else:
#     print("DISCORD_BOT_TOKEN not found in environment variables.")

# Heroku/Render などのプラットフォームに合わせて実行
try:
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if not TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment variables.")
    bot.run(TOKEN)
except ValueError as e:
    print(f"Error: {e}")
except discord.errors.LoginFailure:
    print("Error: Invalid Discord token.")
except Exception as e:
    print(f"An unexpected error occurred: {e}")
