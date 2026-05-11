# “””
Daily Stock Signal System v5

Repository: github.com/ryanhsu1983/AI_stock_0050
v5 updates:

- 100-point weighted scoring system
- 4-level signals (notice/weak/mid/strong)
- TWSE institutional investors crawler with validation
- Independent buy/sell scoring
  “””

import json, os, smtplib, time, requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ══════════════════════════════════════════════════════════════

# 權重定義（買進權重 / 賣出權重）

# ══════════════════════════════════════════════════════════════

WEIGHTS = {
“trend”:       25,   # 趨勢環境（均線排列）
“macd”:        20,   # MACD 動能轉折
“institutional”: 15, # 三大法人買賣超
“kd”:          15,   # KD 交叉
“obv”:         10,   # OBV 量價關係
“bias20”:      10,   # 短線乖離率
“vol”:          5,   # 量能趨勢
}

# BIAS60 Z-Score 不計入分數，但過熱時鎖定所有買進

# 四級訊號門檻

SIGNAL_LEVELS = [
(70, “STRONG”,  “🔴”, “強訊號”),
(50, “MID”,     “🟠”, “中訊號”),
(30, “WEAK”,    “🟡”, “弱訊號”),
(15, “NOTICE”,  “🔵”, “提醒”),
( 0, “NEUTRAL”, “⚪”, “無訊號”),
]

# ══════════════════════════════════════════════════════════════

# 設定載入

# ══════════════════════════════════════════════════════════════

def load_config() -> dict:
with open(Path(**file**).parent / “config.json”, “r”, encoding=“utf-8”) as f:
return json.load(f)

def get_stock_cfg(stock: dict, global_cfg: dict) -> dict:
ov  = stock.get(“overrides”, {})
thr = dict(global_cfg[“thresholds”])
ma  = dict(global_cfg[“ma_periods”])
for key in (“kd_buy”,“kd_sell”,“bias20_buy”,“bias20_sell”,
“bias60_p_low”,“bias60_p_high”,“vol_ma_period”,“obv_ma_period”):
if key in ov:
thr[key] = ov[key]
if “bias_buy”  in thr and “bias20_buy”  not in thr: thr[“bias20_buy”]  = thr[“bias_buy”]
if “bias_sell” in thr and “bias20_sell” not in thr: thr[“bias20_sell”] = thr[“bias_sell”]
if “ma_periods” in ov:
ma.update(ov[“ma_periods”])
return {
“thresholds”:       thr,
“ma_periods”:       ma,
“pyramid”:          global_cfg.get(“pyramid”, {}),
“use_obv”:          ov.get(“use_obv”,          True),
“use_vol_trend”:    ov.get(“use_vol_trend”,     True),
“use_institutional”:ov.get(“use_institutional”, True),
“leverage_warning”: ov.get(“leverage_warning”,  False),
“bias60_locked”:    ov.get(“bias60_locked”,     True),
}

# ══════════════════════════════════════════════════════════════

# 三大法人爬蟲

# ══════════════════════════════════════════════════════════════

def fetch_institutional(ticker_raw: str) -> dict:
“””
從台灣證交所抓取三大法人當日買賣超。
回傳 dict：
success      : bool
foreign_net  : 外資買賣超（張）
invest_net   : 投信買賣超（張）
dealer_net   : 自營商買賣超（張）
total_net    : 三大合計（張）
error        : 錯誤訊息（success=False 時）
“””
# 標準化股票代號（去掉 .TW）
stock_id = ticker_raw.upper().replace(”.TW”, “”).replace(”.TWO”, “”)

```
try:
    today_str = datetime.today().strftime("%Y%m%d")
    url = (f"https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?response=json&date={today_str}&selectType=ALLBUT0999")

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://www.twse.com.tw/",
    }

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # ── 格式驗證 ──────────────────────────────────────────
    if data.get("stat") != "OK":
        return {"success": False, "error": f"證交所回傳狀態非OK：{data.get('stat')}"}

    fields = data.get("fields", [])
    rows   = data.get("data",   [])

    required_fields = ["證券代號", "外陸資買賣超股數", "投信買賣超股數", "自營商買賣超股數"]
    for f in required_fields:
        if f not in fields:
            return {"success": False,
                    "error": f"欄位格式異動，缺少：{f}｜目前欄位：{fields[:6]}"}

    if not rows:
        return {"success": False, "error": "今日無三大法人資料（可能為非交易日）"}

    # ── 找目標股票 ────────────────────────────────────────
    idx_id      = fields.index("證券代號")
    idx_foreign = fields.index("外陸資買賣超股數")
    idx_invest  = fields.index("投信買賣超股數")
    idx_dealer  = fields.index("自營商買賣超股數")

    target_row = None
    for row in rows:
        if str(row[idx_id]).strip() == stock_id:
            target_row = row
            break

    if target_row is None:
        return {"success": False,
                "error": f"資料中找不到 {stock_id}（可能為ETF或上櫃股票，需另外查詢）"}

    def parse_num(s):
        try:
            return int(str(s).replace(",", "").replace(" ", ""))
        except:
            return 0

    foreign = parse_num(target_row[idx_foreign])
    invest  = parse_num(target_row[idx_invest])
    dealer  = parse_num(target_row[idx_dealer])
    total   = foreign + invest + dealer

    # 單位轉換：股 → 張（1張=1000股）
    return {
        "success":     True,
        "foreign_net": foreign // 1000,
        "invest_net":  invest  // 1000,
        "dealer_net":  dealer  // 1000,
        "total_net":   total   // 1000,
        "error":       "",
    }

except requests.exceptions.Timeout:
    return {"success": False, "error": "證交所請求逾時（15秒）"}
except requests.exceptions.ConnectionError:
    return {"success": False, "error": "無法連線至證交所，請確認網路"}
except Exception as e:
    return {"success": False, "error": f"未預期錯誤：{str(e)[:80]}"}
```

