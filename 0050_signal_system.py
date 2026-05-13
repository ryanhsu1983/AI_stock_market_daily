"""
每日股市訊號系統 v4
===================
Repository : github.com/ryanhsu1983/AI_stock_0050
v4 新增：每檔股票獨立 overrides 設定，支援個別化指標門檻與開關
"""

import html as html_lib
import base64, json, os, re, smtplib, sys, requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CACHE_DIR = Path(__file__).parent / ".yfinance_cache"
CACHE_DIR.mkdir(exist_ok=True)
yf.cache.set_cache_location(str(CACHE_DIR))

UP_COLOR = "#c0392b"
DOWN_COLOR = "#168f4d"
WARN_COLOR = "#e67e22"
INFO_COLOR = "#3498db"
NEUTRAL_COLOR = "#95a5a6"
TAIPEI_TZ = timezone(timedelta(hours=8))

WEIGHTS = {
    "trend": 25,
    "macd": 20,
    "institutional": 15,
    "kd": 12,
    "obv": 8,
    "fx": 7,
    "rates": 7,
    "vol": 6,
}

SIGNAL_LEVELS = [
    (70, "STRONG", "強訊號"),
    (50, "MID", "中訊號"),
    (30, "WEAK", "弱訊號"),
    (15, "NOTICE", "提醒"),
    (0, "NEUTRAL", "無訊號"),
]

TRADE_BASE_PCTS = {
    "STRONG": 50,
    "MID": 40,
    "WEAK": 10,
    "NOTICE": 0,
    "NEUTRAL": 0,
}


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
        "use_institutional":ov.get("use_institutional", True),
        "use_fx":           ov.get("use_fx",            True),
        "use_rates":        ov.get("use_rates",         True),
        "macro_sensitivity": ov.get("macro_sensitivity", "market"),
        "leverage_warning": ov.get("leverage_warning",  False),
        "bias60_locked":    ov.get("bias60_locked",     True),
    }


def _parse_int(value) -> int:
    try:
        return int(str(value).replace(",", "").replace(" ", ""))
    except Exception:
        return 0


def _find_field(fields: list, *keywords: str) -> int | None:
    for idx, field in enumerate(fields):
        if all(keyword in field for keyword in keywords):
            return idx
    return None


def _find_exact_field(fields: list, name: str) -> int | None:
    try:
        return fields.index(name)
    except ValueError:
        return None


