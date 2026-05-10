"""
每日股市訊號系統 v4
===================
Repository : github.com/ryanhsu1983/AI_stock_0050
v4 新增：每檔股票獨立 overrides 設定，支援個別化指標門檻與開關
"""

import json, os, smtplib
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


# ── 讀取設定 ────────────────────────────────────────────────
def load_config() -> dict:
    with open(Path(__file__).parent / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def get_stock_cfg(stock: dict, global_cfg: dict) -> dict:
    """
    將全域設定與個股 overrides 合併，個股設定優先。
    回傳該股票實際使用的完整設定。
    """
    ov  = stock.get("overrides", {})
    thr = dict(global_cfg["thresholds"])
    ma  = dict(global_cfg["ma_periods"])

    # 覆蓋 thresholds
    for key in ("kd_buy","kd_sell","bias20_buy","bias20_sell",
                "bias60_p_low","bias60_p_high","vol_ma_period","obv_ma_period"):
        if key in ov:
            thr[key] = ov[key]

    # 向下相容舊欄位名稱
    if "bias_buy"  in thr and "bias20_buy"  not in thr: thr["bias20_buy"]  = thr["bias_buy"]
    if "bias_sell" in thr and "bias20_sell" not in thr: thr["bias20_sell"] = thr["bias_sell"]

    # 覆蓋 ma_periods
    if "ma_periods" in ov:
        ma.update(ov["ma_periods"])

    return {
        "thresholds":       thr,
        "ma_periods":       ma,
        "pyramid":          global_cfg.get("pyramid", {}),
        "use_obv":          ov.get("use_obv",          True),
        "use_vol_trend":    ov.get("use_vol_trend",     True),
        "leverage_warning": ov.get("leverage_warning",  False),
        "bias60_locked":    ov.get("bias60_locked",     True),
    }


# ── 抓取資料 ────────────────────────────────────────────────
def fetch_data(ticker: str, days: int) -> pd.DataFrame:
    end   = datetime.today()
    start = end - timedelta(days=days)
    df = yf.download(ticker,
                     start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"),
                     progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"無法取得 {ticker} 資料")
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df[["Open","High","Low","Close","Volume"]].dropna()


# ── 計算指標 ────────────────────────────────────────────────
def calc_indicators(df: pd.DataFrame, scfg: dict) -> pd.DataFrame:
    ma  = scfg["ma_periods"]
    thr = scfg["thresholds"]
    s, m, l = ma["short"], ma["mid"], ma["long"]

    df[f"MA{s}"] = df["Close"].rolling(s).mean()
    df[f"MA{m}"] = df["Close"].rolling(m).mean()
    df[f"MA{l}"] = df["Close"].rolling(l).mean()

    # BIAS60（季線乖離，固定60日，用於Z-Score）
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

    # 短線乖離率（依各股 mid MA）
    df["Bias20"] = (df["Close"] - df[f"MA{m}"]) / df[f"MA{m}"] * 100

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

    # 量能趨勢
    vp           = thr["vol_ma_period"]
    df["Vol_MA"] = df["Volume"].rolling(vp).mean()
    df["Vol_Trend"] = df["Vol_MA"] - df["Vol_MA"].shift(3)

    # OBV
    obv = [0]
    for i in range(1, len(df)):
        if   df["Close"].iloc[i] > df["Close"].iloc[i-1]: obv.append(obv[-1] + df["Volume"].iloc[i])
        elif df["Close"].iloc[i] < df["Close"].iloc[i-1]: obv.append(obv[-1] - df["Volume"].iloc[i])
        else:                                               obv.append(obv[-1])
    df["OBV"]    = obv
    df["OBV_MA"] = df["OBV"].rolling(thr["obv_ma_period"]).mean()

    return df


# ── BIAS60 Z-Score 評估 ──────────────────────────────────────
def eval_bias60(df: pd.DataFrame, scfg: dict) -> dict:
    latest  = df.iloc[-1]
    bias60  = float(latest["BIAS60"])
    z       = float(latest["BIAS60_Z"])
    p_high  = df.attrs["bias60_p_high"]
    p_low   = df.attrs["bias60_p_low"]
    p_high_pct = scfg["thresholds"].get("bias60_p_high", 95)
    p_low_pct  = scfg["thresholds"].get("bias60_p_low",   5)
    can_lock   = scfg.get("bias60_locked", True)

    if bias60 >= p_high:
        zone   = "overheated"
        locked = can_lock
        label  = f"🔥 過熱{'鎖定' if can_lock else '警示'}（季線乖離{bias60:.1f}%，歷史{p_high_pct}%分位）"
        color  = "#c0392b"
        note   = f"Z={z:.2f}｜超過歷史{p_high_pct}%分位({p_high:.1f}%)｜{'強制禁止買進' if can_lock else '僅警示，不鎖定'}"
    elif bias60 <= p_low:
        zone   = "oversold"
        locked = False
        label  = f"❄️ 超跌部署區（季線乖離{bias60:.1f}%，歷史{p_low_pct}%分位）"
        color  = "#2980b9"
        note   = f"Z={z:.2f}｜低於歷史{p_low_pct}%分位({p_low:.1f}%)｜統計黃金建倉區"
    else:
        zone   = "normal"
        locked = False
        label  = f"正常範圍（季線乖離{bias60:.1f}%）"
        color  = "#95a5a6"
        note   = f"Z={z:.2f}｜介於{p_low_pct}%({p_low:.1f}%)～{p_high_pct}%({p_high:.1f}%)分位之間"

    return dict(zone=zone, locked=locked, bias60=bias60,
                z_score=z, p_high=p_high, p_low=p_low,
                label=label, color=color, note=note)


# ── 金字塔建倉計算 ───────────────────────────────────────────
def calc_pyramid(df: pd.DataFrame, scfg: dict, signal_level: str) -> dict:
    py         = scfg.get("pyramid", {})
    drop_step  = py.get("add_per_drop_pct",    5.0)
    add_ratio  = py.get("add_ratio_pct",       20.0)
    time_days  = py.get("time_rebalance_days", 20)
    time_ratio = py.get("time_add_ratio_pct",   5.0)

    close    = float(df["Close"].iloc[-1])
    recent   = df["Close"].iloc[-time_days:]
    high_ref = float(recent.max())
    drop_pct = (close - high_ref) / high_ref * 100
    range_pct = (float(recent.max()) - float(recent.min())) / float(recent.min()) * 100
    is_consolidating = range_pct < 5.0
    suggestions = []

    if signal_level in ("STRONG_BUY", "WEAK_BUY"):
        batches = int(abs(drop_pct) / drop_step) if drop_pct < 0 else 0
        if batches == 0:
            suggestions.append(
                f"📌 第1批建倉：建議投入可用資金 <b>{add_ratio:.0f}%</b>（首批試單）")
        else:
            suggestions.append(
                f"📌 第{batches+1}批加碼：距高點回落 {abs(drop_pct):.1f}%，"
                f"建議再投入剩餘資金 <b>{add_ratio:.0f}%</b>")
            suggestions.append(
                f"　　累計已達 {batches} 次加碼條件（每跌 {drop_step:.0f}% 加一批）")
        if is_consolidating:
            suggestions.append(
                f"⏱️ 時間補位提醒：近 {time_days} 日盤整幅度僅 {range_pct:.1f}%，"
                f"可考慮投入剩餘資金 <b>{time_ratio:.0f}%</b> 進行時間性補位")

    return dict(drop_pct=drop_pct, is_consolidating=is_consolidating,
                range_pct=range_pct, suggestions=suggestions)


# ── 評估訊號 ────────────────────────────────────────────────
def evaluate(df: pd.DataFrame, scfg: dict) -> dict:
    thr        = scfg["thresholds"]
    ma         = scfg["ma_periods"]
    use_obv    = scfg.get("use_obv", True)
    use_vol    = scfg.get("use_vol_trend", True)
    lev_warn   = scfg.get("leverage_warning", False)
    s, m, l    = ma["short"], ma["mid"], ma["long"]

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

    items  = []
    l2_buy = l2_sell = 0

    # 槓桿ETF警示標籤
    if lev_warn:
        items.append(("⚠️ 槓桿警示", "每日重置ETF，不適合長抱", "#e67e22",
                      "槓桿ETF有長期耗損效應，僅適合短線波段操作"))

    # ── BIAS60 Z-Score ────────────────────────────────────────
    b60 = eval_bias60(df, scfg)
    items.append(("BIAS60 Z-Score", b60["label"], b60["color"], b60["note"]))

    # ── 第一層：趨勢環境 ──────────────────────────────────────
    ma_s_dir   = ma_s > ma_s_prev
    above_ma_s = close > ma_s

    if ma_m > ma_l and above_ma_s and ma_s_dir:     trend = "healthy_bull"
    elif ma_m > ma_l and (not above_ma_s or not ma_s_dir): trend = "weak_bull"
    elif ma_m < ma_l:                                trend = "bear"
    else:                                            trend = "neutral"

    trend_label = {"healthy_bull":"多頭健康","weak_bull":"多頭轉弱",
                   "bear":"空頭確認","neutral":"方向不明"}[trend]
    trend_color = {"healthy_bull":"#2ecc71","weak_bull":"#f39c12",
                   "bear":"#e74c3c","neutral":"#95a5a6"}[trend]
    items.append(("趨勢環境", trend_label, trend_color,
                  f"MA{s}={ma_s:.1f}｜MA{m}={ma_m:.1f}｜MA{l}={ma_l:.1f}｜"
                  f"收盤{'站上' if above_ma_s else '跌破'}{s}日線，{s}日線{'↑' if ma_s_dir else '↓'}｜"
                  f"多頭健康=MA{m}>MA{l}且站上{s}日線，多頭轉弱=均線排列但跌破短線支撐，空頭=MA{m}<MA{l}"))
    # ── 第二層：時機指標 ──────────────────────────────────────

    # MACD
    # 計算歷史MACD柱狀範圍供參考
    hist_series = df["MACD_hist"].dropna()
    hist_p10 = float(hist_series.quantile(0.10))
    hist_p90 = float(hist_series.quantile(0.90))
    macd_range_note = f"當前={hist:.4f}｜歷史正常區間[{hist_p10:.4f}～{hist_p90:.4f}]｜正=多頭動能，負=空頭動能，0軸為中性"
    if hist > 0 and hist_p <= 0:
        l2_buy += 1
        items.append(("MACD", "柱狀由負翻正 ✅", "#2ecc71", macd_range_note + "｜剛翻正，動能轉強"))
    elif hist < 0 and hist_p >= 0:
        l2_sell += 1
        items.append(("MACD", "柱狀由正翻負 ⚠️", "#e74c3c", macd_range_note + "｜剛翻負，動能轉弱"))
    else:
        sign = "正（多頭）" if hist > 0 else "負（空頭）"
        items.append(("MACD", f"柱狀持續為{sign}", "#95a5a6", macd_range_note))

    # KD（使用個股門檻）
    kd_buy  = k > d and kp <= dp and k < thr["kd_buy"]
    kd_sell = k < d and kp >= dp and k > thr["kd_sell"]
    kd_note = (f"當前 K={k:.1f} D={d:.1f}｜"
               f"買進區：K<{thr['kd_buy']}且K上穿D｜"
               f"賣出區：K>{thr['kd_sell']}且K下穿D｜"
               f"正常區間：{thr['kd_buy']}～{thr['kd_sell']}")
    if kd_buy:
        l2_buy += 1
        items.append(("KD", "低檔黃金交叉 ✅", "#2ecc71", kd_note))
    elif kd_sell:
        l2_sell += 1
        items.append(("KD", "高檔死亡交叉 ⚠️", "#e74c3c", kd_note))
    else:
        items.append(("KD", "無交叉訊號", "#95a5a6", kd_note))

    # 短線乖離率（使用個股門檻）
    b20_buy  = thr.get("bias20_buy",  thr.get("bias_buy",  -4.0))
    b20_sell = thr.get("bias20_sell", thr.get("bias_sell",  5.0))
    bias20_note = (f"當前={bias20:.2f}%（收盤偏離MA{m}的幅度）｜"
                   f"正常區間：{b20_buy}%～+{b20_sell}%｜"
                   f"低於{b20_buy}%=跌深買進區，高於+{b20_sell}%=漲多賣出區")
    if bias20 < b20_buy:
        l2_buy += 1
        items.append(("乖離率(MA{})".format(m), "跌深反彈機會 ✅", "#2ecc71", bias20_note))
    elif bias20 > b20_sell:
        l2_sell += 1
        items.append(("乖離率(MA{})".format(m), "漲幅過高警示 ⚠️", "#e74c3c", bias20_note))
    else:
        items.append(("乖離率(MA{})".format(m), "正常範圍", "#95a5a6", bias20_note))

    # 均線交叉
    ma_bull = ma_m > ma_l and ma_m_prev <= ma_l_prev
    ma_bear = ma_m < ma_l and ma_m_prev >= ma_l_prev
    ma_note = (f"MA{s}={ma_s:.1f}｜MA{m}={ma_m:.1f}｜MA{l}={ma_l:.1f}｜"
               f"MA{m}>MA{l}=多頭排列，MA{m}<MA{l}=空頭排列｜"
               f"剛發生交叉才觸發訊號，持續排列為中性")
    if ma_bull:
        l2_buy += 1
        items.append(("均線交叉", f"MA{m}上穿MA{l} ✅", "#2ecc71", ma_note + "｜趨勢剛確立"))
    elif ma_bear:
        l2_sell += 1
        items.append(("均線交叉", f"MA{m}下穿MA{l} ⚠️", "#e74c3c", ma_note + "｜趨勢剛反轉"))
    else:
        rel = ">" if ma_m > ma_l else "<"
        status = "多頭排列持續" if ma_m > ma_l else "空頭排列持續"
        items.append(("均線交叉", status, "#95a5a6", ma_note))

    # 量能趨勢（可關閉）
    vol_ratio = vol / vol_ma if vol_ma > 0 else 1
    if use_vol:
        vol_note = (f"今日成交量／{thr['vol_ma_period']}日均量={vol_ratio:.2f}倍｜"
                    f"正常範圍：0.8～1.2倍｜"
                    f">1.2倍且價漲=量能擴張買訊，<0.8倍=量能萎縮警示")
        if vol_trend > 0 and vol_ratio > 1.2:
            vol_label, vol_color = "量能擴張 ✅", "#2ecc71"
            if close > float(prev["Close"]): l2_buy += 1
        elif vol_trend < 0 and vol_ratio < 0.8:
            vol_label, vol_color = "量能萎縮 ⚠️", "#e74c3c"
        else:
            vol_label, vol_color = "量能平穩", "#95a5a6"
        items.append(("量能趨勢", vol_label, vol_color, vol_note))
    else:
        items.append(("量能趨勢", "已關閉（槓桿ETF不適用）", "#bdc3c7",
                      "槓桿ETF成交量主要來自當沖套利，無法反映真實多空"))

    # OBV（可關閉）
    if use_obv:
        obv_rising  = obv > obv_ma and obv > obv_prev
        obv_falling = obv < obv_ma and obv < obv_prev
        price_up    = close > float(prev["Close"])
        obv_note = (f"OBV={'高於' if obv>obv_ma else '低於'}{thr['obv_ma_period']}日均線｜"
                    f"OBV持續累積=買盤入場，OBV持續下滑=賣盤出場｜"
                    f"OBV領先價格=強力買訊，價漲OBV跌=背離警示")
        if obv_rising and price_up:
            obv_label, obv_color = "量價齊揚 ✅",    "#2ecc71"; l2_buy  += 1
        elif obv_rising and not price_up:
            obv_label, obv_color = "OBV領先價格 💡", "#3498db"
        elif obv_falling and not price_up:
            obv_label, obv_color = "量價齊跌 ⚠️",   "#e74c3c"; l2_sell += 1
        elif obv_falling and price_up:
            obv_label, obv_color = "價漲量縮背離 ⚠️","#f39c12"
        else:
            obv_label, obv_color = "OBV中性",        "#95a5a6"
        items.append(("OBV", obv_label, obv_color, obv_note))
    else:
        items.append(("OBV", "已關閉（槓桿ETF不適用）", "#bdc3c7",
                      "槓桿ETF成交量結構特殊，OBV訊號不具參考價值"))

    # 價格行為
    is_red   = close > float(latest["Open"])
    open_p   = float(latest["Open"])
    chg_pct  = (close - open_p) / open_p * 100
    price_note = (f"開盤={open_p:.2f}｜收盤={close:.2f}｜當日漲跌={chg_pct:+.2f}%｜"
                  f"紅K=收盤>開盤（買方強勢），黑K=收盤<開盤（賣方強勢）")
    items.append(("價格行為",
                  f"紅K（+{chg_pct:.2f}%）" if is_red else f"黑K（{chg_pct:.2f}%）",
                  "#2ecc71" if is_red else "#e74c3c",
                  price_note))

    # ── 綜合訊號 ──────────────────────────────────────────────
    if b60["locked"]:
        if l2_sell >= 2 or trend == "bear":
            level, emoji, summary = "STRONG_SELL", "🔵", "強賣出訊號"
            advice = f"市場過熱且技術面轉弱，建議出場"
            bg, border = "#eaf4fb", "#3498db"
        else:
            level, emoji, summary = "OVERHEATED", "🔥", "過熱鎖定｜禁止追買"
            advice = (f"季線乖離{b60['bias60']:.1f}%超過歷史{scfg['thresholds'].get('bias60_p_high',95)}%分位"
                      f"({b60['p_high']:.1f}%)，Z={b60['z_score']:.2f}，強制停止買進")
            bg, border = "#fdecea", "#c0392b"

    elif b60["zone"] == "oversold":
        if trend in ("healthy_bull","weak_bull") and l2_buy >= 1:
            level, emoji, summary = "STRONG_BUY", "🔴", "強買進訊號（超跌加碼區）"
            advice = (f"季線乖離{b60['bias60']:.1f}%低於歷史{scfg['thresholds'].get('bias60_p_low',5)}%分位"
                      f"({b60['p_low']:.1f}%)，統計超跌，高信心建倉機會")
            bg, border = "#fdecea", "#e74c3c"
        else:
            level, emoji, summary = "WEAK_BUY", "🟡", "超跌觀察區"
            advice = "季線乖離統計超跌，但技術面尚未確認，可列入觀察"
            bg, border = "#fef9e7", "#f39c12"

    else:
        if trend == "healthy_bull" and l2_buy >= 2:
            level, emoji, summary = "STRONG_BUY",  "🔴", "強買進訊號"
            advice = "多頭健康，多指標共振，建議關注進場機會"
            bg, border = "#fdecea", "#e74c3c"
        elif (trend == "healthy_bull" and l2_buy == 1) or \
             (trend == "weak_bull"    and l2_buy >= 2):
            level, emoji, summary = "WEAK_BUY",    "🟡", "弱買進提醒"
            advice = "單一訊號或趨勢轉弱，列入觀察，勿躁進"
            bg, border = "#fef9e7", "#f39c12"
        elif trend in ("weak_bull","healthy_bull") and not ma_s_dir:
            level, emoji, summary = "WARNING",     "🟠", "風險警示"
            advice = f"{s}日線走弱，建議降低部位或暫緩操作"
            bg, border = "#fef5e7", "#e67e22"
        elif trend == "bear" and l2_sell >= 2:
            level, emoji, summary = "STRONG_SELL", "🔵", "強賣出訊號"
            advice = "空頭確認，多指標共振，建議考慮出場"
            bg, border = "#eaf4fb", "#3498db"
        elif trend == "neutral":
            level, emoji, summary = "NEUTRAL",     "⚪", "方向不明"
            advice = "均線糾結或訊號矛盾，建議觀望"
            bg, border = "#f8f9fa", "#95a5a6"
        else:
            level, emoji, summary = "NEUTRAL",     "⚪", "無明顯訊號"
            advice = "目前無強烈進出依據，繼續觀察"
            bg, border = "#f8f9fa", "#95a5a6"

    pyramid = calc_pyramid(df, scfg, level)

    return dict(
        level=level, emoji=emoji, summary=summary, advice=advice,
        bg=bg, border=border, items=items,
        close=close, bias20=bias20, is_red=is_red,
        l2_buy=l2_buy, l2_sell=l2_sell,
        b60=b60, pyramid=pyramid,
    )


# ── 產生單檔 HTML 區塊 ───────────────────────────────────────
def stock_html_block(name: str, ticker: str, result: dict, note: str = "") -> str:
    rows = ""
    for label, value, color, n in result["items"]:
        rows += (f'<tr>'
                 f'<td style="padding:6px 10px;color:#555;width:110px;font-size:13px;">{label}</td>'
                 f'<td style="padding:6px 10px;font-weight:bold;color:{color};font-size:13px;">{value}</td>'
                 f'<td style="padding:6px 10px;color:#777;font-size:11px;">{n}</td>'
                 f'</tr>')

    note_html = ""
    if note:
        note_html = (f'<div style="background:#fef9e7;padding:6px 16px;'
                     f'font-size:11px;color:#7d6608;border-bottom:1px solid #eee;">'
                     f'💡 {note}</div>')

    pyramid_html = ""
    if result["pyramid"]["suggestions"]:
        sugg = "".join(f'<li style="margin:4px 0;font-size:13px;">{s}</li>'
                       for s in result["pyramid"]["suggestions"])
        pyramid_html = (f'<div style="background:#f0f8ff;padding:10px 16px;border-top:1px solid #d6eaf8;">'
                        f'<div style="font-weight:bold;color:#2471a3;margin-bottom:4px;">🏗️ 金字塔建倉建議</div>'
                        f'<ul style="margin:0;padding-left:18px;">{sugg}</ul></div>')

    return (f'<div style="margin-bottom:28px;border:2px solid {result["border"]};'
            f'border-radius:10px;overflow:hidden;background:#fff;">'
            f'<div style="background:{result["border"]};padding:12px 16px;'
            f'display:flex;justify-content:space-between;align-items:center;">'
            f'<span style="color:#fff;font-size:16px;font-weight:bold;">'
            f'{result["emoji"]} {name} ({ticker.replace(".TW","").replace(".tw","")})</span>'
            f'<span style="color:#fff;font-size:20px;font-weight:bold;">{result["close"]:.2f}</span>'
            f'</div>'
            f'{note_html}'
            f'<div style="background:{result["bg"]};padding:10px 16px;border-bottom:1px solid #eee;">'
            f'<strong>{result["summary"]}</strong>'
            f'<span style="color:#555;margin-left:8px;">— {result["advice"]}</span></div>'
            f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'
            f'{pyramid_html}</div>')


# ── 產生總覽表格 ─────────────────────────────────────────────
def summary_table(results: list) -> str:
    rows = ""
    for name, ticker, r in results:
        badge = ""
        if r["b60"]["zone"] == "overheated":
            badge = (' <span style="background:#c0392b;color:#fff;'
                     'font-size:10px;padding:2px 6px;border-radius:4px;">🔥過熱</span>')
        elif r["b60"]["zone"] == "oversold":
            badge = (' <span style="background:#2980b9;color:#fff;'
                     'font-size:10px;padding:2px 6px;border-radius:4px;">❄️超跌</span>')
        rows += (f'<tr style="border-bottom:1px solid #eee;">'
                 f'<td style="padding:8px 12px;">{name}{badge}</td>'
                 f'<td style="padding:8px 12px;color:#777;">'
                 f'{ticker.replace(".TW","").replace(".tw","")}</td>'
                 f'<td style="padding:8px 12px;font-weight:bold;">{r["close"]:.2f}</td>'
                 f'<td style="padding:8px 12px;font-size:18px;">{r["emoji"]}</td>'
                 f'<td style="padding:8px 12px;font-weight:bold;color:{r["border"]};">{r["summary"]}</td>'
                 f'<td style="padding:8px 12px;color:#777;font-size:12px;">'
                 f'BIAS60={r["b60"]["bias60"]:.1f}%</td>'
                 f'</tr>')
    return (f'<table style="width:100%;border-collapse:collapse;margin-bottom:28px;'
            f'border:1px solid #ddd;border-radius:8px;overflow:hidden;">'
            f'<thead><tr style="background:#2c3e50;color:#fff;">'
            f'<th style="padding:10px 12px;text-align:left;">股票名稱</th>'
            f'<th style="padding:10px 12px;text-align:left;">代號</th>'
            f'<th style="padding:10px 12px;text-align:left;">收盤價</th>'
            f'<th style="padding:10px 12px;text-align:left;">訊號</th>'
            f'<th style="padding:10px 12px;text-align:left;">說明</th>'
            f'<th style="padding:10px 12px;text-align:left;">季線乖離</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>')


# ── 組裝 HTML Email ──────────────────────────────────────────
def build_email_html(results: list, today: str) -> str:
    overview = summary_table(results)
    details  = "".join(
        stock_html_block(n, t, r, note=r.get("stock_note",""))
        for n, t, r in results)
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
            f'<body style="font-family:Arial,sans-serif;max-width:720px;margin:0 auto;'
            f'padding:20px;background:#f4f6f8;">'
            f'<div style="background:#2c3e50;color:#fff;padding:20px;'
            f'border-radius:10px 10px 0 0;text-align:center;">'
            f'<h2 style="margin:0;">📊 每日股市訊號報告</h2>'
            f'<p style="margin:6px 0 0;opacity:.8;">{today}｜收盤後分析</p></div>'
            f'<div style="background:#fff;padding:24px;border-radius:0 0 10px 10px;'
            f'box-shadow:0 2px 8px rgba(0,0,0,.08);">'
            f'<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">今日總覽</h3>'
            f'{overview}'
            f'<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">各股詳細指標</h3>'
            f'{details}'
            f'<p style="color:#aaa;font-size:11px;text-align:center;'
            f'border-top:1px solid #eee;padding-top:12px;margin-top:8px;">'
            f'⚠️ 本報告由自動化程式產生，僅供參考，不構成投資建議。</p>'
            f'</div></body></html>')