# ══════════════════════════════════════════════════════════════

# 資料抓取

# ══════════════════════════════════════════════════════════════

def fetch_data(ticker: str, days: int) -> pd.DataFrame:
end   = datetime.today()
start = end - timedelta(days=days)
df = yf.download(ticker,
start=start.strftime(”%Y-%m-%d”),
end=end.strftime(”%Y-%m-%d”),
progress=False, auto_adjust=True)
if df.empty:
raise ValueError(f”無法取得 {ticker} 資料”)
df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
return df[[“Open”,“High”,“Low”,“Close”,“Volume”]].dropna()

# ══════════════════════════════════════════════════════════════

# 指標計算

# ══════════════════════════════════════════════════════════════

def calc_indicators(df: pd.DataFrame, scfg: dict) -> pd.DataFrame:
ma  = scfg[“ma_periods”]
thr = scfg[“thresholds”]
s, m, l = ma[“short”], ma[“mid”], ma[“long”]

```
df[f"MA{s}"] = df["Close"].rolling(s).mean()
df[f"MA{m}"] = df["Close"].rolling(m).mean()
df[f"MA{l}"] = df["Close"].rolling(l).mean()

ma60         = df["Close"].rolling(60).mean()
df["BIAS60"] = (df["Close"] - ma60) / ma60 * 100
b60_clean    = df["BIAS60"].dropna()
p_low        = thr.get("bias60_p_low",  5)
p_high       = thr.get("bias60_p_high", 95)
df.attrs["bias60_p_high"] = float(b60_clean.quantile(p_high / 100))
df.attrs["bias60_p_low"]  = float(b60_clean.quantile(p_low  / 100))
df.attrs["bias60_mean"]   = float(b60_clean.mean())
df.attrs["bias60_std"]    = float(b60_clean.std())
df["BIAS60_Z"] = (df["BIAS60"] - df.attrs["bias60_mean"]) / df.attrs["bias60_std"]

df["Bias20"] = (df["Close"] - df[f"MA{m}"]) / df[f"MA{m}"] * 100

low_min  = df["Low"].rolling(9).min()
high_max = df["High"].rolling(9).max()
rsv      = (df["Close"] - low_min) / (high_max - low_min) * 100
df["K"]  = rsv.ewm(com=2, adjust=False).mean()
df["D"]  = df["K"].ewm(com=2, adjust=False).mean()

ema12           = df["Close"].ewm(span=12, adjust=False).mean()
ema26           = df["Close"].ewm(span=26, adjust=False).mean()
df["DIF"]       = ema12 - ema26
df["Signal"]    = df["DIF"].ewm(span=9, adjust=False).mean()
df["MACD_hist"] = df["DIF"] - df["Signal"]

vp              = thr["vol_ma_period"]
df["Vol_MA"]    = df["Volume"].rolling(vp).mean()
df["Vol_Trend"] = df["Vol_MA"] - df["Vol_MA"].shift(3)

obv = [0]
for i in range(1, len(df)):
    if   df["Close"].iloc[i] > df["Close"].iloc[i-1]: obv.append(obv[-1] + df["Volume"].iloc[i])
    elif df["Close"].iloc[i] < df["Close"].iloc[i-1]: obv.append(obv[-1] - df["Volume"].iloc[i])
    else:                                               obv.append(obv[-1])
df["OBV"]    = obv
df["OBV_MA"] = df["OBV"].rolling(thr["obv_ma_period"]).mean()

return df
```

# ══════════════════════════════════════════════════════════════

# BIAS60 評估

# ══════════════════════════════════════════════════════════════

def eval_bias60(df: pd.DataFrame, scfg: dict) -> dict:
latest   = df.iloc[-1]
bias60   = float(latest[“BIAS60”])
z        = float(latest[“BIAS60_Z”])
p_high   = df.attrs[“bias60_p_high”]
p_low    = df.attrs[“bias60_p_low”]
ph_pct   = scfg[“thresholds”].get(“bias60_p_high”, 95)
pl_pct   = scfg[“thresholds”].get(“bias60_p_low”,   5)
can_lock = scfg.get(“bias60_locked”, True)

```
if bias60 >= p_high:
    zone, locked = "overheated", can_lock
    label = f"🔥 過熱{'鎖定' if can_lock else '警示'}（季線乖離{bias60:.1f}%，歷史{ph_pct}%分位）"
    color = "#c0392b"
    note  = (f"當前季線乖離={bias60:.1f}%｜"
             f"歷史{ph_pct}%分位門檻={p_high:.1f}%｜"
             f"Z-Score={z:.2f}（>2代表極端過熱）｜"
             f"{'強制禁止買進，等待回落後再評估' if can_lock else '僅警示，不鎖定（槓桿ETF特殊處理）'}")
elif bias60 <= p_low:
    zone, locked = "oversold", False
    label = f"❄️ 超跌部署區（季線乖離{bias60:.1f}%，歷史{pl_pct}%分位）"
    color = "#2980b9"
    note  = (f"當前季線乖離={bias60:.1f}%｜"
             f"歷史{pl_pct}%分位門檻={p_low:.1f}%｜"
             f"Z-Score={z:.2f}（<-2代表極端超跌）｜"
             f"統計黃金建倉區，搭配技術面確認後可積極布局")
else:
    zone, locked = "normal", False
    label = f"正常範圍（季線乖離{bias60:.1f}%）"
    color = "#95a5a6"
    note  = (f"當前季線乖離={bias60:.1f}%｜"
             f"正常區間={p_low:.1f}%～{p_high:.1f}%｜"
             f"Z-Score={z:.2f}｜"
             f"介於歷史{pl_pct}%～{ph_pct}%分位之間，無極端訊號")

return dict(zone=zone, locked=locked, bias60=bias60,
            z_score=z, p_high=p_high, p_low=p_low,
            label=label, color=color, note=note)
```