# ── 三大法人資料 ─────────────────────────────────────────────
def fetch_institutional(ticker: str, lookback_days: int = 7) -> dict:
    stock_id = ticker.upper().replace(".TW", "").replace(".TWO", "")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.twse.com.tw/",
    }

    last_error = ""
    for offset in range(lookback_days):
        date_str = (datetime.today() - timedelta(days=offset)).strftime("%Y%m%d")
        url = (
            "https://www.twse.com.tw/rwd/zh/fund/T86"
            f"?response=json&date={date_str}&selectType=ALL"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            last_error = f"證交所連線失敗:{str(exc)[:80]}"
            continue

        if data.get("stat") != "OK":
            last_error = f"{date_str} 狀態:{data.get('stat')}"
            continue

        fields = data.get("fields", [])
        rows = data.get("data", [])
        idx_id = _find_field(fields, "證券代號")
        idx_foreign = (
            _find_exact_field(fields, "外陸資買賣超股數(不含外資自營商)")
            or _find_field(fields, "外陸資", "買賣超")
        )
        idx_invest = _find_exact_field(fields, "投信買賣超股數") or _find_field(fields, "投信", "買賣超")
        idx_dealer = _find_exact_field(fields, "自營商買賣超股數")
        idx_total = _find_exact_field(fields, "三大法人買賣超股數")

        if None in (idx_id, idx_foreign, idx_invest, idx_dealer):
            last_error = f"{date_str} 欄位格式異動"
            continue

        for row in rows:
            if str(row[idx_id]).strip() == stock_id:
                foreign = _parse_int(row[idx_foreign])
                invest = _parse_int(row[idx_invest])
                dealer = _parse_int(row[idx_dealer])
                total = _parse_int(row[idx_total]) if idx_total is not None else foreign + invest + dealer
                return {
                    "success": True,
                    "date": date_str,
                    "foreign_net": foreign,
                    "invest_net": invest,
                    "dealer_net": dealer,
                    "total_net": total,
                    "error": "",
                }
        last_error = f"{date_str} 找不到 {stock_id}"

    return {
        "success": False,
        "date": "",
        "foreign_net": 0,
        "invest_net": 0,
        "dealer_net": 0,
        "total_net": 0,
        "error": last_error or "無三大法人資料",
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


def _fetch_close_series(ticker: str, days: int = 180) -> pd.Series:
    end   = datetime.today()
    start = end - timedelta(days=days)
    df = yf.download(ticker,
                     start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"),
                     progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"無法取得 {ticker} 資料")
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df["Close"].dropna()


def _series_change_pct(series: pd.Series, periods: int) -> float | None:
    if len(series) <= periods:
        return None
    prev = float(series.iloc[-1 - periods])
    if prev == 0:
        return None
    return (float(series.iloc[-1]) - prev) / prev * 100


def fetch_market_context() -> dict:
    """
    抓取每日會影響台股風險偏好的總體資料。
    USD/TWD 上升代表美元變貴、台幣轉弱；美債殖利率上升代表估值壓力提高。
    """
    context = {"success": True, "fx": None, "rates": None, "errors": []}

    try:
        fx = _fetch_close_series("TWD=X", 180)
        context["fx"] = {
            "ticker": "TWD=X",
            "label": "美元/台幣",
            "value": float(fx.iloc[-1]),
            "chg_5d_pct": _series_change_pct(fx, 5),
            "chg_20d_pct": _series_change_pct(fx, 20),
        }
    except Exception as exc:
        context["success"] = False
        context["errors"].append(f"匯率資料失敗:{str(exc)[:80]}")

    try:
        rates = _fetch_close_series("^TNX", 180)
        current = float(rates.iloc[-1])
        context["rates"] = {
            "ticker": "^TNX",
            "label": "美國10年期公債殖利率",
            "value": current,
            "chg_5d_bp": (current - float(rates.iloc[-6])) * 100 if len(rates) > 5 else None,
            "chg_20d_bp": (current - float(rates.iloc[-21])) * 100 if len(rates) > 20 else None,
        }
    except Exception as exc:
        context["success"] = False
        context["errors"].append(f"利率資料失敗:{str(exc)[:80]}")

    return context


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
        color  = UP_COLOR
        note   = f"Z={z:.2f}｜超過歷史{p_high_pct}%分位({p_high:.1f}%)｜{'強制禁止買進' if can_lock else '僅警示，不鎖定'}"
    elif bias60 <= p_low:
        zone   = "oversold"
        locked = False
        label  = f"❄️ 超跌部署區（季線乖離{bias60:.1f}%，歷史{p_low_pct}%分位）"
        color  = DOWN_COLOR
        note   = f"Z={z:.2f}｜低於歷史{p_low_pct}%分位({p_low:.1f}%)｜統計黃金建倉區"
    else:
        zone   = "normal"
        locked = False
        label  = f"正常範圍（季線乖離{bias60:.1f}%）"
        color  = NEUTRAL_COLOR
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

    if signal_level.startswith("BUY_"):
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


def score_to_signal(score: float) -> tuple:
    for threshold, key, label in SIGNAL_LEVELS:
        if score >= threshold:
            return key, label
    return "NEUTRAL", "無訊號"


def classify_market_regime(close: float, ma_s: float, ma_m: float, ma_l: float,
                           ma_s_prev: float, ma_m_prev: float, ma_l_prev: float) -> dict:
    ma_s_up = ma_s > ma_s_prev
    ma_m_up = ma_m > ma_m_prev
    ma_l_up = ma_l > ma_l_prev

    if ma_m > ma_l and close > ma_m and ma_s_up and ma_m_up and ma_l_up:
        return {
            "key": "STRONG_BULL",
            "label": "大多頭",
            "color": UP_COLOR,
            "note": "中期均線維持多頭排列，價格也站在主要均線上方；此時重點是抱住核心部位，不因短線弱賣出訊號頻繁下車。",
        }
    if ma_m > ma_l:
        return {
            "key": "BULL_PULLBACK",
            "label": "多頭修正",
            "color": WARN_COLOR,
            "note": "中期仍是多頭，但短線轉弱或跌回均線附近；此時適合觀察是否回到支撐，而不是把它直接當成空頭。",
        }
    if ma_m < ma_l and close < ma_m:
        return {
            "key": "BEAR",
            "label": "空頭",
            "color": DOWN_COLOR,
            "note": "中期均線偏空且價格落在主要均線下方；此時賣出訊號權重提高，買進訊號需更保守。",
        }
    return {
        "key": "RANGE",
        "label": "盤整",
        "color": NEUTRAL_COLOR,
        "note": "趨勢方向尚未明確；此時可依分數分批，但不宜把單一弱訊號視為重倉依據。",
    }


def _parse_signal_level(level: str) -> tuple:
    if level.startswith("BUY_"):
        return "BUY", level.replace("BUY_", "")
    if level.startswith("SELL_"):
        return "SELL", level.replace("SELL_", "")
    if level.startswith("OVERHEATED_"):
        return "OVERHEATED", level.replace("OVERHEATED_", "")
    return "HOLD", "NEUTRAL"


def build_trade_plan(level: str, regime: dict, b60: dict, lev_warn: bool = False) -> dict:
    direction, level_key = _parse_signal_level(level)
    base_pct = TRADE_BASE_PCTS.get(level_key, 0)
    regime_key = regime["key"]
    action = "觀察"
    trade_pct = 0
    color = NEUTRAL_COLOR
    headline = "不建議交易"
    reason = "目前訊號不足，保留觀察即可。"

    if direction == "BUY":
        action = "買進或加碼"
        color = UP_COLOR
        if regime_key == "BEAR":
            trade_pct = {"STRONG": 20, "MID": 10, "WEAK": 0, "NOTICE": 0, "NEUTRAL": 0}.get(level_key, 0)
            reason = "空頭環境下即使出現買訊，也先視為反彈或試單，不建議直接重倉。"
        elif regime_key == "STRONG_BULL" and b60["zone"] == "overheated":
            trade_pct = 0
            action = "暫停追買"
            color = WARN_COLOR
            reason = "大多頭仍可續抱，但季線乖離已高，不建議用新資金追價。"
        elif regime_key == "STRONG_BULL":
            trade_pct = base_pct
            reason = "大多頭環境下，買訊可順勢執行，但仍只在訊號首次出現或升級時加碼。"
        elif regime_key == "BULL_PULLBACK":
            trade_pct = base_pct
            reason = "多頭修正中的買訊較有分批布局意義，但仍需保留後續加碼空間。"
        else:
            trade_pct = base_pct
            reason = "盤整環境下依訊號分批，不一次打滿部位。"

    elif direction == "SELL":
        action = "賣出或減碼"
        color = DOWN_COLOR
        if regime_key == "STRONG_BULL":
            trade_pct = {"STRONG": 30, "MID": 10, "WEAK": 0, "NOTICE": 0, "NEUTRAL": 0}.get(level_key, 0)
            reason = "大多頭下弱賣出通常只是震盪提醒；中訊號才小幅降風險，強訊號再明顯減碼。"
        elif regime_key == "BULL_PULLBACK":
            trade_pct = {"STRONG": 40, "MID": 20, "WEAK": 0, "NOTICE": 0, "NEUTRAL": 0}.get(level_key, 0)
            reason = "多頭修正時先守核心持股，弱賣出不急著動作，中強訊號才分批降部位。"
        elif regime_key == "BEAR":
            trade_pct = base_pct
            reason = "空頭環境下賣出訊號可信度提高，可依原始比例控管風險。"
        else:
            trade_pct = base_pct
            reason = "盤整環境下依原始比例分批，避免單日判斷過度影響部位。"

    elif direction == "OVERHEATED":
        action = "禁止追買"
        color = WARN_COLOR
        if level_key in ("MID", "STRONG"):
            if regime_key == "STRONG_BULL":
                trade_pct = 10 if level_key == "MID" else 30
                reason = "行情仍屬大多頭，但已過熱且賣壓分數升高；以小幅停利或降低槓桿為主，不清空核心部位。"
            elif regime_key == "BULL_PULLBACK":
                trade_pct = 20 if level_key == "MID" else 40
                reason = "過熱後進入修正，賣壓分數已不低，可分批降部位並等待下一次整理。"
            else:
                trade_pct = base_pct
                reason = "過熱且賣壓明顯，先降低風險，不新增買進。"
            action = "減碼"
            color = DOWN_COLOR
        else:
            trade_pct = 0
            reason = "過熱代表不追買；但賣出分數還不夠強，若處在多頭中不建議只因過熱就下車。"

    if level_key == "NOTICE":
        trade_pct = 0
        reason = "提醒等級只代表市場溫度有變化，不作為實際交易依據。"

    if lev_warn and trade_pct > 0:
        trade_pct = min(trade_pct, 20)
        reason += " 槓桿ETF波動與耗損較高，單次動作上限先壓低。"

    if trade_pct > 0:
        headline = f"{action} {trade_pct}%"
    elif action == "暫停追買":
        headline = "暫停追買"
    elif action == "禁止追買":
        headline = "禁止追買，核心部位續抱觀察"
    else:
        headline = "不交易，觀察"

    return {
        "headline": headline,
        "action": action,
        "trade_pct": trade_pct,
        "base_pct": base_pct,
        "color": color,
        "reason": reason,
        "regime": regime,
        "repeat_rule": "同一等級訊號連續出現時，不建議每天重複交易；只有首次出現、訊號升級，或部位尚未達計畫比例時才執行。",
    }


def trade_plan_html(result: dict, compact: bool = False) -> str:
    trade_plan = result.get("trade_plan", {})
    if not trade_plan:
        return ""

    regime = trade_plan.get("regime", result.get("regime", {}))
    status_tags = (
        f'<span style="background:{regime.get("color", NEUTRAL_COLOR)};color:#fff;'
        f'font-size:12px;font-weight:bold;padding:4px 8px;border-radius:5px;'
        f'white-space:nowrap;display:inline-block;margin-right:6px;">'
        f'{regime.get("label", "市場狀態不明")}</span>'
    )
    if result.get("b60", {}).get("zone") == "overheated":
        status_tags += (
            f'<span style="background:#c0392b;color:#fff;font-size:12px;'
            f'font-weight:bold;padding:4px 8px;border-radius:5px;white-space:nowrap;'
            f'display:inline-block;margin-right:6px;">過熱鎖定</span>'
        )
    elif result.get("b60", {}).get("zone") == "oversold":
        status_tags += (
            f'<span style="background:#2980b9;color:#fff;font-size:12px;'
            f'font-weight:bold;padding:4px 8px;border-radius:5px;white-space:nowrap;'
            f'display:inline-block;margin-right:6px;">超跌區</span>'
        )

    margin = "margin-top:8px;" if compact else ""
    return (
        f'<div style="{margin}background:#fff;border:1px solid #eee;border-left:5px solid '
        f'{trade_plan.get("color", NEUTRAL_COLOR)};border-radius:8px;padding:10px 12px;">'
        f'<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:7px;">'
        f'{status_tags}'
        f'<span style="color:{trade_plan.get("color", NEUTRAL_COLOR)};'
        f'font-size:15px;font-weight:bold;">{trade_plan.get("headline", "不交易，觀察")}</span>'
        f'</div>'
        f'<div style="font-size:12px;color:#555;line-height:1.7;">'
        f'{trade_plan.get("reason", "")}</div>'
        f'</div>'
    )


def _direction_style(direction: str, level_key: str, locked: bool = False) -> tuple:
    if locked:
        return "🔥", "#fdecea", UP_COLOR
    if direction == "buy":
        return {
            "STRONG": ("🔴", "#fdecea", UP_COLOR),
            "MID": ("🟠", "#fef5e7", WARN_COLOR),
            "WEAK": ("🟡", "#fef9e7", "#f39c12"),
            "NOTICE": ("🔵", "#eaf4fb", INFO_COLOR),
            "NEUTRAL": ("⚪", "#f8f9fa", NEUTRAL_COLOR),
        }.get(level_key, ("⚪", "#f8f9fa", NEUTRAL_COLOR))
    return {
        "STRONG": ("🟢", "#eafaf1", DOWN_COLOR),
        "MID": ("🟣", "#f4ecf7", "#8e44ad"),
        "WEAK": ("🟡", "#f8f9fa", "#7f8c8d"),
        "NOTICE": ("⚪", "#f8f9fa", NEUTRAL_COLOR),
        "NEUTRAL": ("⚪", "#f8f9fa", NEUTRAL_COLOR),
    }.get(level_key, ("⚪", "#f8f9fa", NEUTRAL_COLOR))


def format_market_value(value: float, unit: str = "張") -> str:
    if value > 0:
        return f'<span style="color:{UP_COLOR};font-weight:bold;">買超 {value:.0f}{unit}</span>'
    if value < 0:
        return f'<span style="color:{DOWN_COLOR};font-weight:bold;">賣超 {abs(value):.0f}{unit}</span>'
    return f'平盤 0{unit}'


def format_ratio_value(value: float) -> str:
    if value > 0:
        return f'<span style="color:{UP_COLOR};font-weight:bold;">+{value:.2f}%</span>'
    if value < 0:
        return f'<span style="color:{DOWN_COLOR};font-weight:bold;">{value:.2f}%</span>'
    return "0.00%"


# ── 評估訊號 ────────────────────────────────────────────────
def evaluate(df: pd.DataFrame, scfg: dict, inst: dict | None = None) -> dict:
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
                  f"收盤{'站上' if above_ma_s else '跌破'}{s}日線（{s}日線{'向上' if ma_s_dir else '向下'}）｜"
                  f"多頭健康：MA{m}>MA{l} 且收盤站上{s}日線｜"
                  f"多頭轉弱：MA{m}>MA{l} 但跌破{s}日線或{s}日線轉向｜"
                  f"空頭確認：MA{m}<MA{l}"))
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
                  f"紅K：收盤>開盤，買方強勢｜黑K：收盤<開盤，賣方強勢｜"
                  f"長上影線：上漲被壓回，賣壓重｜長下影線：下跌被撐回，買盤強")
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


