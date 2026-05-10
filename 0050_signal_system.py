"""
每日股市訊號系統
================
Repository : github.com/ryanhsu1983/AI_stock_0050
說明        : 每日台股收盤後自動執行，分析 watchlist 中所有股票，
              產生 HTML 格式每日報告寄送至指定 Email。
              所有參數從 config.json 讀取。
"""

import json
import os
import smtplib
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
        raise ValueError(f"無法取得 {ticker} 資料")
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


# ── 計算指標 ────────────────────────────────────────────────
def calc_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ma  = cfg["ma_periods"]
    thr = cfg["thresholds"]
    s, m, l = ma["short"], ma["mid"], ma["long"]

    # 均線
    df[f"MA{s}"] = df["Close"].rolling(s).mean()
    df[f"MA{m}"] = df["Close"].rolling(m).mean()
    df[f"MA{l}"] = df["Close"].rolling(l).mean()

    # KD
    low_min  = df["Low"].rolling(9).min()
    high_max = df["High"].rolling(9).max()
    rsv      = (df["Close"] - low_min) / (high_max - low_min) * 100
    df["K"]  = rsv.ewm(com=2, adjust=False).mean()
    df["D"]  = df["K"].ewm(com=2, adjust=False).mean()

    # MACD
    ema12           = df["Close"].ewm(span=12, adjust=False).mean()
    ema26           = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"]       = ema12 - ema26
    df["Signal"]    = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["DIF"] - df["Signal"]

    # 乖離率
    df["Bias"] = (df["Close"] - df[f"MA{m}"]) / df[f"MA{m}"] * 100

    # 量能趨勢（Vol MA）
    vp = thr["vol_ma_period"]
    df["Vol_MA"] = df["Volume"].rolling(vp).mean()
    df["Vol_Trend"] = df["Vol_MA"] - df["Vol_MA"].shift(3)  # 3日斜率

    # OBV
    obv = [0]
    for i in range(1, len(df)):
        if df["Close"].iloc[i] > df["Close"].iloc[i - 1]:
            obv.append(obv[-1] + df["Volume"].iloc[i])
        elif df["Close"].iloc[i] < df["Close"].iloc[i - 1]:
            obv.append(obv[-1] - df["Volume"].iloc[i])
        else:
            obv.append(obv[-1])
    df["OBV"] = obv
    op = thr["obv_ma_period"]
    df["OBV_MA"] = df["OBV"].rolling(op).mean()

    return df


