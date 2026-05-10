"""
0050 波段操作訊號系統
====================
Repository : github.com/ryanhsu1983/AI_stock_0050
說明        : 每日台股收盤後自動執行，依三層指標架構判斷訊號強度，
              透過 Discord Webhook 推播提醒。所有參數從 config.json 讀取。
"""

import json
import os
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path


# ── 讀取設定 ────────────────────────────────────────────────
def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 抓取資料 ────────────────────────────────────────────────
def fetch_data(ticker: str, days: int) -> pd.DataFrame:
    end   = datetime.today()
    start = end - timedelta(days=days)
    df = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )
    if df.empty:
        raise ValueError(f"無法取得 {ticker} 資料，請確認代號正確")
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


# ── 計算指標 ────────────────────────────────────────────────
def calc_indicators(df: pd.DataFrame, ma: dict) -> pd.DataFrame:
    s, m, l = ma["short"], ma["mid"], ma["long"]

    df[f"MA{s}"]  = df["Close"].rolling(s).mean()
    df[f"MA{m}"]  = df["Close"].rolling(m).mean()
    df[f"MA{l}"]  = df["Close"].rolling(l).mean()

    # KD
    low_min  = df["Low"].rolling(9).min()
    high_max = df["High"].rolling(9).max()
    rsv      = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["K"]  = rsv.ewm(com=2, adjust=False).mean()
    df["D"]  = df["K"].ewm(com=2, adjust=False).mean()

    # MACD
    ema12         = df["Close"].ewm(span=12, adjust=False).mean()
    ema26         = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"]     = ema12 - ema26
    df["Signal"]  = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["DIF"] - df["Signal"]

    # 乖離率
    df["Bias"] = (df["Close"] - df[f"MA{m}"]) / df[f"MA{m}"] * 100

    return df


# ── 評估訊號 ────────────────────────────────────────────────
def evaluate_signals(df: pd.DataFrame, cfg: dict) -> dict:
    thr = cfg["thresholds"]
    ma  = cfg["ma_periods"]
    s, m, l = ma["short"], ma["mid"], ma["long"]

    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    close      = float(latest["Close"])
    ma_s       = float(latest[f"MA{s}"])
    ma_m       = float(latest[f"MA{m}"])
    ma_l       = float(latest[f"MA{l}"])
    ma_s_prev  = float(prev[f"MA{s}"])
    ma_m_prev  = float(prev[f"MA{m}"])
    ma_l_prev  = float(prev[f"MA{l}"])
    k, d       = float(latest["K"]), float(latest["D"])
    k_p, d_p   = float(prev["K"]),   float(prev["D"])
    hist       = float(latest["MACD_hist"])
    hist_p     = float(prev["MACD_hist"])
    bias       = float(latest["Bias"])

    details        = []
    layer2_buy     = 0
    layer2_sell    = 0

    # ── 第一層：趨勢環境 ──────────────────────────────────────
    ma_s_dir   = "up" if ma_s > ma_s_prev else "down"
    above_ma_s = close > ma_s

    if ma_m > ma_l and above_ma_s and ma_s_dir == "up":
        trend = "healthy_bull"
    elif ma_m > ma_l and (not above_ma_s or ma_s_dir == "down"):
        trend = "weak_bull"
    elif ma_m < ma_l:
        trend = "bear"
    else:
        trend = "neutral"

    trend_label = {
        "healthy_bull": "多頭健康",
        "weak_bull":    "多頭轉弱",
        "bear":         "空頭確認",
        "neutral":      "方向不明",
    }[trend]

    details.append(f"【第一層】趨勢環境：{trend_label}")
    details.append(f"  MA{s}={ma_s:.2f}  MA{m}={ma_m:.2f}  MA{l}={ma_l:.2f}")
    details.append(f"  收盤={close:.2f}（{'站上' if above_ma_s else '跌破'}{s}日線，{s}日線{'向上' if ma_s_dir=='up' else '向下'}）")

    # ── 第二層：時機指標 ──────────────────────────────────────
    details.append("【第二層】時機指標")

    # MACD
    if hist > 0 and hist_p <= 0:
        layer2_buy += 1
        details.append(f"  ✅ MACD 柱狀由負翻正（動能轉強）{hist:.3f}")
    elif hist < 0 and hist_p >= 0:
        layer2_sell += 1
        details.append(f"  🔻 MACD 柱狀由正翻負（動能轉弱）{hist:.3f}")
    else:
        details.append(f"  ⬜ MACD 無翻轉（{hist:.3f}）")

    # KD
    kd_buy  = k > d and k_p <= d_p and k < thr["kd_buy"]
    kd_sell = k < d and k_p >= d_p and k > thr["kd_sell"]
    if kd_buy:
        layer2_buy += 1
        details.append(f"  ✅ KD 低檔黃金交叉（K={k:.1f} D={d:.1f}，門檻<{thr['kd_buy']}）")
    elif kd_sell:
        layer2_sell += 1
        details.append(f"  🔻 KD 高檔死亡交叉（K={k:.1f} D={d:.1f}，門檻>{thr['kd_sell']}）")
    else:
        details.append(f"  ⬜ KD 無交叉（K={k:.1f} D={d:.1f}）")

    # 乖離率
    if bias < thr["bias_buy"]:
        layer2_buy += 1
        details.append(f"  ✅ 乖離率跌深（{bias:.2f}%，門檻{thr['bias_buy']}%）")
    elif bias > thr["bias_sell"]:
        layer2_sell += 1
        details.append(f"  🔻 乖離率過高（{bias:.2f}%，門檻+{thr['bias_sell']}%）")
    else:
        details.append(f"  ⬜ 乖離率正常（{bias:.2f}%）")

    # 均線交叉
    ma_bull = ma_m > ma_l and ma_m_prev <= ma_l_prev
    ma_bear = ma_m < ma_l and ma_m_prev >= ma_l_prev
    if ma_bull:
        layer2_buy += 1
        details.append(f"  ✅ MA{m} 剛上穿 MA{l}（趨勢確立）")
    elif ma_bear:
        layer2_sell += 1
        details.append(f"  🔻 MA{m} 剛下穿 MA{l}（趨勢反轉）")
    else:
        details.append(f"  ⬜ 均線排列維持（MA{m}{'>' if ma_m>ma_l else '<'}MA{l}）")

    # ── 第三層：價格行為確認 ──────────────────────────────────
    is_red = close > float(latest["Open"])
    details.append(f"【第三層】價格行為：{'🕯 紅K（收盤>開盤）' if is_red else '🕯 黑K（收盤<開盤）'}")

    # ── 綜合判斷 ──────────────────────────────────────────────
    if trend == "healthy_bull" and layer2_buy >= 2:
        level, emoji, summary = "STRONG_BUY",  "🔴", "強買進訊號"
        advice = "多頭健康，多指標共振，建議關注進場機會"
    elif (trend == "healthy_bull" and layer2_buy == 1) or \
         (trend == "weak_bull"    and layer2_buy >= 2):
        level, emoji, summary = "WEAK_BUY",    "🟡", "弱買進提醒"
        advice = "單一訊號或趨勢轉弱，列入觀察，勿躁進"
    elif trend in ("weak_bull", "healthy_bull") and ma_s_dir == "down":
        level, emoji, summary = "WARNING",     "🟠", "風險警示"
        advice = f"{s}日線走弱，建議降低部位或暫緩操作"
    elif trend == "bear" and layer2_sell >= 2:
        level, emoji, summary = "STRONG_SELL", "🔵", "強賣出訊號"
        advice = "空頭確認，多指標共振，建議考慮出場"
    elif trend == "neutral":
        level, emoji, summary = "NEUTRAL",     "⚪", "方向不明"
        advice = "均線糾結或訊號矛盾，建議觀望"
    else:
        level, emoji, summary = "NEUTRAL",     "⚪", "無明顯訊號"
        advice = "目前無強烈進出依據，繼續觀察"

    return dict(
        level=level, emoji=emoji, summary=summary,
        advice=advice, details=details,
        close=close, bias=bias, is_red=is_red,
    )