def evaluate_weighted(df: pd.DataFrame, scfg: dict, inst: dict | None = None,
                      macro: dict | None = None) -> dict:
    thr = scfg["thresholds"]
    ma = scfg["ma_periods"]
    use_obv = scfg.get("use_obv", True)
    use_vol = scfg.get("use_vol_trend", True)
    use_inst = scfg.get("use_institutional", True)
    use_fx = scfg.get("use_fx", True)
    use_rates = scfg.get("use_rates", True)
    macro_sensitivity = scfg.get("macro_sensitivity", "market")
    lev_warn = scfg.get("leverage_warning", False)
    s, m, l = ma["short"], ma["mid"], ma["long"]

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest["Close"])
    prev_close = float(prev["Close"])
    ma_s = float(latest[f"MA{s}"])
    ma_m = float(latest[f"MA{m}"])
    ma_l = float(latest[f"MA{l}"])
    ma_s_prev = float(prev[f"MA{s}"])
    ma_m_prev = float(prev[f"MA{m}"])
    ma_l_prev = float(prev[f"MA{l}"])
    k, d = float(latest["K"]), float(latest["D"])
    kp, dp = float(prev["K"]), float(prev["D"])
    hist = float(latest["MACD_hist"])
    hist_p = float(prev["MACD_hist"])
    bias20 = float(latest["Bias20"])
    vol = float(latest["Volume"])
    vol_ma = float(latest["Vol_MA"])
    vol_trend = float(latest["Vol_Trend"])
    obv = float(latest["OBV"])
    obv_ma = float(latest["OBV_MA"])
    obv_prev = float(prev["OBV"])

    items = []
    buy_score = 0.0
    sell_score = 0.0
    max_possible = float(sum(WEIGHTS.values()))

    def add_item(label, value, color, note, buy=0.0, sell=0.0):
        nonlocal buy_score, sell_score
        buy_score += buy
        sell_score += sell
        if buy or sell:
            note = f"{note}｜分數影響:買進+{buy:.0f}/賣出+{sell:.0f}"
        items.append((label, value, color, note))

    if lev_warn:
        add_item("⚠️ 槓桿警示", "每日重置ETF，不適合長抱", "#e67e22",
                 "槓桿ETF有長期耗損效應，僅適合短線波段操作")

    b60 = eval_bias60(df, scfg)
    add_item("BIAS60 Z-Score", b60["label"], b60["color"],
             b60["note"] + "｜用途:判斷中期位置是否過熱或超跌；過熱時不建議追買")

    ma_s_dir = ma_s > ma_s_prev
    above_ma_s = close > ma_s
    if ma_m > ma_l and above_ma_s and ma_s_dir:
        trend = "healthy_bull"
        trend_label, trend_color = "多頭健康", DOWN_COLOR
        trend_buy, trend_sell = WEIGHTS["trend"], 0
    elif ma_m > ma_l and (not above_ma_s or not ma_s_dir):
        trend = "weak_bull"
        trend_label, trend_color = "多頭轉弱", "#f39c12"
        trend_buy, trend_sell = 0, WEIGHTS["trend"] * 0.4
    elif ma_m < ma_l:
        trend = "bear"
        trend_label, trend_color = "空頭確認", UP_COLOR
        trend_buy, trend_sell = 0, WEIGHTS["trend"]
    else:
        trend = "neutral"
        trend_label, trend_color = "方向不明", NEUTRAL_COLOR
        trend_buy = trend_sell = 0
    add_item(
        "趨勢環境", trend_label, trend_color,
        f"MA{s}={ma_s:.1f}｜MA{m}={ma_m:.1f}｜MA{l}={ma_l:.1f}｜"
        f"收盤{'站上' if above_ma_s else '跌破'}{s}日線（{s}日線{'向上' if ma_s_dir else '向下'}）｜"
        f"趨勢代表目前市場主方向，是本模型最重要的判斷項目｜均線交叉已包含在趨勢判斷中，不重複加分",
        trend_buy, trend_sell,
    )
    regime = classify_market_regime(close, ma_s, ma_m, ma_l, ma_s_prev, ma_m_prev, ma_l_prev)

    if use_fx:
        fx = macro.get("fx") if macro else None
        if fx:
            fx_5d = fx.get("chg_5d_pct")
            fx_20d = fx.get("chg_20d_pct")
            fx_value = fx["value"]
            fx_note = (
                f"美元/台幣={fx_value:.3f}｜5日變動={fx_5d:+.2f}%｜20日變動={fx_20d:+.2f}%｜"
                "數字變高代表美元變貴、台幣轉弱；台幣快速貶值常伴隨外資撤出壓力，"
                "但對台積電、聯發科等出口股有部分匯兌抵銷"
            )
            exporter = macro_sensitivity == "exporter"
            full = WEIGHTS["fx"] * (0.75 if exporter else 1.0)
            half = full * 0.5
            if fx_5d is not None and fx_20d is not None and (fx_5d >= 1.0 or fx_20d >= 2.0):
                add_item("美元/台幣匯率", "台幣明顯轉弱 ⚠️", "#e67e22", fx_note, 0, full)
            elif fx_5d is not None and fx_20d is not None and (fx_5d <= -1.0 or fx_20d <= -2.0):
                add_item("美元/台幣匯率", "台幣明顯轉強 ✅", UP_COLOR, fx_note, full, 0)
            elif fx_5d is not None and fx_20d is not None and (fx_5d >= 0.5 or fx_20d >= 1.0):
                add_item("美元/台幣匯率", "台幣偏弱", "#f39c12", fx_note, 0, half)
            elif fx_5d is not None and fx_20d is not None and (fx_5d <= -0.5 or fx_20d <= -1.0):
                add_item("美元/台幣匯率", "台幣偏強", "#3498db", fx_note, half, 0)
            else:
                add_item("美元/台幣匯率", "匯率中性", NEUTRAL_COLOR, fx_note)
        else:
            reason = "；".join(macro.get("errors", [])) if macro else "未取得總體資料"
            add_item("美元/台幣匯率", "資料暫不可用", "#bdc3c7",
                     f"{reason}｜不計分，避免資料源異常影響判斷")
    else:
        add_item("美元/台幣匯率", "已關閉", "#bdc3c7", "此標的不使用匯率權重")

    if use_rates:
        rates = macro.get("rates") if macro else None
        if rates:
            rate_value = rates["value"]
            bp_5d = rates.get("chg_5d_bp")
            bp_20d = rates.get("chg_20d_bp")
            rate_note = (
                f"美國10年期殖利率={rate_value:.2f}%｜5日變動={bp_5d:+.0f}bp｜20日變動={bp_20d:+.0f}bp｜"
                "殖利率上升會提高股市折現率，通常壓抑科技股評價；殖利率下行則有利成長股估值修復"
            )
            if bp_5d is not None and bp_20d is not None and (bp_5d >= 10 or bp_20d >= 20):
                add_item("利率環境", "殖利率快速上升 ⚠️", DOWN_COLOR, rate_note, 0, WEIGHTS["rates"])
            elif bp_5d is not None and bp_20d is not None and (bp_5d <= -10 or bp_20d <= -20):
                add_item("利率環境", "殖利率明顯下行 ✅", UP_COLOR, rate_note, WEIGHTS["rates"], 0)
            elif bp_5d is not None and bp_20d is not None and (bp_5d >= 5 or bp_20d >= 10):
                add_item("利率環境", "利率偏上行", "#f39c12", rate_note, 0, WEIGHTS["rates"] * 0.5)
            elif bp_5d is not None and bp_20d is not None and (bp_5d <= -5 or bp_20d <= -10):
                add_item("利率環境", "利率偏下行", "#3498db", rate_note, WEIGHTS["rates"] * 0.5, 0)
            else:
                add_item("利率環境", "利率中性", NEUTRAL_COLOR, rate_note)
        else:
            reason = "；".join(macro.get("errors", [])) if macro else "未取得總體資料"
            add_item("利率環境", "資料暫不可用", "#bdc3c7",
                     f"{reason}｜不計分，避免資料源異常影響判斷")
    else:
        add_item("利率環境", "已關閉", "#bdc3c7", "此標的不使用利率權重")

    hist_series = df["MACD_hist"].dropna()
    hist_p10 = float(hist_series.quantile(0.10))
    hist_p90 = float(hist_series.quantile(0.90))
    macd_note = f"當前={hist:.4f}｜歷史正常區間[{hist_p10:.4f}～{hist_p90:.4f}]｜正=多頭動能，負=空頭動能"
    if hist > 0 and hist_p <= 0:
        add_item("MACD", "柱狀由負翻正 ✅", UP_COLOR, macd_note + "｜剛翻正，動能轉強", WEIGHTS["macd"], 0)
    elif hist < 0 and hist_p >= 0:
        add_item("MACD", "柱狀由正翻負 ⚠️", DOWN_COLOR, macd_note + "｜剛翻負，動能轉弱", 0, WEIGHTS["macd"])
    elif hist > 0 and hist > hist_p:
        add_item("MACD", "多頭動能延續", UP_COLOR, macd_note + "｜動能仍改善", WEIGHTS["macd"] * 0.5, 0)
    elif hist < 0 and hist < hist_p:
        add_item("MACD", "空頭動能延續", DOWN_COLOR, macd_note + "｜動能仍惡化", 0, WEIGHTS["macd"] * 0.5)
    else:
        sign = "正（多頭）" if hist > 0 else "負（空頭）"
        add_item("MACD", f"柱狀持續為{sign}", NEUTRAL_COLOR, macd_note)

    avg_vol20 = float(df["Volume"].tail(20).mean())
    if use_inst:
        if inst and inst.get("success"):
            total_net = float(inst["total_net"])
            net_ratio = total_net / avg_vol20 * 100 if avg_vol20 > 0 else 0.0
            nets = [inst["foreign_net"], inst["invest_net"], inst["dealer_net"]]
            buy_breadth = sum(1 for n in nets if n > 0)
            sell_breadth = sum(1 for n in nets if n < 0)
            inst_note = (
                f"資料日={inst['date']}｜"
                f"外資 {format_market_value(inst['foreign_net']/1000)}｜"
                f"投信 {format_market_value(inst['invest_net']/1000)}｜"
                f"自營 {format_market_value(inst['dealer_net']/1000)}｜"
                f"合計 {format_market_value(total_net/1000)}｜"
                f"占20日均量 {format_ratio_value(net_ratio)}"
            )
            if net_ratio >= 5 and buy_breadth >= 2:
                add_item("三大法人", "法人明顯買超 ✅", UP_COLOR, inst_note, WEIGHTS["institutional"], 0)
            elif net_ratio <= -5 and sell_breadth >= 2:
                add_item("三大法人", "法人明顯賣超 ⚠️", DOWN_COLOR, inst_note, 0, WEIGHTS["institutional"])
            elif net_ratio > 1 or buy_breadth >= 2:
                add_item("三大法人", "法人偏買", UP_COLOR, inst_note, WEIGHTS["institutional"] * 0.5, 0)
            elif net_ratio < -1 or sell_breadth >= 2:
                add_item("三大法人", "法人偏賣", DOWN_COLOR, inst_note, 0, WEIGHTS["institutional"] * 0.5)
            else:
                add_item("三大法人", "籌碼中性", NEUTRAL_COLOR, inst_note)
        else:
            reason = inst.get("error", "未取得資料") if inst else "未取得資料"
            add_item("三大法人", "資料暫不可用", "#bdc3c7",
                     f"{reason}｜不計分，避免資料源異常影響整體判斷")
    else:
        add_item("三大法人", "已關閉（此標的不適用）", "#bdc3c7",
                 "此標的無法直接使用個股三大法人買賣超，避免用錯資料來源")

    kd_buy = k > d and kp <= dp and k < thr["kd_buy"]
    kd_sell = k < d and kp >= dp and k > thr["kd_sell"]
    kd_note = (
        f"當前 K={k:.1f} D={d:.1f}｜買進區:K<{thr['kd_buy']}且K上穿D｜"
        f"賣出區:K>{thr['kd_sell']}且K下穿D｜KD適合抓時機，但容易鈍化"
    )
    if kd_buy:
        add_item("KD", "低檔黃金交叉 ✅", UP_COLOR, kd_note, WEIGHTS["kd"], 0)
    elif kd_sell:
        add_item("KD", "高檔死亡交叉 ⚠️", DOWN_COLOR, kd_note, 0, WEIGHTS["kd"])
    elif k > d and k < 50:
        add_item("KD", "低檔轉強但未交叉", "#3498db", kd_note, WEIGHTS["kd"] * 0.4, 0)
    elif k < d and k > 50:
        add_item("KD", "高檔轉弱但未交叉", "#f39c12", kd_note, 0, WEIGHTS["kd"] * 0.4)
    else:
        add_item("KD", "無交叉訊號", NEUTRAL_COLOR, kd_note)

    ma_bull = ma_m > ma_l and ma_m_prev <= ma_l_prev
    ma_bear = ma_m < ma_l and ma_m_prev >= ma_l_prev
    ma_note = (
        f"MA{s}={ma_s:.1f}｜MA{m}={ma_m:.1f}｜MA{l}={ma_l:.1f}｜"
        f"這項只說明均線是否剛轉向；分數已在趨勢環境反映，不另外加分"
    )
    if ma_bull:
        add_item("均線交叉", f"MA{m}上穿MA{l} ✅", UP_COLOR, ma_note)
    elif ma_bear:
        add_item("均線交叉", f"MA{m}下穿MA{l} ⚠️", DOWN_COLOR, ma_note)
    else:
        status = "多頭排列持續" if ma_m > ma_l else "空頭排列持續"
        add_item("均線交叉", status, NEUTRAL_COLOR, ma_note)

    vol_ratio = vol / vol_ma if vol_ma > 0 else 1
    if use_vol:
        vol_note = (
            f"今日成交量/{thr['vol_ma_period']}日均量={vol_ratio:.2f}倍｜"
            f"量能是確認項，權重較低"
        )
        if vol_trend > 0 and vol_ratio > 1.2 and close > prev_close:
            add_item("量能趨勢", "價漲量增 ✅", UP_COLOR, vol_note, WEIGHTS["vol"], 0)
        elif vol_trend > 0 and vol_ratio > 1.2 and close < prev_close:
            add_item("量能趨勢", "價跌量增 ⚠️", DOWN_COLOR, vol_note, 0, WEIGHTS["vol"])
        elif vol_trend < 0 and vol_ratio < 0.8 and close < prev_close:
            add_item("量能趨勢", "價跌量縮", "#f39c12", vol_note, 0, WEIGHTS["vol"] * 0.4)
        else:
            add_item("量能趨勢", "量能平穩", NEUTRAL_COLOR, vol_note)
    else:
        add_item("量能趨勢", "已關閉（此標的不適用）", "#bdc3c7",
                 "此標的成交量資料不適合直接作為多空分數")

    if use_obv:
        obv_rising = obv > obv_ma and obv > obv_prev
        obv_falling = obv < obv_ma and obv < obv_prev
        price_up = close > prev_close
        obv_note = (
            f"OBV={'高於' if obv > obv_ma else '低於'}{thr['obv_ma_period']}日均線｜"
            f"OBV可觀察量價累積，但雜訊高於趨勢與MACD"
        )
        if obv_rising and price_up:
            add_item("OBV", "量價齊揚 ✅", UP_COLOR, obv_note, WEIGHTS["obv"], 0)
        elif obv_rising and not price_up:
            add_item("OBV", "OBV領先價格 💡", "#3498db", obv_note, WEIGHTS["obv"] * 0.5, 0)
        elif obv_falling and not price_up:
            add_item("OBV", "量價齊跌 ⚠️", DOWN_COLOR, obv_note, 0, WEIGHTS["obv"])
        elif obv_falling and price_up:
            add_item("OBV", "價漲量縮背離 ⚠️", "#f39c12", obv_note, 0, WEIGHTS["obv"] * 0.5)
        else:
            add_item("OBV", "OBV中性", NEUTRAL_COLOR, obv_note)
    else:
        add_item("OBV", "已關閉（此標的不適用）", "#bdc3c7",
                 "此標的成交量結構不適合用OBV作為主要判斷")

    is_red = close > float(latest["Open"])
    open_p = float(latest["Open"])
    chg_pct = (close - open_p) / open_p * 100
    price_note = (
        f"開盤={open_p:.2f}｜收盤={close:.2f}｜當日漲跌={chg_pct:+.2f}%｜"
        f"只用來輔助理解今天盤勢，不直接加分"
    )
    add_item("價格行為",
             f"紅K（+{chg_pct:.2f}%）" if is_red else f"黑K（{chg_pct:.2f}%）",
             UP_COLOR if is_red else DOWN_COLOR, price_note)

    effective_buy = 0.0 if b60["locked"] else buy_score
    effective_sell = sell_score
    if b60["locked"]:
        level_key, level_label = score_to_signal(effective_sell)
        level, emoji = f"OVERHEATED_{level_key}", "🔥"
        if effective_sell >= 15:
            summary = f"過熱鎖定｜賣出{level_label}({effective_sell:.0f}/{max_possible:.0f}分)"
        else:
            summary = "過熱鎖定｜禁止追買"
        advice = (
            f"季線乖離{b60['bias60']:.1f}%超過歷史門檻，"
            f"原始買進分數{buy_score:.0f}分僅供參考，實際買進分數歸零"
        )
        bg, border = "#fdecea", UP_COLOR
    elif effective_buy >= effective_sell:
        score = effective_buy
        level_key, level_label = score_to_signal(score)
        emoji, bg, border = _direction_style("buy", level_key)
        level = f"BUY_{level_key}"
        prefix = "超跌買進" if b60["zone"] == "oversold" and score >= 15 else "買進"
        summary = f"{emoji} {prefix}{level_label}({score:.0f}/{max_possible:.0f}分)"
        advice = {
            "STRONG": "多項高權重指標共振，可依金字塔計畫分批執行",
            "MID": "訊號有一定一致性，可考慮小部位或分批試單",
            "WEAK": "值得關注，但仍需等待更多確認",
            "NOTICE": "微弱買進跡象，僅列入觀察",
            "NEUTRAL": "買進依據不足，繼續觀察",
        }[level_key]
    else:
        score = effective_sell
        level_key, level_label = score_to_signal(score)
        emoji, bg, border = _direction_style("sell", level_key)
        level = f"SELL_{level_key}"
        summary = f"{emoji} 賣出{level_label}({score:.0f}/{max_possible:.0f}分)"
        advice = {
            "STRONG": "多項高權重風險指標共振，應優先控管部位風險",
            "MID": "賣出訊號有一定一致性，持有者應提高警覺",
            "WEAK": "風險升溫，可檢查停損或降低追價",
            "NOTICE": "微弱賣出跡象，僅列入觀察",
            "NEUTRAL": "賣出依據不足，繼續觀察",
        }[level_key]

    trade_plan = build_trade_plan(level, regime, b60, lev_warn)
    pyramid = calc_pyramid(df, scfg, level)

    return dict(
        level=level, emoji=emoji, summary=summary, advice=advice,
        bg=bg, border=border, items=items,
        close=close, bias20=bias20, is_red=is_red,
        buy_score=buy_score, sell_score=sell_score,
        effective_buy=effective_buy, effective_sell=effective_sell,
        score_note="季線乖離過熱，買進分數已鎖定" if b60["locked"] else "",
        max_possible=max_possible, b60=b60, regime=regime,
        trade_plan=trade_plan, pyramid=pyramid,
    )