# ── 評估訊號 ────────────────────────────────────────────────
def evaluate(df: pd.DataFrame, cfg: dict) -> dict:
    thr = cfg["thresholds"]
    ma  = cfg["ma_periods"]
    s, m, l = ma["short"], ma["mid"], ma["long"]

    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    close     = float(latest["Close"])
    ma_s      = float(latest[f"MA{s}"])
    ma_m      = float(latest[f"MA{m}"])
    ma_l      = float(latest[f"MA{l}"])
    ma_s_prev = float(prev[f"MA{s}"])
    ma_m_prev = float(prev[f"MA{m}"])
    ma_l_prev = float(prev[f"MA{l}"])
    k, d      = float(latest["K"]),  float(latest["D"])
    kp, dp    = float(prev["K"]),    float(prev["D"])
    hist      = float(latest["MACD_hist"])
    hist_p    = float(prev["MACD_hist"])
    bias      = float(latest["Bias"])
    vol       = float(latest["Volume"])
    vol_ma    = float(latest["Vol_MA"])
    vol_trend = float(latest["Vol_Trend"])
    obv       = float(latest["OBV"])
    obv_ma    = float(latest["OBV_MA"])
    obv_prev  = float(prev["OBV"])

    items   = []   # 指標明細列表
    l2_buy  = 0
    l2_sell = 0

    # ── 第一層：趨勢環境 ──────────────────────────────────────
    ma_s_dir   = ma_s > ma_s_prev
    above_ma_s = close > ma_s

    if ma_m > ma_l and above_ma_s and ma_s_dir:
        trend = "healthy_bull"
    elif ma_m > ma_l and (not above_ma_s or not ma_s_dir):
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

    trend_color = {
        "healthy_bull": "#2ecc71",
        "weak_bull":    "#f39c12",
        "bear":         "#e74c3c",
        "neutral":      "#95a5a6",
    }[trend]

    items.append(("趨勢環境", trend_label, trend_color,
                  f"MA{s}={ma_s:.1f} MA{m}={ma_m:.1f} MA{l}={ma_l:.1f}｜"
                  f"收盤{'站上' if above_ma_s else '跌破'}{s}日線，{s}日線{'↑' if ma_s_dir else '↓'}"))

    # ── 第二層：時機指標 ──────────────────────────────────────

    # MACD
    if hist > 0 and hist_p <= 0:
        l2_buy += 1
        items.append(("MACD", "柱狀由負翻正", "#2ecc71", f"hist={hist:.4f}｜動能轉強"))
    elif hist < 0 and hist_p >= 0:
        l2_sell += 1
        items.append(("MACD", "柱狀由正翻負", "#e74c3c", f"hist={hist:.4f}｜動能轉弱"))
    else:
        sign = "正" if hist > 0 else "負"
        items.append(("MACD", f"柱狀持續為{sign}", "#95a5a6", f"hist={hist:.4f}"))

    # KD
    kd_buy  = k > d and kp <= dp and k < thr["kd_buy"]
    kd_sell = k < d and kp >= dp and k > thr["kd_sell"]
    if kd_buy:
        l2_buy += 1
        items.append(("KD", "低檔黃金交叉", "#2ecc71", f"K={k:.1f} D={d:.1f}｜門檻<{thr['kd_buy']}"))
    elif kd_sell:
        l2_sell += 1
        items.append(("KD", "高檔死亡交叉", "#e74c3c", f"K={k:.1f} D={d:.1f}｜門檻>{thr['kd_sell']}"))
    else:
        items.append(("KD", "無交叉訊號", "#95a5a6", f"K={k:.1f} D={d:.1f}"))

    # 乖離率
    if bias < thr["bias_buy"]:
        l2_buy += 1
        items.append(("乖離率", "跌深反彈機會", "#2ecc71", f"{bias:.2f}%｜門檻{thr['bias_buy']}%"))
    elif bias > thr["bias_sell"]:
        l2_sell += 1
        items.append(("乖離率", "漲幅過高警示", "#e74c3c", f"{bias:.2f}%｜門檻+{thr['bias_sell']}%"))
    else:
        items.append(("乖離率", "正常範圍", "#95a5a6", f"{bias:.2f}%"))

    # 均線交叉
    ma_bull = ma_m > ma_l and ma_m_prev <= ma_l_prev
    ma_bear = ma_m < ma_l and ma_m_prev >= ma_l_prev
    if ma_bull:
        l2_buy += 1
        items.append(("均線交叉", f"MA{m}剛上穿MA{l}", "#2ecc71", "趨勢確立"))
    elif ma_bear:
        l2_sell += 1
        items.append(("均線交叉", f"MA{m}剛下穿MA{l}", "#e74c3c", "趨勢反轉"))
    else:
        rel = ">" if ma_m > ma_l else "<"
        items.append(("均線交叉", "維持現狀", "#95a5a6", f"MA{m}{rel}MA{l}"))

    # ── 量能趨勢 ──────────────────────────────────────────────
    vol_ratio = vol / vol_ma if vol_ma > 0 else 1
    if vol_trend > 0 and vol_ratio > 1.2:
        vol_label = "量能擴張"
        vol_color = "#2ecc71"
        if close > float(prev["Close"]):
            l2_buy += 1
    elif vol_trend < 0 and vol_ratio < 0.8:
        vol_label = "量能萎縮"
        vol_color = "#e74c3c"
    else:
        vol_label = "量能平穩"
        vol_color = "#95a5a6"
    vp = thr["vol_ma_period"]
    items.append(("量能趨勢", vol_label, vol_color,
                  f"今日量/均量={vol_ratio:.2f}｜{vp}日均量趨勢{'↑' if vol_trend>0 else '↓' if vol_trend<0 else '→'}"))

    # ── OBV ───────────────────────────────────────────────────
    obv_rising = obv > obv_ma and obv > obv_prev
    obv_falling = obv < obv_ma and obv < obv_prev
    price_rising = close > float(prev["Close"])

    if obv_rising and price_rising:
        obv_label = "量價齊揚"
        obv_color = "#2ecc71"
        l2_buy += 1
    elif obv_rising and not price_rising:
        obv_label = "OBV領先價格"
        obv_color = "#3498db"
    elif obv_falling and not price_rising:
        obv_label = "量價齊跌"
        obv_color = "#e74c3c"
        l2_sell += 1
    elif obv_falling and price_rising:
        obv_label = "價漲量縮背離"
        obv_color = "#f39c12"
    else:
        obv_label = "OBV中性"
        obv_color = "#95a5a6"
    op = thr["obv_ma_period"]
    items.append(("OBV", obv_label, obv_color,
                  f"OBV={'高於' if obv>obv_ma else '低於'}{op}日均線"))

    # ── 第三層：價格行為 ──────────────────────────────────────
    is_red = close > float(latest["Open"])
    items.append(("價格行為", "紅K" if is_red else "黑K", "#2ecc71" if is_red else "#e74c3c",
                  f"開={float(latest['Open']):.2f} 收={close:.2f}"))

    # ── 綜合訊號 ──────────────────────────────────────────────
    if trend == "healthy_bull" and l2_buy >= 2:
        level, emoji, summary = "STRONG_BUY",  "🔴", "強買進訊號"
        advice = "多頭健康，多指標共振，建議關注進場機會"
        bg     = "#fdecea"
        border = "#e74c3c"
    elif (trend == "healthy_bull" and l2_buy == 1) or \
         (trend == "weak_bull"    and l2_buy >= 2):
        level, emoji, summary = "WEAK_BUY",    "🟡", "弱買進提醒"
        advice = "單一訊號或趨勢轉弱，列入觀察，勿躁進"
        bg     = "#fef9e7"
        border = "#f39c12"
    elif trend in ("weak_bull", "healthy_bull") and not ma_s_dir:
        level, emoji, summary = "WARNING",     "🟠", "風險警示"
        advice = f"{s}日線走弱，建議降低部位或暫緩操作"
        bg     = "#fef5e7"
        border = "#e67e22"
    elif trend == "bear" and l2_sell >= 2:
        level, emoji, summary = "STRONG_SELL", "🔵", "強賣出訊號"
        advice = "空頭確認，多指標共振，建議考慮出場"
        bg     = "#eaf4fb"
        border = "#3498db"
    elif trend == "neutral":
        level, emoji, summary = "NEUTRAL",     "⚪", "方向不明"
        advice = "均線糾結或訊號矛盾，建議觀望"
        bg     = "#f8f9fa"
        border = "#95a5a6"
    else:
        level, emoji, summary = "NEUTRAL",     "⚪", "無明顯訊號"
        advice = "目前無強烈進出依據，繼續觀察"
        bg     = "#f8f9fa"
        border = "#95a5a6"

    return dict(
        level=level, emoji=emoji, summary=summary, advice=advice,
        bg=bg, border=border, items=items,
        close=close, bias=bias, is_red=is_red,
        l2_buy=l2_buy, l2_sell=l2_sell,
    )