# ══════════════════════════════════════════════════════════════

# 訊號等級判斷

# ══════════════════════════════════════════════════════════════

def score_to_level(score: float) -> tuple:
“”“回傳 (level_key, emoji, label)”””
for threshold, key, emoji, label in SIGNAL_LEVELS:
if score >= threshold:
return key, emoji, label
return “NEUTRAL”, “⚪”, “無訊號”

# ══════════════════════════════════════════════════════════════

# 主評估函式

# ══════════════════════════════════════════════════════════════

def evaluate(df: pd.DataFrame, scfg: dict, inst: dict) -> dict:
thr      = scfg[“thresholds”]
ma       = scfg[“ma_periods”]
use_obv  = scfg.get(“use_obv”,          True)
use_vol  = scfg.get(“use_vol_trend”,     True)
use_inst = scfg.get(“use_institutional”, True)
lev_warn = scfg.get(“leverage_warning”,  False)
s, m, l  = ma[“short”], ma[“mid”], ma[“long”]

```
latest = df.iloc[-1]
prev   = df.iloc[-2]

close     = float(latest["Close"])
ma_s      = float(latest[f"MA{s}"])
ma_m      = float(latest[f"MA{m}"])
ma_l      = float(latest[f"MA{l}"])
ma_s_prev = float(prev[f"MA{s}"])
ma_m_prev = float(prev[f"MA{m}"])
ma_l_prev = float(prev[f"MA{l}"])
k, d      = float(latest["K"]),        float(latest["D"])
kp, dp    = float(prev["K"]),          float(prev["D"])
hist      = float(latest["MACD_hist"])
hist_p    = float(prev["MACD_hist"])
bias20    = float(latest["Bias20"])
vol       = float(latest["Volume"])
vol_ma    = float(latest["Vol_MA"])
vol_trend = float(latest["Vol_Trend"])
obv       = float(latest["OBV"])
obv_ma    = float(latest["OBV_MA"])
obv_prev  = float(prev["OBV"])
open_p    = float(latest["Open"])

items        = []   # (label, value, color, note, buy_score, sell_score)
total_buy    = 0.0
total_sell   = 0.0
max_possible = sum(WEIGHTS.values())

# ── 槓桿警示 ──────────────────────────────────────────────
if lev_warn:
    items.append(("⚠️ 槓桿警示", "每日重置ETF，不適合長抱", "#e67e22",
                  "槓桿ETF有長期耗損效應｜"
                  "震盪市場耗損比預期嚴重｜"
                  "僅適合趨勢明確時短線波段操作｜"
                  "趨勢不明時請勿持有", 0, 0))

# ── BIAS60 Z-Score（不計分，但可鎖定）────────────────────
b60 = eval_bias60(df, scfg)
items.append(("BIAS60 Z-Score", b60["label"], b60["color"], b60["note"], 0, 0))

# ── 1. 趨勢環境（權重25）─────────────────────────────────
w = WEIGHTS["trend"]
ma_s_dir   = ma_s > ma_s_prev
above_ma_s = close > ma_s
ma_bull_cross = ma_m > ma_l and ma_m_prev <= ma_l_prev
ma_bear_cross = ma_m < ma_l and ma_m_prev >= ma_l_prev

if ma_m > ma_l and above_ma_s and ma_s_dir:
    trend = "healthy_bull"
    buy_s, sell_s = w, 0
elif ma_m > ma_l and (not above_ma_s or not ma_s_dir):
    trend = "weak_bull"
    buy_s, sell_s = w * 0.3, 0
elif ma_m < ma_l:
    trend = "bear"
    buy_s, sell_s = 0, w
else:
    trend = "neutral"
    buy_s, sell_s = 0, 0

trend_label = {"healthy_bull":"多頭健康","weak_bull":"多頭轉弱",
               "bear":"空頭確認","neutral":"方向不明"}[trend]
trend_color = {"healthy_bull":"#2ecc71","weak_bull":"#f39c12",
               "bear":"#e74c3c","neutral":"#95a5a6"}[trend]
total_buy  += buy_s
total_sell += sell_s
items.append(("趨勢環境", f"{trend_label}（+{buy_s:.0f}分）" if buy_s > 0
              else f"{trend_label}（+{sell_s:.0f}分賣）" if sell_s > 0
              else trend_label,
              trend_color,
              f"MA{s}={ma_s:.1f}｜MA{m}={ma_m:.1f}｜MA{l}={ma_l:.1f}｜"
              f"收盤{'站上' if above_ma_s else '跌破'}{s}日線（{s}日線{'向上' if ma_s_dir else '向下'}）｜"
              f"多頭健康=滿分{w}分｜多頭轉弱={w*0.3:.0f}分｜空頭={w}分（賣出）｜"
              f"{'⚡ 均線剛發生交叉！' if ma_bull_cross or ma_bear_cross else ''}",
              buy_s, sell_s))

# ── 2. MACD（權重20）─────────────────────────────────────
w = WEIGHTS["macd"]
hist_series = df["MACD_hist"].dropna()
hist_p10    = float(hist_series.quantile(0.10))
hist_p90    = float(hist_series.quantile(0.90))

if hist > 0 and hist_p <= 0:
    macd_buy, macd_sell = w, 0
    macd_val = f"柱狀由負翻正 ✅（+{w}分）"
    macd_col = "#2ecc71"
elif hist < 0 and hist_p >= 0:
    macd_buy, macd_sell = 0, w
    macd_val = f"柱狀由正翻負 ⚠️（+{w}分賣）"
    macd_col = "#e74c3c"
elif hist > 0:
    macd_buy, macd_sell = w * 0.3, 0
    macd_val = f"柱狀持續為正（+{w*0.3:.0f}分）"
    macd_col = "#95a5a6"
elif hist < 0:
    macd_buy, macd_sell = 0, w * 0.3
    macd_val = f"柱狀持續為負（+{w*0.3:.0f}分賣）"
    macd_col = "#95a5a6"
else:
    macd_buy, macd_sell = 0, 0
    macd_val, macd_col = "MACD歸零", "#95a5a6"

total_buy  += macd_buy
total_sell += macd_sell
items.append(("MACD", macd_val, macd_col,
              f"當前柱狀值={hist:.4f}｜"
              f"歷史正常區間：{hist_p10:.4f}～{hist_p90:.4f}｜"
              f"翻轉時滿分{w}分｜持續同向{w*0.3:.0f}分｜"
              f"正=多頭動能｜負=空頭動能｜由負翻正為最強買進訊號",
              macd_buy, macd_sell))

# ── 3. 三大法人（權重15）─────────────────────────────────
w = WEIGHTS["institutional"]
inst_buy = inst_sell = 0

if not use_inst:
    inst_note = "已關閉（槓桿ETF不適用）"
    inst_val  = "已關閉"
    inst_col  = "#bdc3c7"
    inst_detail = "槓桿ETF成交量結構特殊，三大法人數據不具代表性"
elif not inst.get("success", False):
    inst_note = inst.get("error", "資料取得失敗")
    inst_val  = f"⚠️ 資料異常"
    inst_col  = "#f39c12"
    inst_detail = (f"三大法人資料今日無法取得，已略過不計分｜"
                   f"原因：{inst.get('error', '未知')}｜"
                   f"其他指標不受影響")
else:
    total_net   = inst.get("total_net",   0)
    foreign_net = inst.get("foreign_net", 0)
    invest_net  = inst.get("invest_net",  0)
    dealer_net  = inst.get("dealer_net",  0)

    if total_net > 0:
        # 依買超規模給分（超過500張給滿分，100-500張給半分）
        if abs(total_net) >= 500:
            inst_buy, inst_sell = w, 0
        else:
            inst_buy, inst_sell = w * 0.5, 0
        inst_val = f"三大法人買超 {total_net:+,}張 ✅（+{inst_buy:.0f}分）"
        inst_col = "#2ecc71"
    elif total_net < 0:
        if abs(total_net) >= 500:
            inst_buy, inst_sell = 0, w
        else:
            inst_buy, inst_sell = 0, w * 0.5
        inst_val = f"三大法人賣超 {total_net:+,}張 ⚠️（+{inst_sell:.0f}分賣）"
        inst_col = "#e74c3c"
    else:
        inst_val = "三大法人中立（0分）"
        inst_col = "#95a5a6"

    inst_detail = (f"外資={foreign_net:+,}張｜"
                   f"投信={invest_net:+,}張｜"
                   f"自營商={dealer_net:+,}張｜"
                   f"合計={total_net:+,}張｜"
                   f"≥500張給滿分{w}分｜100-499張給{w*0.5:.0f}分｜"
                   f"買超=多頭籌碼集中｜賣超=大戶出場訊號")

total_buy  += inst_buy
total_sell += inst_sell
items.append(("三大法人", inst_val, inst_col, inst_detail, inst_buy, inst_sell))

# ── 4. KD（權重15）───────────────────────────────────────
w = WEIGHTS["kd"]
kd_buy_signal  = k > d and kp <= dp and k < thr["kd_buy"]
kd_sell_signal = k < d and kp >= dp and k > thr["kd_sell"]

if kd_buy_signal:
    kd_buy_s, kd_sell_s = w, 0
    kd_val = f"低檔黃金交叉 ✅（+{w}分）"
    kd_col = "#2ecc71"
elif kd_sell_signal:
    kd_buy_s, kd_sell_s = 0, w
    kd_val = f"高檔死亡交叉 ⚠️（+{w}分賣）"
    kd_col = "#e74c3c"
elif k < thr["kd_buy"]:
    kd_buy_s, kd_sell_s = w * 0.3, 0
    kd_val = f"低檔區尚未交叉（+{w*0.3:.0f}分）"
    kd_col = "#3498db"
elif k > thr["kd_sell"]:
    kd_buy_s, kd_sell_s = 0, w * 0.3
    kd_val = f"高檔區尚未交叉（+{w*0.3:.0f}分賣）"
    kd_col = "#f39c12"
else:
    kd_buy_s, kd_sell_s = 0, 0
    kd_val = "中性區間（0分）"
    kd_col = "#95a5a6"

total_buy  += kd_buy_s
total_sell += kd_sell_s
items.append(("KD", kd_val, kd_col,
              f"當前 K={k:.1f}｜D={d:.1f}｜"
              f"買進區門檻：K<{thr['kd_buy']}且K上穿D=滿分{w}分｜"
              f"賣出區門檻：K>{thr['kd_sell']}且K下穿D=滿分{w}分｜"
              f"在買賣區間內但尚未交叉={w*0.3:.0f}分｜"
              f"正常區間（{thr['kd_buy']}～{thr['kd_sell']}）=0分",
              kd_buy_s, kd_sell_s))

# ── 5. OBV（權重10）──────────────────────────────────────
w = WEIGHTS["obv"]
obv_buy_s = obv_sell_s = 0

if use_obv:
    obv_rising  = obv > obv_ma and obv > obv_prev
    obv_falling = obv < obv_ma and obv < obv_prev
    price_up    = close > float(prev["Close"])

    if obv_rising and price_up:
        obv_buy_s = w
        obv_val, obv_col = f"量價齊揚 ✅（+{w}分）", "#2ecc71"
    elif obv_rising and not price_up:
        obv_buy_s = w * 0.6
        obv_val, obv_col = f"OBV領先價格 💡（+{w*0.6:.0f}分）", "#3498db"
    elif obv_falling and not price_up:
        obv_sell_s = w
        obv_val, obv_col = f"量價齊跌 ⚠️（+{w}分賣）", "#e74c3c"
    elif obv_falling and price_up:
        obv_sell_s = w * 0.6
        obv_val, obv_col = f"價漲量縮背離 ⚠️（+{w*0.6:.0f}分賣）", "#f39c12"
    else:
        obv_val, obv_col = "OBV中性（0分）", "#95a5a6"

    obv_detail = (f"OBV={'高於' if obv>obv_ma else '低於'}{thr['obv_ma_period']}日均線｜"
                  f"量價齊揚=滿分{w}分｜OBV領先={w*0.6:.0f}分｜"
                  f"量價齊跌=賣出{w}分｜價漲量縮背離=賣出{w*0.6:.0f}分｜"
                  f"OBV持續累積=買盤入場訊號｜OBV領先價格=預警買訊")
else:
    obv_val     = "已關閉（槓桿ETF不適用）"
    obv_col     = "#bdc3c7"
    obv_detail  = "槓桿ETF成交量結構特殊，OBV訊號不具參考價值"

total_buy  += obv_buy_s
total_sell += obv_sell_s
items.append(("OBV", obv_val, obv_col, obv_detail, obv_buy_s, obv_sell_s))

# ── 6. 乖離率MA20（權重10）───────────────────────────────
w = WEIGHTS["bias20"]
b20_buy  = thr.get("bias20_buy",  thr.get("bias_buy",  -4.0))
b20_sell = thr.get("bias20_sell", thr.get("bias_sell",  5.0))

if bias20 < b20_buy:
    b20_buy_s, b20_sell_s = w, 0
    b20_val = f"跌深反彈機會 ✅（+{w}分）"
    b20_col = "#2ecc71"
elif bias20 > b20_sell:
    b20_buy_s, b20_sell_s = 0, w
    b20_val = f"漲幅過高警示 ⚠️（+{w}分賣）"
    b20_col = "#e74c3c"
else:
    b20_buy_s, b20_sell_s = 0, 0
    b20_val = "正常範圍（0分）"
    b20_col = "#95a5a6"

total_buy  += b20_buy_s
total_sell += b20_sell_s
items.append((f"乖離率(MA{m})", b20_val, b20_col,
              f"當前={bias20:.2f}%（收盤偏離MA{m}的幅度）｜"
              f"正常區間：{b20_buy}%～+{b20_sell}%｜"
              f"低於{b20_buy}%=跌深買進區（{w}分）｜"
              f"高於+{b20_sell}%=漲多賣出區（{w}分）｜"
              f"在正常範圍內=0分",
              b20_buy_s, b20_sell_s))

# ── 7. 量能趨勢（權重5）──────────────────────────────────
w = WEIGHTS["vol"]
vol_ratio  = vol / vol_ma if vol_ma > 0 else 1
vol_buy_s  = vol_sell_s = 0

if use_vol:
    price_up_today = close > float(prev["Close"])
    if vol_trend > 0 and vol_ratio > 1.2 and price_up_today:
        vol_buy_s = w
        vol_val, vol_col = f"量能擴張 ✅（+{w}分）", "#2ecc71"
    elif vol_trend < 0 and vol_ratio < 0.8:
        vol_sell_s = w * 0.5
        vol_val, vol_col = f"量能萎縮 ⚠️（+{w*0.5:.0f}分賣）", "#e74c3c"
    else:
        vol_val, vol_col = "量能平穩（0分）", "#95a5a6"

    vol_detail = (f"今日成交量／{thr['vol_ma_period']}日均量={vol_ratio:.2f}倍｜"
                  f"正常範圍：0.8～1.2倍｜"
                  f"量能擴張且價漲={w}分｜量能萎縮={w*0.5:.0f}分賣出｜"
                  f"量能為輔助指標，單獨參考價值較低")
else:
    vol_val    = "已關閉（槓桿ETF不適用）"
    vol_col    = "#bdc3c7"
    vol_detail = "槓桿ETF成交量主要來自當沖套利，無法反映真實多空"

total_buy  += vol_buy_s
total_sell += vol_sell_s
items.append(("量能趨勢", vol_val, vol_col, vol_detail, vol_buy_s, vol_sell_s))

# ── 價格行為（第三層確認，不計分但影響呈現）─────────────
is_red  = close > open_p
chg_pct = (close - open_p) / open_p * 100
items.append(("價格行為",
              f"{'紅K' if is_red else '黑K'}（{chg_pct:+.2f}%）（參考）",
              "#2ecc71" if is_red else "#e74c3c",
              f"開盤={open_p:.2f}｜收盤={close:.2f}｜當日漲跌={chg_pct:+.2f}%｜"
              f"紅K：收盤>開盤，買方強勢｜黑K：收盤<開盤，賣方強勢｜"
              f"長上影線：上漲被壓回，賣壓重｜長下影線：下跌被撐回，買盤強｜"
              f"此指標僅供參考，不計入總分",
              0, 0))

# ── 綜合訊號判斷 ──────────────────────────────────────────

# BIAS60過熱鎖定：買進分數強制歸零
if b60["locked"]:
    effective_buy  = 0
    effective_sell = total_sell
else:
    effective_buy  = total_buy
    effective_sell = total_sell

# 買進/賣出哪邊分數高決定方向
if effective_buy >= effective_sell:
    direction = "buy"
    score     = effective_buy
else:
    direction = "sell"
    score     = effective_sell

level_key, emoji, level_label = score_to_level(score)

# 決定顯示文字和顏色
if b60["locked"] and direction == "buy":
    summary = f"🔥 過熱鎖定｜禁止追買（買進分={total_buy:.0f}分但被鎖定）"
    advice  = (f"季線乖離{b60['bias60']:.1f}%超過歷史門檻，"
               f"即使技術面有{total_buy:.0f}分買進訊號，統計上追買風險極高")
    bg, border = "#fdecea", "#c0392b"
    final_level = "OVERHEATED"
elif direction == "buy":
    dir_label = f"買進{level_label}"
    summary   = f"{emoji} {dir_label}（{score:.0f}/{max_possible}分）"
    advice    = _buy_advice(level_key, score)
    bg, border = _level_colors(level_key, "buy")
    final_level = f"BUY_{level_key}"
else:
    dir_label = f"賣出{level_label}"
    summary   = f"{emoji} {dir_label}（{score:.0f}/{max_possible}分）"
    advice    = _sell_advice(level_key, score)
    bg, border = _level_colors(level_key, "sell")
    final_level = f"SELL_{level_key}"

# 金字塔建倉
pyramid = calc_pyramid(df, scfg, final_level)

return dict(
    level=final_level, emoji=emoji, summary=summary, advice=advice,
    bg=bg, border=border, items=items,
    close=close, is_red=is_red,
    buy_score=total_buy, sell_score=total_sell,
    effective_buy=effective_buy, effective_sell=effective_sell,
    max_possible=max_possible,
    b60=b60, pyramid=pyramid,
)
```