# ── 產生單檔 HTML 區塊 ───────────────────────────────────────
def stock_html_block(name: str, ticker: str, result: dict, note: str = "") -> str:
    rows = ""
    for idx, (label, value, color, n) in enumerate(result["items"]):
        # 把備註用｜切開，每段變成一個編號子項目
        parts = [p.strip() for p in n.split("｜") if p.strip()]
        note_items = "".join(
            f'<span style="display:block;margin:1px 0;">'
            f'<span style="color:#aaa;margin-right:4px;">{i+1}.</span>{p}</span>'
            for i, p in enumerate(parts)
        )
        bg_row = "#fafafa" if idx % 2 == 0 else "#ffffff"
        rows += (
            f'<tr style="background:{bg_row};border-bottom:1px solid #eee;">'
            f'<td style="padding:10px 12px;color:#555;width:22%;font-size:13px;'
            f'font-weight:bold;vertical-align:top;line-height:1.5;">{label}</td>'
            f'<td style="padding:8px 10px;font-weight:bold;color:{color};'
            f'font-size:13px;vertical-align:top;line-height:1.5;width:25%;">{value}</td>'
            f'<td style="padding:10px 12px;color:#666;font-size:12px;'
            f'line-height:1.6;vertical-align:top;">{note_items}</td>'
            f'</tr>'
        )

    note_html = ""
    if note:
        note_html = (f'<div style="background:#fef9e7;padding:8px 16px;'
                     f'font-size:12px;color:#7d6608;border-bottom:1px solid #eee;">'
                     f'💡 {note}</div>')

    trade_html = (
        f'<div style="background:#fff;padding:12px 16px;border-bottom:1px solid #eee;">'
        f'{trade_plan_html(result)}</div>'
    )

    pyramid_html = ""
    if result["pyramid"]["suggestions"]:
        sugg = "".join(f'<li style="margin:4px 0;font-size:13px;">{s}</li>'
                       for s in result["pyramid"]["suggestions"])
        pyramid_html = (f'<div style="background:#f0f8ff;padding:12px 16px;border-top:1px solid #d6eaf8;">'
                        f'<div style="font-weight:bold;color:#2471a3;margin-bottom:6px;">🏗️ 金字塔建倉建議</div>'
                        f'<ul style="margin:0;padding-left:18px;">{sugg}</ul></div>')

    return (
        f'<div style="margin-bottom:28px;border:2px solid {result["border"]};'
        f'border-radius:10px;overflow:hidden;background:#fff;">'
        # 標題列
        f'<div style="background:{result["border"]};padding:12px 16px;'
        f'display:flex;justify-content:space-between;align-items:center;">'
        f'<span style="color:#fff;font-size:16px;font-weight:bold;">'
        f'{result["emoji"]} {name} ({ticker.replace(".TW","").replace(".tw","")})</span>'
        f'<span style="color:#fff;font-size:20px;font-weight:bold;">{result["close"]:.2f}</span>'
        f'</div>'
        # 個股備註
        f'{note_html}'
        # 實際交易建議
        f'{trade_html}'
        # 指標明細表格
        f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'
        # 金字塔建議
        f'{pyramid_html}</div>'
    )