# ── 產生單檔 HTML 區塊 ───────────────────────────────────────
def stock_html_block(name: str, ticker: str, result: dict) -> str:
    rows = ""
    for label, value, color, note in result["items"]:
        rows += f"""
        <tr>
          <td style="padding:6px 10px;color:#555;width:90px;">{label}</td>
          <td style="padding:6px 10px;font-weight:bold;color:{color};">{value}</td>
          <td style="padding:6px 10px;color:#777;font-size:12px;">{note}</td>
        </tr>"""

    return f"""
    <div style="margin-bottom:28px;border:2px solid {result['border']};
                border-radius:10px;overflow:hidden;background:#fff;">
      <!-- 股票標題列 -->
      <div style="background:{result['border']};padding:12px 16px;
                  display:flex;justify-content:space-between;align-items:center;">
        <span style="color:#fff;font-size:16px;font-weight:bold;">
          {result['emoji']} {name} ({ticker.replace('.TW','')})
        </span>
        <span style="color:#fff;font-size:20px;font-weight:bold;">
          ${result['close']:.2f}
        </span>
      </div>
      <!-- 訊號摘要 -->
      <div style="background:{result['bg']};padding:10px 16px;
                  border-bottom:1px solid #eee;">
        <strong>{result['summary']}</strong>
        <span style="color:#555;margin-left:8px;">— {result['advice']}</span>
      </div>
      <!-- 指標明細 -->
      <table style="width:100%;border-collapse:collapse;">
        {rows}
      </table>
    </div>"""