# ── 組裝訊息 ────────────────────────────────────────────────
def build_message(result: dict) -> str:
    today = datetime.today().strftime("%Y/%m/%d")
    lines = [
        "📊 0050 波段訊號提醒",
        f"日期：{today}",
        f"收盤價：{result['close']:.2f}",
        "",
        f"{result['emoji']} {result['summary']}",
        f"→ {result['advice']}",
        "",
        "─── 指標明細 ───",
        *result["details"],
        "",
        "⚠️ 本訊號僅供參考，請自行判斷進出時機",
    ]
    return "\n".join(lines)


# ── Discord 推播 ─────────────────────────────────────────────
def send_discord(webhook_url: str, message: str) -> bool:
    payload = {"content": f"```\n{message[:1950]}\n```"}
    resp = requests.post(webhook_url, json=payload, timeout=10)
    return resp.status_code in (200, 204)


# ── 主流程 ───────────────────────────────────────────────────
def main():
    cfg         = load_config()
    webhook_url = os.environ.get("DISCORD_WEBHOOK", "")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 開始分析 {cfg['ticker']} ...")

    df     = fetch_data(cfg["ticker"], cfg["lookback_days"])
    df     = calc_indicators(df, cfg["ma_periods"])
    result = evaluate_signals(df, cfg)
    msg    = build_message(result)

    print("\n" + "=" * 50)
    print(msg)
    print("=" * 50)

    notify_map = {
        "STRONG_BUY":  cfg["notify"]["on_strong_buy"],
        "WEAK_BUY":    cfg["notify"]["on_weak_buy"],
        "WARNING":     cfg["notify"]["on_warning"],
        "STRONG_SELL": cfg["notify"]["on_strong_sell"],
        "NEUTRAL":     cfg["notify"]["on_neutral"],
    }

    if notify_map.get(result["level"], False):
        if not webhook_url:
            print("\n⚠️  未設定 DISCORD_WEBHOOK（GitHub Secret），跳過推播")
        elif send_discord(webhook_url, msg):
            print("\n✅ Discord 推播成功")
        else:
            print("\n❌ Discord 推播失敗（請確認 Webhook URL）")
    else:
        print(f"\n⚪ 訊號等級 {result['level']} 設定為不推播")


if __name__ == "__main__":
    main()