# ── 產生總覽表格 ─────────────────────────────────────────────
def summary_table(results: list) -> str:
    cards = ""
    for name, ticker, r in results:
        code = ticker.replace(".TW", "").replace(".tw", "")
        cards += (
            f'<div style="border:1px solid #ddd;border-left:5px solid {r["border"]};'
            f'border-radius:8px;padding:12px 14px;margin-bottom:12px;background:#fff;">'
            f'<div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;">'
            f'<div style="min-width:0;">'
            f'<div style="font-size:16px;font-weight:bold;color:#2c3e50;line-height:1.4;">{name}</div>'
            f'<div style="font-size:12px;color:#888;margin-top:2px;">代號 {code}</div>'
            f'</div>'
            f'<div style="text-align:right;white-space:nowrap;">'
            f'<div style="font-size:11px;color:#888;">收盤價</div>'
            f'<div style="font-size:18px;font-weight:bold;color:#2c3e50;">{r["close"]:.2f}</div>'
            f'</div></div>'
            f'{trade_plan_html(r, compact=True)}'
            f'</div>'
        )
    return f'<div style="margin-bottom:28px;">{cards}</div>'


def market_context_html(macro: dict | None) -> str:
    if not macro:
        return ""

    fx = macro.get("fx")
    rates = macro.get("rates")
    fx_html = ""
    rates_html = ""

    if fx:
        fx_html = (
            f'<div style="padding:10px 12px;border-bottom:1px solid #eee;">'
            f'<strong>美元/台幣</strong>：{fx["value"]:.3f}｜'
            f'5日 {fx["chg_5d_pct"]:+.2f}%｜20日 {fx["chg_20d_pct"]:+.2f}%'
            f'<div style="color:#777;font-size:12px;margin-top:3px;">'
            f'數字變高代表台幣轉弱；短線通常提高外資撤出與台股修正風險，但出口股有部分匯兌抵銷。</div></div>'
        )

    if rates:
        rates_html = (
            f'<div style="padding:10px 12px;">'
            f'<strong>美國10年期公債殖利率</strong>：{rates["value"]:.2f}%｜'
            f'5日 {rates["chg_5d_bp"]:+.0f}bp｜20日 {rates["chg_20d_bp"]:+.0f}bp'
            f'<div style="color:#777;font-size:12px;margin-top:3px;">'
            f'殖利率上升通常壓抑科技股評價；殖利率下行則有利成長股估值修復。</div></div>'
        )

    if not fx_html and not rates_html:
        errors = "；".join(macro.get("errors", [])) or "未取得總體資料"
        return (f'<div style="background:#fff3cd;border:1px solid #ffeeba;'
                f'padding:10px 12px;border-radius:6px;margin-bottom:18px;'
                f'font-size:12px;color:#856404;">總體資料暫不可用：{errors}</div>')

    return (
        f'<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">總體環境</h3>'
        f'<div style="border:1px solid #ddd;border-radius:8px;overflow:hidden;margin-bottom:28px;">'
        f'{fx_html}{rates_html}</div>'
    )