# ── 產生總覽表格 ─────────────────────────────────────────────
def summary_table(results: list) -> str:
    rows = ""
    for name, ticker, r in results:
        rows += f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:8px 12px;">{name}</td>
          <td style="padding:8px 12px;color:#777;">{ticker.replace('.TW','')}</td>
          <td style="padding:8px 12px;font-weight:bold;">{r['close']:.2f}</td>
          <td style="padding:8px 12px;font-size:18px;">{r['emoji']}</td>
          <td style="padding:8px 12px;font-weight:bold;color:{r['border']};">{r['summary']}</td>
        </tr>"""

    return f"""
    <table style="width:100%;border-collapse:collapse;margin-bottom:28px;
                  border:1px solid #ddd;border-radius:8px;overflow:hidden;">
      <thead>
        <tr style="background:#2c3e50;color:#fff;">
          <th style="padding:10px 12px;text-align:left;">股票名稱</th>
          <th style="padding:10px 12px;text-align:left;">代號</th>
          <th style="padding:10px 12px;text-align:left;">收盤價</th>
          <th style="padding:10px 12px;text-align:left;">訊號</th>
          <th style="padding:10px 12px;text-align:left;">說明</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


# ── 組裝完整 HTML Email ──────────────────────────────────────
def build_email_html(results: list, today: str) -> str:
    overview  = summary_table(results)
    details   = "".join(stock_html_block(n, t, r) for n, t, r in results)

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:700px;
             margin:0 auto;padding:20px;background:#f4f6f8;">

  <!-- 頁首 -->
  <div style="background:#2c3e50;color:#fff;padding:20px;
              border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="margin:0;">📊 每日股市訊號報告</h2>
    <p style="margin:6px 0 0;opacity:.8;">{today}｜收盤後分析</p>
  </div>

  <div style="background:#fff;padding:24px;border-radius:0 0 10px 10px;
              box-shadow:0 2px 8px rgba(0,0,0,.08);">

    <!-- 今日總覽 -->
    <h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;
               padding-bottom:6px;">今日總覽</h3>
    {overview}

    <!-- 各股詳細指標 -->
    <h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;
               padding-bottom:6px;">各股詳細指標</h3>
    {details}

    <!-- 免責聲明 -->
    <p style="color:#aaa;font-size:11px;text-align:center;
              border-top:1px solid #eee;padding-top:12px;margin-top:8px;">
      ⚠️ 本報告由自動化程式產生，僅供參考，不構成投資建議。<br>
      請依據個人判斷決定進出場時機。
    </p>
  </div>
</body>
</html>"""


# ── 發送 Email ───────────────────────────────────────────────
def send_email(cfg: dict, html: str, today: str) -> bool:
    gmail_pass = os.environ.get("GMAIL_PASSWORD", "")
    if not gmail_pass:
        print("⚠️  未設定 GMAIL_PASSWORD（GitHub Secret），跳過發信")
        return False

    ec      = cfg["email"]
    subject = ec["subject"].format(date=today)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = ec["from"]
    msg["To"]      = ec["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(ec["from"], gmail_pass)
        server.sendmail(ec["from"], ec["to"], msg.as_string())
    return True


# ── 主流程 ───────────────────────────────────────────────────
def main():
    cfg   = load_config()
    today = datetime.today().strftime("%Y/%m/%d")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 開始分析，共 {len(cfg['watchlist'])} 檔股票")

    results = []
    for stock in cfg["watchlist"]:
        ticker = stock["ticker"]
        name   = stock["name"]
        print(f"  分析中：{name} ({ticker}) ...", end=" ")
        try:
            df = fetch_data(ticker, cfg["lookback_days"])
            df = calc_indicators(df, cfg)
            r  = evaluate(df, cfg)
            results.append((name, ticker, r))
            print(f"{r['emoji']} {r['summary']}")
        except Exception as e:
            print(f"❌ 錯誤：{e}")

    if not results:
        print("所有股票分析失敗，中止執行")
        return

    html = build_email_html(results, today)

    print(f"\n發送 Email 至 {cfg['email']['to']} ...")
    try:
        if send_email(cfg, html, today):
            print("✅ Email 發送成功")
    except Exception as e:
        print(f"❌ Email 發送失敗：{e}")


if __name__ == "__main__":
    main()