# ── 發送 Email ───────────────────────────────────────────────
def send_email(cfg: dict, html: str, today: str) -> bool:
    gmail_pass = os.environ.get("GMAIL_PASSWORD", "")
    if not gmail_pass:
        print("⚠️  未設定 GMAIL_PASSWORD（GitHub Secret），跳過發信")
        return False
    ec  = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = ec["subject"].format(date=today)
    msg["From"]    = ec["from"]
    msg["To"]      = ec["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(ec["from"], gmail_pass)
        s.sendmail(ec["from"], ec["to"], msg.as_string())
    return True


# ── 主流程 ───────────────────────────────────────────────────
def main():
    cfg   = load_config()
    today = datetime.today().strftime("%Y/%m/%d")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 開始分析，共 {len(cfg['watchlist'])} 檔")

    results = []
    for stock in cfg["watchlist"]:
        ticker = stock["ticker"]
        name   = stock["name"]
        note   = stock.get("note", "")
        print(f"  {name} ({ticker}) ...", end=" ")
        try:
            scfg = get_stock_cfg(stock, cfg)
            df   = fetch_data(ticker, cfg["lookback_days"])
            df   = calc_indicators(df, scfg)
            r    = evaluate(df, scfg)
            r["stock_note"] = note
            results.append((name, ticker, r))
            print(f"{r['emoji']} {r['summary']} | BIAS60={r['b60']['bias60']:.1f}%")
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


if __name__ == "__main__":
    main()