def _classify_news_item(title: str) -> tuple:
    text = title.lower()
    high_keywords = ["戰爭", "開戰", "伊朗", "美伊", "霍爾木茲", "關稅", "晶片管制", "fomc", "fed", "川習", "習近平", "trump", "xi"]
    mid_keywords = ["原油", "油價", "利率", "殖利率", "匯率", "台積電", "tsmc", "nvidia", "ai", "半導體", "外資", "營收", "法說"]

    if any(k in text for k in high_keywords):
        impact = "高"
    elif any(k in text for k in mid_keywords):
        impact = "中高"
    else:
        impact = "中"

    if any(k in text for k in ["原油", "油價", "中東", "伊朗", "美伊", "霍爾木茲"]):
        note = "能源與地緣風險會影響通膨、利率預期與科技股評價；油價急漲通常壓抑風險偏好。"
        scope = "油價、通膨、全球股市、台股風險偏好"
    elif any(k in text for k in ["fed", "fomc", "利率", "殖利率"]):
        note = "利率預期會直接影響成長股估值；偏鷹訊息通常壓抑半導體與高本益比族群。"
        scope = "全球股市、美元、科技股、外資資金流"
    elif any(k in text for k in ["川習", "美中", "關稅", "晶片管制", "trump", "xi"]):
        note = "美中談判與晶片政策會影響半導體供應鏈、外資風險偏好與台股權值股評價。"
        scope = "台股、半導體、匯率、外資風險偏好"
    elif any(k in text for k in ["台積電", "tsmc", "nvidia", "ai", "半導體", "營收", "法說"]):
        note = "AI與半導體需求變化會影響台積電、聯發科與加權指數權值股表現。"
        scope = "台積電、聯發科、半導體供應鏈"
    else:
        note = "屬於市場風險偏好觀察項，需搭配價格、籌碼與總體環境判斷。"
        scope = "台股與全球風險偏好"
    return impact, scope, note


def _is_market_relevant_news(title: str) -> bool:
    text = title.lower()
    keywords = [
        "台股", "加權", "櫃買", "外資", "匯率", "台幣", "半導體", "晶片", "關稅",
        "美中", "川習", "習近平", "trump", "xi", "fed", "fomc", "利率", "殖利率",
        "原油", "油價", "中東", "伊朗", "美伊", "霍爾木茲", "台積電", "tsmc",
        "聯發科", "台達電", "鴻海", "廣達", "緯創", "緯穎", "nvidia", "ai",
        "ai伺服器", "cnyes", "鉅亨",
    ]
    return any(keyword in text for keyword in keywords)


def fetch_auto_news(cfg: dict) -> list:
    news_cfg = cfg.get("auto_news", {})
    if not news_cfg.get("enabled", False):
        return []

    queries = news_cfg.get("queries", [])
    lookback_days = int(news_cfg.get("lookback_days", 3))
    max_items = int(news_cfg.get("max_items", 8))
    max_items_per_query = int(news_cfg.get("max_items_per_query", 3))
    now = datetime.now(TAIPEI_TZ)
    min_date = now - timedelta(days=lookback_days)
    headers = {"User-Agent": "Mozilla/5.0"}
    items = []
    seen = set()

    for query in queries:
        url = (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query + f' when:{lookback_days}d')}"
            "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception:
            continue

        query_count = 0
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_text = item.findtext("pubDate", "").strip()
            source = item.findtext("source", "").strip() or "Google News"
            if not title:
                continue
            if not _is_market_relevant_news(title):
                continue
            try:
                pub_dt = parsedate_to_datetime(pub_text).astimezone(TAIPEI_TZ)
            except Exception:
                pub_dt = now
            if pub_dt < min_date:
                continue
            key = re.sub(r"\s+", "", f"{title}{source}".lower())
            if key in seen:
                continue
            impact, scope, note = _classify_news_item(title)
            seen.add(key)
            items.append({
                "date": pub_dt.strftime("%Y-%m-%d %H:%M"),
                "_published_at": pub_dt,
                "title": title,
                "impact": impact,
                "scope": scope,
                "note": note,
                "source": source,
                "link": link,
            })
            query_count += 1
            if query_count >= max_items_per_query:
                break

    impact_rank = {"高": 3, "中高": 2, "中": 1, "低": 0}
    items.sort(key=lambda x: (x["_published_at"], impact_rank.get(x["impact"], 0)), reverse=True)
    for item in items:
        item.pop("_published_at", None)
    return items[:max_items]