def _buy_advice(level_key, score):
return {
“STRONG”:  f”多指標強力共振，高信心買進機會，可按金字塔計畫分批建倉”,
“MID”:     f”中等強度訊號，可考慮少量試單，等待更多確認再加碼”,
“WEAK”:    f”弱訊號，建議觀察等待，訊號增強後再行動”,
“NOTICE”:  f”微弱訊號，僅供留意，不建議單獨行動”,
“NEUTRAL”: f”無明顯買進依據，繼續觀察”,
}.get(level_key, “繼續觀察”)

def _sell_advice(level_key, score):
return {
“STRONG”:  f”多指標同步發出賣出訊號，建議考慮出場或減碼”,
“MID”:     f”中等賣出訊號，持有者應提高警覺，考慮設定停損”,
“WEAK”:    f”弱賣出訊號，持有者注意風險，暫不急於出場”,
“NOTICE”:  f”微弱賣出跡象，僅供留意”,
“NEUTRAL”: f”無明顯賣出依據，繼續持有觀察”,
}.get(level_key, “繼續觀察”)

def _level_colors(level_key, direction):
if direction == “buy”:
return {
“STRONG”:  (”#fdecea”, “#e74c3c”),
“MID”:     (”#fef5e7”, “#e67e22”),
“WEAK”:    (”#fef9e7”, “#f39c12”),
“NOTICE”:  (”#eaf4fb”, “#3498db”),
“NEUTRAL”: (”#f8f9fa”, “#95a5a6”),
}.get(level_key, (”#f8f9fa”, “#95a5a6”))
else:
return {
“STRONG”:  (”#eaf4fb”, “#2980b9”),
“MID”:     (”#f4ecf7”, “#8e44ad”),
“WEAK”:    (”#f8f9fa”, “#7f8c8d”),
“NOTICE”:  (”#f8f9fa”, “#95a5a6”),
“NEUTRAL”: (”#f8f9fa”, “#95a5a6”),
}.get(level_key, (”#f8f9fa”, “#95a5a6”))

# ══════════════════════════════════════════════════════════════

# 金字塔建倉

# ══════════════════════════════════════════════════════════════

def calc_pyramid(df: pd.DataFrame, scfg: dict, level: str) -> dict:
py         = scfg.get(“pyramid”, {})
drop_step  = py.get(“add_per_drop_pct”,    5.0)
add_ratio  = py.get(“add_ratio_pct”,       20.0)
time_days  = py.get(“time_rebalance_days”, 20)
time_ratio = py.get(“time_add_ratio_pct”,   5.0)

```
close     = float(df["Close"].iloc[-1])
recent    = df["Close"].iloc[-time_days:]
high_ref  = float(recent.max())
drop_pct  = (close - high_ref) / high_ref * 100
range_pct = (float(recent.max()) - float(recent.min())) / float(recent.min()) * 100
is_cons   = range_pct < 5.0
suggs     = []

if "BUY_" in level:
    batches = int(abs(drop_pct) / drop_step) if drop_pct < 0 else 0
    if batches == 0:
        suggs.append(f"📌 第1批建倉：建議投入可用資金 <b>{add_ratio:.0f}%</b>（首批試單）")
    else:
        suggs.append(f"📌 第{batches+1}批加碼：距高點回落 {abs(drop_pct):.1f}%，"
                     f"建議再投入剩餘資金 <b>{add_ratio:.0f}%</b>")
        suggs.append(f"　　累計已達 {batches} 次加碼條件（每跌 {drop_step:.0f}% 加一批）")
    if is_cons:
        suggs.append(f"⏱️ 時間補位提醒：近 {time_days} 日盤整幅度僅 {range_pct:.1f}%，"
                     f"可考慮投入剩餘資金 <b>{time_ratio:.0f}%</b> 進行時間性補位")

return dict(drop_pct=drop_pct, is_consolidating=is_cons,
            range_pct=range_pct, suggestions=suggs)
```

# ══════════════════════════════════════════════════════════════

# HTML 產生

# ══════════════════════════════════════════════════════════════

def score_bar_html(buy_score, sell_score, max_score, b60_locked):
“”“產生買進/賣出分數視覺化橫條”””
buy_pct  = min(buy_score  / max_score * 100, 100)
sell_pct = min(sell_score / max_score * 100, 100)
lock_note = ’ <span style="color:#c0392b;font-size:11px;">🔥 過熱鎖定</span>’ if b60_locked else “”
return (
f’<div style="padding:10px 16px;background:#f8f9fa;border-bottom:1px solid #eee;">’
f’<div style="display:flex;gap:16px;align-items:center;">’
f’<div style="flex:1;">’
f’<div style="font-size:11px;color:#555;margin-bottom:3px;">買進分數：{buy_score:.0f}/{max_score}分{lock_note}</div>’
f’<div style="background:#ddd;border-radius:4px;height:10px;">’
f’<div style=“background:{”#aaa” if b60_locked else “#2ecc71”};’
f’width:{buy_pct:.0f}%;height:10px;border-radius:4px;”></div></div></div>’
f’<div style="flex:1;">’
f’<div style="font-size:11px;color:#555;margin-bottom:3px;">賣出分數：{sell_score:.0f}/{max_score}分</div>’
f’<div style="background:#ddd;border-radius:4px;height:10px;">’
f’<div style="background:#e74c3c;width:{sell_pct:.0f}%;height:10px;border-radius:4px;"></div>’
f’</div></div></div></div>’
)

def stock_html_block(name: str, ticker: str, result: dict, note: str = “”) -> str:
rows = “”
for idx, item in enumerate(result[“items”]):
label, value, color, n = item[0], item[1], item[2], item[3]
parts = [p.strip() for p in n.split(“｜”) if p.strip()]
note_items = “”.join(
f’<span style="display:block;margin:2px 0;">’
f’<span style="color:#bbb;margin-right:4px;">{i+1}.</span>{p}</span>’
for i, p in enumerate(parts))
bg_row = “#fafafa” if idx % 2 == 0 else “#ffffff”
rows += (f’<tr style="background:{bg_row};border-bottom:1px solid #eee;">’
f’<td style="padding:8px 10px;color:#444;width:120px;font-size:13px;'
f'font-weight:bold;vertical-align:top;white-space:nowrap;">{label}</td>’
f’<td style="padding:8px 10px;font-weight:bold;color:{color};'
f'font-size:13px;vertical-align:top;width:200px;">{value}</td>’
f’<td style="padding:8px 10px;color:#666;font-size:12px;'
f'line-height:1.8;vertical-align:top;">{note_items}</td>’
f’</tr>’)

```
note_html = (f'<div style="background:#fef9e7;padding:8px 16px;'
             f'font-size:12px;color:#7d6608;border-bottom:1px solid #eee;">💡 {note}</div>'
             if note else "")

pyramid_html = ""
if result["pyramid"]["suggestions"]:
    sugg = "".join(f'<li style="margin:4px 0;font-size:13px;">{s}</li>'
                   for s in result["pyramid"]["suggestions"])
    pyramid_html = (f'<div style="background:#f0f8ff;padding:12px 16px;border-top:1px solid #d6eaf8;">'
                    f'<div style="font-weight:bold;color:#2471a3;margin-bottom:6px;">🏗️ 金字塔建倉建議</div>'
                    f'<ul style="margin:0;padding-left:18px;">{sugg}</ul></div>')

score_html = score_bar_html(
    result["buy_score"], result["sell_score"],
    result["max_possible"], result["b60"]["locked"])

return (
    f'<div style="margin-bottom:28px;border:2px solid {result["border"]};'
    f'border-radius:10px;overflow:hidden;background:#fff;">'
    f'<div style="background:{result["border"]};padding:12px 16px;'
    f'display:flex;justify-content:space-between;align-items:center;">'
    f'<span style="color:#fff;font-size:16px;font-weight:bold;">'
    f'{result["emoji"]} {name} ({ticker.replace(".TW","").replace(".tw","")})</span>'
    f'<span style="color:#fff;font-size:20px;font-weight:bold;">{result["close"]:.2f}</span>'
    f'</div>'
    f'{note_html}'
    f'<div style="background:{result["bg"]};padding:10px 16px;border-bottom:1px solid #eee;">'
    f'<strong style="font-size:15px;">{result["summary"]}</strong><br>'
    f'<span style="color:#555;font-size:13px;">{result["advice"]}</span></div>'
    f'{score_html}'
    f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'
    f'{pyramid_html}</div>'
)
```

def summary_table(results: list) -> str:
rows = “”
for name, ticker, r in results:
badge = “”
if r[“b60”][“zone”] == “overheated”:
badge = (’ <span style="background:#c0392b;color:#fff;font-size:10px;'
'padding:2px 6px;border-radius:4px;">🔥過熱</span>’)
elif r[“b60”][“zone”] == “oversold”:
badge = (’ <span style="background:#2980b9;color:#fff;font-size:10px;'
'padding:2px 6px;border-radius:4px;">❄️超跌</span>’)
rows += (f’<tr style="border-bottom:1px solid #eee;">’
f’<td style="padding:8px 12px;">{name}{badge}</td>’
f’<td style="padding:8px 12px;color:#777;">’
f’{ticker.replace(”.TW”,””).replace(”.tw”,””)}</td>’
f’<td style="padding:8px 12px;font-weight:bold;">{r[“close”]:.2f}</td>’
f’<td style="padding:8px 12px;font-size:16px;">{r[“emoji”]}</td>’
f’<td style=“padding:8px 12px;font-weight:bold;color:{r[“border”]};”>{r[“summary”]}</td>’
f’<td style="padding:8px 12px;font-size:12px;color:#777;">’
f’買{r[“buy_score”]:.0f}/賣{r[“sell_score”]:.0f}（滿分{r[“max_possible”]}）</td>’
f’</tr>’)
return (f’<table style="width:100%;border-collapse:collapse;margin-bottom:28px;'
f'border:1px solid #ddd;border-radius:8px;overflow:hidden;">’
f’<thead><tr style="background:#2c3e50;color:#fff;">’
f’<th style="padding:10px 12px;text-align:left;">股票名稱</th>’
f’<th style="padding:10px 12px;text-align:left;">代號</th>’
f’<th style="padding:10px 12px;text-align:left;">收盤價</th>’
f’<th style="padding:10px 12px;text-align:left;">訊號</th>’
f’<th style="padding:10px 12px;text-align:left;">說明</th>’
f’<th style="padding:10px 12px;text-align:left;">分數</th>’
f’</tr></thead><tbody>{rows}</tbody></table>’)

def build_email_html(results: list, today: str) -> str:
overview = summary_table(results)
details  = “”.join(
stock_html_block(n, t, r, note=r.get(“stock_note”, “”))
for n, t, r in results)
weights_note = (
f’趨勢環境{WEIGHTS[“trend”]}分｜MACD {WEIGHTS[“macd”]}分｜’
f’三大法人{WEIGHTS[“institutional”]}分｜KD {WEIGHTS[“kd”]}分｜’
f’OBV {WEIGHTS[“obv”]}分｜乖離率{WEIGHTS[“bias20”]}分｜’
f’量能{WEIGHTS[“vol”]}分｜總分100分｜’
f’強訊號≥70｜中訊號50-69｜弱訊號30-49｜提醒15-29’)
return (
f’<!DOCTYPE html><html><head><meta charset="utf-8"></head>’
f’<body style="font-family:Arial,sans-serif;max-width:760px;margin:0 auto;'
f'padding:20px;background:#f4f6f8;">’
f’<div style="background:#2c3e50;color:#fff;padding:20px;'
f'border-radius:10px 10px 0 0;text-align:center;">’
f’<h2 style="margin:0;">📊 每日股市訊號報告</h2>’
f’<p style="margin:6px 0 0;opacity:.8;">{today}｜收盤後分析</p></div>’
f’<div style="background:#fff;padding:24px;border-radius:0 0 10px 10px;'
f'box-shadow:0 2px 8px rgba(0,0,0,.08);">’
f’<div style="background:#eaf4fb;padding:8px 14px;border-radius:6px;'
f'font-size:11px;color:#2471a3;margin-bottom:16px;">ℹ️ 評分權重：{weights_note}</div>’
f’<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">今日總覽</h3>’
f’{overview}’
f’<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">各股詳細指標</h3>’
f’{details}’
f’<p style="color:#aaa;font-size:11px;text-align:center;'
f'border-top:1px solid #eee;padding-top:12px;margin-top:8px;">’
f’⚠️ 本報告由自動化程式產生，僅供參考，不構成投資建議。</p>’
f’</div></body></html>’)

# ══════════════════════════════════════════════════════════════

# Email 發送

# ══════════════════════════════════════════════════════════════

def send_email(cfg: dict, html: str, today: str) -> bool:
gmail_pass = os.environ.get(“GMAIL_PASSWORD”, “”)
if not gmail_pass:
print(“⚠️  未設定 GMAIL_PASSWORD（GitHub Secret），跳過發信”)
return False
ec  = cfg[“email”]
msg = MIMEMultipart(“alternative”)
msg[“Subject”] = ec[“subject”].format(date=today)
msg[“From”]    = ec[“from”]
msg[“To”]      = ec[“to”]
msg.attach(MIMEText(html, “html”, “utf-8”))
with smtplib.SMTP_SSL(“smtp.gmail.com”, 465) as s:
s.login(ec[“from”], gmail_pass)
s.sendmail(ec[“from”], ec[“to”], msg.as_string())
return True

# ══════════════════════════════════════════════════════════════

# 主流程

# ══════════════════════════════════════════════════════════════

def main():
cfg   = load_config()
today = datetime.today().strftime(”%Y/%m/%d”)
print(f”[{datetime.now().strftime(’%Y-%m-%d %H:%M’)}] 開始分析，共 {len(cfg[‘watchlist’])} 檔”)

```
# 三大法人資料只抓一次（所有股票共用同一份當日資料）
print("  抓取三大法人資料...", end=" ")
inst_cache = {}
for stock in cfg["watchlist"]:
    ticker = stock["ticker"]
    scfg   = get_stock_cfg(stock, cfg)
    if scfg.get("use_institutional", True):
        inst_data = fetch_institutional(ticker)
        inst_cache[ticker] = inst_data
        if inst_data["success"]:
            print(f"{ticker}✅ ", end="")
        else:
            print(f"{ticker}⚠️ ", end="")
    else:
        inst_cache[ticker] = {"success": False, "error": "已關閉"}
print()

results = []
for stock in cfg["watchlist"]:
    ticker = stock["ticker"]
    name   = stock["name"]
    note   = stock.get("note", "")
    print(f"  分析 {name} ({ticker}) ...", end=" ")
    try:
        scfg = get_stock_cfg(stock, cfg)
        df   = fetch_data(ticker, cfg["lookback_days"])
        df   = calc_indicators(df, scfg)
        inst = inst_cache.get(ticker, {"success": False, "error": "未查詢"})
        r    = evaluate(df, scfg, inst)
        r["stock_note"] = note
        results.append((name, ticker, r))
        print(f"{r['emoji']} {r['summary']}")
    except Exception as e:
        print(f"❌ {e}")

if not results:
    print("所有分析失敗，中止")
    return

html = build_email_html(results, today)
print(f"\n發送 Email 至 {cfg['email']['to']} ...")
try:
    if send_email(cfg, html, today):
        print("✅ Email 發送成功")
except Exception as e:
    print(f"❌ Email 失敗：{e}")
```

if **name** == “**main**”:
main() 