def market_events_html(cfg: dict, today: str, news_items: list | None = None) -> str:
    events = cfg.get("market_events", [])
    window_days = int(cfg.get("market_events_window_days", cfg.get("market_events_lookahead_days", 3)))
    today_date = datetime.strptime(today, "%Y-%m-%d").date()
    start = today_date - timedelta(days=window_days)
    end = today_date + timedelta(days=window_days)
    scheduled_rows = ""
    news_rows = ""
    impact_colors = {"高": "#c0392b", "中高": "#e67e22", "中": "#f39c12", "低": "#7f8c8d"}

    for event in events:
        try:
            event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if not (start <= event_date <= end):
            continue
        color = impact_colors.get(event.get("impact", ""), "#7f8c8d")
        scheduled_rows += (
            f'<tr style="border-bottom:1px solid #eee;">'
            f'<td style="padding:9px 12px;white-space:nowrap;color:#555;">{event["date"]}</td>'
            f'<td style="padding:9px 12px;font-weight:bold;">{event["title"]}</td>'
            f'<td style="padding:9px 12px;">'
            f'<span style="background:{color};color:#fff;font-size:11px;padding:2px 7px;border-radius:4px;white-space:nowrap;display:inline-block;">'
            f'{event.get("impact", "未評估")}</span></td>'
            f'<td style="padding:9px 12px;color:#666;font-size:12px;line-height:1.6;">'
            f'{event.get("scope", "")}｜{event.get("note", "")}'
            f'<div style="color:#aaa;margin-top:3px;">來源：{event.get("source", "手動維護")}</div></td>'
            f'</tr>'
        )

    if not scheduled_rows:
        scheduled_rows = (f'<tr><td style="padding:10px 12px;color:#777;font-size:12px;" colspan="4">'
                f'前後 {window_days} 天內尚未設定重大事件。</td></tr>')

    for item in news_items or []:
        color = impact_colors.get(item.get("impact", ""), "#7f8c8d")
        title = html_lib.escape(item.get("title", ""))
        source = html_lib.escape(item.get("source", "Google News"))
        link = html_lib.escape(item.get("link", ""))
        linked_title = f'<a href="{link}" style="color:#2c3e50;text-decoration:none;">{title}</a>' if link else title
        news_rows += (
            f'<tr style="border-bottom:1px solid #eee;">'
            f'<td style="padding:9px 12px;white-space:nowrap;color:#555;">{item.get("date", "")}</td>'
            f'<td style="padding:9px 12px;font-weight:bold;">{linked_title}</td>'
            f'<td style="padding:9px 12px;">'
            f'<span style="background:{color};color:#fff;font-size:11px;padding:2px 7px;border-radius:4px;white-space:nowrap;display:inline-block;">'
            f'{item.get("impact", "未評估")}</span></td>'
            f'<td style="padding:9px 12px;color:#666;font-size:12px;line-height:1.6;">'
            f'{item.get("scope", "")}｜{item.get("note", "")}'
            f'<div style="color:#aaa;margin-top:3px;">來源：{source}</div></td>'
            f'</tr>'
        )

    if not news_rows:
        news_rows = (f'<tr><td style="padding:10px 12px;color:#777;font-size:12px;" colspan="4">'
                     f'近 {cfg.get("auto_news", {}).get("lookback_days", 7)} 天未抓到符合條件的高關聯新聞。</td></tr>')

    return (
        f'<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">消息面與重大行事曆</h3>'
        f'<div style="font-size:12px;color:#777;margin:-12px 0 10px;">'
        f'固定行事曆顯示今天前後 {window_days} 天事件；自動新聞掃描只抓近 '
        f'{cfg.get("auto_news", {}).get("lookback_days", 3)} 天高關聯消息，例如油價、戰爭、美中、Fed與半導體新聞。</div>'
        f'<div style="font-weight:bold;color:#2c3e50;margin:4px 0 6px;">固定重大行事曆</div>'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:28px;'
        f'border:1px solid #ddd;border-radius:8px;overflow:hidden;">'
        f'<thead><tr style="background:#34495e;color:#fff;">'
        f'<th style="padding:10px 12px;text-align:left;">日期</th>'
        f'<th style="padding:10px 12px;text-align:left;">事件</th>'
        f'<th style="padding:10px 12px;text-align:left;">影響</th>'
        f'<th style="padding:10px 12px;text-align:left;">可能影響</th>'
        f'</tr></thead><tbody>{scheduled_rows}</tbody></table>'
        f'<div style="font-weight:bold;color:#2c3e50;margin:4px 0 6px;">近期自動新聞掃描</div>'
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:28px;'
        f'border:1px solid #ddd;border-radius:8px;overflow:hidden;">'
        f'<thead><tr style="background:#566573;color:#fff;">'
        f'<th style="padding:10px 12px;text-align:left;">日期</th>'
        f'<th style="padding:10px 12px;text-align:left;">新聞</th>'
        f'<th style="padding:10px 12px;text-align:left;">影響</th>'
        f'<th style="padding:10px 12px;text-align:left;">可能影響</th>'
        f'</tr></thead><tbody>{news_rows}</tbody></table>'
    )


def scoring_rules_html() -> str:
    weights = [
        ("趨勢方向", WEIGHTS["trend"], "市場主方向"),
        ("MACD動能", WEIGHTS["macd"], "漲跌動能"),
        ("三大法人", WEIGHTS["institutional"], "法人籌碼"),
        ("KD", WEIGHTS["kd"], "進出場時機"),
        ("OBV", WEIGHTS["obv"], "量價配合"),
        ("台幣匯率", WEIGHTS["fx"], "台幣強弱影響外資流向與出口股獲利"),
        ("美國利率", WEIGHTS["rates"], "利率升降影響科技股評價"),
        ("量能", WEIGHTS["vol"], "成交確認"),
    ]
    weight_rows = "".join(
        f'<tr style="border-bottom:1px solid #eee;">'
        f'<td style="padding:7px 9px;font-weight:bold;color:#2c3e50;">{name}</td>'
        f'<td style="padding:7px 9px;text-align:right;color:#c0392b;font-weight:bold;">{score}</td>'
        f'<td style="padding:7px 9px;color:#777;font-size:12px;">{meaning}</td>'
        f'</tr>'
        for name, score, meaning in weights
    )
    trade_rows = "".join(
        f'<tr style="border-bottom:1px solid #eee;">'
        f'<td style="padding:7px 9px;font-weight:bold;color:#2c3e50;">{level}</td>'
        f'<td style="padding:7px 9px;color:#777;font-size:12px;">{note}</td>'
        f'</tr>'
        for level, note in [
            ("提醒", "只提醒市場溫度變化，不作為實際交易依據"),
            ("弱訊號", "只做觀察或小幅試單，不能單獨當成重倉理由"),
            ("中訊號", "代表多項條件開始一致，可考慮分批建立或降低部位"),
            ("強訊號", "代表高權重條件共振，但仍需保留後續調整空間"),
        ]
    )
    return (
        f'<details style="background:#f7fbff;border:1px solid #cfe2f3;border-radius:8px;'
        f'padding:12px 14px;margin-bottom:22px;">'
        f'<summary style="cursor:pointer;font-weight:bold;color:#1f4e79;font-size:15px;">'
        f'評分標準</summary>'
        f'<div style="margin-top:12px;">'
        f'<div style="font-size:13px;color:#555;line-height:1.7;margin-bottom:12px;">'
        f'系統會分別計算買進與賣出分數，最後以「實際參考分」作為主要判斷。'
        f'若季線乖離過熱，買進分數會被歸零，只保留背景分數讓你知道原本有哪些條件偏多。</div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;">'
        f'<span style="background:#eef5fb;border:1px solid #d6eaf8;border-radius:6px;padding:5px 8px;font-size:12px;white-space:nowrap;display:inline-block;">提醒 15-29</span>'
        f'<span style="background:#fef9e7;border:1px solid #f9e79f;border-radius:6px;padding:5px 8px;font-size:12px;white-space:nowrap;display:inline-block;">弱 30-49</span>'
        f'<span style="background:#fef5e7;border:1px solid #fad7a0;border-radius:6px;padding:5px 8px;font-size:12px;white-space:nowrap;display:inline-block;">中 50-69</span>'
        f'<span style="background:#fdecea;border:1px solid #f5b7b1;border-radius:6px;padding:5px 8px;font-size:12px;white-space:nowrap;display:inline-block;">強 70+</span>'
        f'</div>'
        f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5eef7;'
        f'border-radius:6px;overflow:hidden;">'
        f'<thead><tr style="background:#eaf4fb;color:#1f4e79;">'
        f'<th style="padding:8px 9px;text-align:left;">指標</th>'
        f'<th style="padding:8px 9px;text-align:right;">分數</th>'
        f'<th style="padding:8px 9px;text-align:left;">用途</th>'
        f'</tr></thead><tbody>{weight_rows}</tbody></table>'
        f'<div style="font-size:12px;color:#777;line-height:1.6;margin-top:10px;">'
        f'BIAS60 用來判斷中期過熱或超跌，不直接加分；過熱時會鎖住買進，避免追高。</div>'
        f'<div style="font-weight:bold;color:#1f4e79;font-size:14px;margin:14px 0 8px;">交易訊號怎麼用</div>'
        f'<div style="font-size:13px;color:#555;line-height:1.7;margin-bottom:10px;">'
        f'這裡說明訊號等級的用途，不代表一定要完整照比例下單。'
        f'系統會再依市場狀態調整：大多頭少賣、空頭少買、盤整時才較適合分批操作。'
        f'同一等級訊號連續出現時，不建議每天重複交易。</div>'
        f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5eef7;'
        f'border-radius:6px;overflow:hidden;">'
        f'<thead><tr style="background:#eaf4fb;color:#1f4e79;">'
        f'<th style="padding:8px 9px;text-align:left;">等級</th>'
        f'<th style="padding:8px 9px;text-align:left;">實際用途</th>'
        f'</tr></thead><tbody>{trade_rows}</tbody></table>'
        f'</div></details>'
    )


# ── 組裝 HTML Email ──────────────────────────────────────────
def build_email_html(results: list, today: str, cfg: dict | None = None,
                     macro: dict | None = None, news_items: list | None = None) -> str:
    overview = summary_table(results)
    events_block = market_events_html(cfg or {}, today, news_items)
    rules_block = scoring_rules_html()
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
            f'{rules_block}'
            f'{events_block}'
            f'<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">今日總覽</h3>'
            f'{overview}'
            f'<h3 style="color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;">各股詳細指標</h3>'
            f'{details}'
            f'<p style="color:#aaa;font-size:11px;text-align:center;'
            f'border-top:1px solid #eee;padding-top:12px;margin-top:8px;">'
            f'⚠️ 本報告由自動化程式產生，僅供參考，不構成投資建議。</p>'
            f'</div></body></html>')


# ── 本機 HTML 預覽 ───────────────────────────────────────────
def save_email_preview(html: str) -> Path:
    preview_path = Path(__file__).parent / "email_preview.html"
    preview_path.write_text(html, encoding="utf-8")
    return preview_path


# ── 產生分享圖片與上傳雲端硬碟 ───────────────────────────────
def render_report_image(html_path: Path, today: str, cfg: dict) -> Path | None:
    drive_cfg = cfg.get("drive_report", {})
    if not drive_cfg.get("enabled", False):
        return None

    image_path = Path(__file__).parent / f"{today.replace('-', '')}.png"
    width = int(drive_cfg.get("image_width", 900))

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"⚠️  未安裝 Playwright，跳過產生圖片：{exc}")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(
                viewport={"width": width, "height": 1200},
                device_scale_factor=2,
            )
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.screenshot(path=str(image_path), full_page=True)
            browser.close()
        return image_path
    except Exception as exc:
        print(f"⚠️  產生報告圖片失敗：{exc}")
        return None


def _load_google_service_account_info() -> dict | None:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:
        print(f"⚠️  GOOGLE_SERVICE_ACCOUNT_JSON 格式錯誤：{exc}")
        return None


def _build_google_drive_credentials():
    scopes = ["https://www.googleapis.com/auth/drive"]
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if refresh_token and client_id and client_secret:
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            credentials = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=scopes,
            )
            credentials.refresh(Request())
            return credentials, "OAuth"
        except Exception as exc:
            print(f"⚠️  Google OAuth 憑證失敗，改試 service account：{exc}")

    sa_info = _load_google_service_account_info()
    if sa_info:
        try:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=scopes,
            )
            return credentials, "service account"
        except Exception as exc:
            print(f"⚠️  Google service account 憑證失敗：{exc}")

    return None, ""


def upload_report_image_to_drive(image_path: Path, today: str, cfg: dict) -> str | None:
    drive_cfg = cfg.get("drive_report", {})
    if not drive_cfg.get("enabled", False):
        return None

    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID") or drive_cfg.get("folder_id")
    if not folder_id:
        print("⚠️  未設定 Google Drive folder_id，跳過上傳圖片")
        return None

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except Exception as exc:
        print(f"⚠️  未安裝 Google Drive API 套件，跳過上傳：{exc}")
        return None

    credentials, auth_mode = _build_google_drive_credentials()
    if not credentials:
        print("⚠️  未設定 Google OAuth 或 service account 憑證，已保留本機圖片但跳過上傳")
        return None

    try:
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        print(f"使用 Google Drive {auth_mode} 憑證上傳圖片")
        file_name = f"{today.replace('-', '')}.png"
        media = MediaFileUpload(str(image_path), mimetype="image/png", resumable=False)
        query = (
            f"'{folder_id}' in parents and "
            f"name = '{file_name}' and "
            "trashed = false"
        )
        existing = service.files().list(
            q=query,
            fields="files(id,name,webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute().get("files", [])

        if existing:
            uploaded = service.files().update(
                fileId=existing[0]["id"],
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()
        else:
            uploaded = service.files().create(
                body={"name": file_name, "parents": [folder_id]},
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()

        return uploaded.get("webViewLink")
    except Exception as exc:
        print(f"⚠️  上傳 Google Drive 失敗：{exc}")
        return None


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
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
    try:
        s.login(ec["from"], gmail_pass)
        s.sendmail(ec["from"], ec["to"], msg.as_string())
        s.quit()
    except Exception:
        s.close()
        raise
    return True


# ── 主流程 ───────────────────────────────────────────────────
def main():
    cfg   = load_config()
    today = datetime.today().strftime("%Y-%m-%d")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 開始分析，共 {len(cfg['watchlist'])} 檔")

    macro = fetch_market_context()
    if macro.get("fx"):
        print(f"  總體環境：美元/台幣 {macro['fx']['value']:.3f}", end="")
    if macro.get("rates"):
        print(f"｜美10年債 {macro['rates']['value']:.2f}%", end="")
    if macro.get("fx") or macro.get("rates"):
        print()
    elif macro.get("errors"):
        print(f"  總體環境資料暫不可用：{'；'.join(macro['errors'])}")

    news_items = fetch_auto_news(cfg)
    print(f"  自動新聞掃描：取得 {len(news_items)} 則高關聯新聞")

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
            inst = fetch_institutional(ticker) if scfg.get("use_institutional", True) else None
            r    = evaluate_weighted(df, scfg, inst, macro)
            r["stock_note"] = note
            results.append((name, ticker, r))
            print(
                f"{r['emoji']} {r['summary']} | "
                f"有效買{r['effective_buy']:.0f}/賣{r['effective_sell']:.0f} "
                f"(原始買{r['buy_score']:.0f}/賣{r['sell_score']:.0f}) | "
                f"BIAS60={r['b60']['bias60']:.1f}%"
            )
        except Exception as e:
            print(f"❌ {e}")

    if not results:
        print("所有分析失敗，中止")
        return

    html = build_email_html(results, today, cfg, macro, news_items)
    preview_path = save_email_preview(html)
    print(f"\n已產生 Email 預覽：{preview_path}")

    image_path = render_report_image(preview_path, today, cfg)
    if image_path:
        print(f"已產生分享圖片：{image_path}")
        drive_link = upload_report_image_to_drive(image_path, today, cfg)
        if drive_link:
            print(f"已上傳分享圖片至 Google Drive：{drive_link}")

    print(f"\n發送 Email 至 {cfg['email']['to']} ...")
    try:
        if send_email(cfg, html, today):
            print("✅ Email 發送成功")
    except Exception as e:
        print(f"❌ Email 失敗：{e}")


if __name__ == "__main__":
    main()
