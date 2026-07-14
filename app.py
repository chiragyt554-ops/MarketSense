"""
MarketSense Web – Flask backend
Run locally : python app.py
Deploy      : Railway (gunicorn)
"""

import json
import os
from datetime import datetime
from flask import Flask, render_template, jsonify, request

# ── Auto-install yfinance if missing ──────────────────────────────────────────
try:
    import yfinance as yf
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance", "--quiet"])
    import yfinance as yf

app = Flask(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Formatters
# ─────────────────────────────────────────────────────────────────────────────
def fmt_large(n):
    if n is None: return "N/A"
    if abs(n) >= 1e12: return f"${n/1e12:.2f}T"
    if abs(n) >= 1e9:  return f"${n/1e9:.2f}B"
    if abs(n) >= 1e6:  return f"${n/1e6:.2f}M"
    return f"${n:,.0f}"

def fmt_pct_abs(n):
    return "N/A" if n is None else f"{n*100:.1f}%"

def fmt_pct_signed(n):
    if n is None: return "N/A"
    return f"{'+' if n >= 0 else ''}{n*100:.1f}%"

def fmt_x(n, d=1):
    if n is None: return "N/A"
    if n <= 0:    return "N/M"
    return f"{n:.{d}f}x"


# ─────────────────────────────────────────────────────────────────────────────
# Scoring helpers
# ─────────────────────────────────────────────────────────────────────────────
def bracket(val, breakpoints):
    for threshold, score in breakpoints:
        if val <= threshold:
            return score
    return breakpoints[-1][1]


# Valuation
def score_pe_ttm(pe):
    if pe is None or pe <= 0:
        return 35, "N/M", "Negative/no earnings", "LOCK"
    s = bracket(pe, [(12,92),(17,82),(22,72),(30,60),(40,48),(60,36),(9999,24)])
    note = ("Deep value" if pe < 12 else "Below market avg" if pe < 17 else
            "Fair value" if pe < 22 else "Premium" if pe < 30 else
            "Rich" if pe < 40 else "Very rich" if pe < 60 else "Extreme premium")
    return s, fmt_x(pe), note, "LOCK"

def score_fwd_pe(fpe):
    if fpe is None or fpe <= 0:
        return 40, "N/A", "No forward estimate", "FLEX"
    s = bracket(fpe, [(10,92),(15,82),(20,72),(28,60),(38,46),(55,32),(9999,22)])
    note = ("Very attractive" if fpe < 10 else "Attractive" if fpe < 15 else
            "Fair" if fpe < 20 else "Premium" if fpe < 28 else
            "Rich" if fpe < 38 else "Very rich" if fpe < 55 else "Extreme")
    return s, fmt_x(fpe), note, "FLEX"

def score_peg(peg):
    if peg is None or peg <= 0:
        return 45, "N/A", "PEG not meaningful", "FLEX"
    s = bracket(peg, [(0.5,95),(1.0,85),(1.5,75),(2.0,60),(3.0,44),(9999,28)])
    note = ("Excellent – growth discounted" if peg < 0.5 else
            "Very attractive" if peg < 1 else "Attractive" if peg < 1.5 else
            "Fair" if peg < 2 else "Stretched" if peg < 3 else "Overvalued vs growth")
    return s, fmt_x(peg, 2), note, "FLEX"

def score_ps(ps):
    if ps is None or ps <= 0:
        return 45, "N/A", "No P/S data", "FLEX"
    s = bracket(ps, [(1,90),(2,82),(4,72),(8,58),(15,42),(9999,28)])
    note = ("Deep value" if ps < 1 else "Cheap" if ps < 2 else
            "Moderate" if ps < 4 else "Premium" if ps < 8 else
            "Rich" if ps < 15 else "Extreme")
    return s, fmt_x(ps), note, "LOCK"

def score_ev_ebitda(ev_ebitda):
    if ev_ebitda is None or ev_ebitda <= 0:
        return 40, "N/A", "Not meaningful", "FLEX"
    s = bracket(ev_ebitda, [(8,90),(12,82),(18,72),(25,60),(35,44),(50,30),(9999,20)])
    note = ("Very cheap" if ev_ebitda < 8 else "Cheap" if ev_ebitda < 12 else
            "Fair" if ev_ebitda < 18 else "Premium" if ev_ebitda < 25 else
            "Rich" if ev_ebitda < 35 else "Very rich" if ev_ebitda < 50 else "Extreme")
    return s, fmt_x(ev_ebitda), note, "LOCK"

def score_ev_fcf(ev, fcf):
    if ev is None or fcf is None or fcf <= 0:
        return 40, "N/A", "No FCF or EV data", "FLEX"
    ratio = ev / fcf
    s = bracket(ratio, [(15,90),(25,82),(35,70),(50,55),(75,38),(100,25),(9999,15)])
    note = ("Excellent FCF yield" if ratio < 15 else "Good" if ratio < 25 else
            "Fair" if ratio < 35 else "Stretched" if ratio < 50 else
            "Rich" if ratio < 75 else "Very rich" if ratio < 100 else "Extreme premium")
    return s, fmt_x(ratio), note, "FLEX"


# Financial Health
def score_de_ratio(de):
    if de is None:
        return 55, "N/A", "No debt data", "FLEX"
    de_actual = de / 100 if abs(de) > 5 else de
    if de_actual < 0:
        return 98, "Net Cash", "Net cash – fortress balance sheet", "LOCK"
    s = bracket(de_actual, [(0.1,95),(0.3,88),(0.5,82),(0.8,74),(1.5,62),(2.5,46),(4.0,32),(9999,18)])
    note = ("Virtually debt-free" if de_actual < 0.1 else "Very low leverage" if de_actual < 0.3 else
            "Conservative" if de_actual < 0.5 else "Modest" if de_actual < 0.8 else
            "Moderate" if de_actual < 1.5 else "Elevated" if de_actual < 2.5 else
            "High leverage" if de_actual < 4 else "Extreme leverage")
    return s, f"{de_actual:.2f}x", note, "LOCK"

def score_current_ratio(cr):
    if cr is None:
        return 50, "N/A", "No liquidity data", "FLEX"
    s = bracket(cr, [(0.8,28),(1.0,50),(1.2,65),(1.5,75),(2.0,84),(3.0,90),(9999,88)])
    note = ("Liquidity concern" if cr < 0.8 else "Watch carefully" if cr < 1.0 else
            "Borderline" if cr < 1.2 else "Adequate" if cr < 1.5 else
            "Comfortable" if cr < 2.0 else "Strong" if cr < 3.0 else "Very strong")
    return s, f"{cr:.2f}x", note, "FLEX"

def score_net_margin(nm):
    if nm is None:
        return 35, "N/A", "No margin data", "FLEX"
    s = bracket(nm, [(-0.01,15),(0.03,38),(0.07,52),(0.12,65),(0.18,75),(0.25,84),(0.35,90),(9,96)])
    note = ("Losses" if nm < 0 else "Razor thin" if nm < 0.03 else
            "Thin" if nm < 0.07 else "Moderate" if nm < 0.12 else
            "Healthy" if nm < 0.18 else "Strong" if nm < 0.25 else
            "Very strong" if nm < 0.35 else "Exceptional")
    return s, fmt_pct_abs(nm), note, "LOCK"

def score_roe(roe):
    if roe is None:
        return 40, "N/A", "No ROE data", "FLEX"
    s = bracket(roe, [(-0.01,15),(0.05,38),(0.10,55),(0.15,67),(0.20,76),(0.30,86),(0.50,92),(9,96)])
    note = ("Negative ROE" if roe < 0 else "Very low" if roe < 0.05 else
            "Low" if roe < 0.10 else "Below average" if roe < 0.15 else
            "Average" if roe < 0.20 else "Good" if roe < 0.30 else
            "Excellent" if roe < 0.50 else "Exceptional")
    return s, fmt_pct_abs(roe), note, "LOCK"

def score_gross_margin(gm):
    if gm is None:
        return 45, "N/A", "No data", "FLEX"
    s = bracket(gm, [(0.15,30),(0.25,48),(0.35,60),(0.45,70),(0.55,78),(0.65,86),(0.75,92),(9,96)])
    note = ("Commodity-like" if gm < 0.15 else "Low" if gm < 0.25 else
            "Below average" if gm < 0.35 else "Average" if gm < 0.45 else
            "Above average" if gm < 0.55 else "Good" if gm < 0.65 else
            "High" if gm < 0.75 else "Exceptional")
    return s, fmt_pct_abs(gm), note, "LOCK"

def score_op_margin(om):
    if om is None:
        return 40, "N/A", "No data", "FLEX"
    s = bracket(om, [(-0.01,12),(0.03,35),(0.07,52),(0.12,65),(0.18,75),(0.25,84),(0.35,91),(9,96)])
    note = ("Operating loss" if om < 0 else "Minimal" if om < 0.03 else
            "Low" if om < 0.07 else "Moderate" if om < 0.12 else
            "Healthy" if om < 0.18 else "Strong" if om < 0.25 else
            "Very strong" if om < 0.35 else "Exceptional")
    return s, fmt_pct_abs(om), note, "FLEX"


# Growth
def score_rev_growth(rg):
    if rg is None:
        return 40, "N/A", "No recent data", "FLEX"
    s = bracket(rg, [(-0.05,12),(-0.01,28),(0.02,42),(0.05,52),(0.10,62),(0.15,70),
                     (0.20,77),(0.30,84),(0.50,90),(9,96)])
    note = ("Declining rapidly" if rg < -0.05 else "Declining" if rg < -0.01 else
            "Flat" if rg < 0.02 else "Slow" if rg < 0.05 else
            "Modest" if rg < 0.10 else "Moderate" if rg < 0.15 else
            "Good" if rg < 0.20 else "Strong" if rg < 0.30 else
            "Very strong" if rg < 0.50 else "Hypergrowth")
    return s, fmt_pct_signed(rg), note, "LOCK"

def score_earnings_growth(eg):
    if eg is None:
        return 40, "N/A", "No data", "FLEX"
    s = bracket(eg, [(-0.10,10),(-0.01,25),(0.02,40),(0.05,52),(0.10,62),(0.15,70),
                     (0.25,78),(0.40,86),(0.75,92),(9,96)])
    note = ("Earnings falling fast" if eg < -0.10 else "Declining" if eg < -0.01 else
            "Flat" if eg < 0.02 else "Slow" if eg < 0.05 else
            "Modest" if eg < 0.10 else "Moderate" if eg < 0.15 else
            "Good" if eg < 0.25 else "Strong" if eg < 0.40 else
            "Very strong" if eg < 0.75 else "Exceptional")
    return s, fmt_pct_signed(eg), note, "LOCK"

def score_fcf_margin(fcf, revenue):
    if fcf is None or revenue is None or revenue == 0:
        return 45, "N/A", "No FCF data", "FLEX"
    margin = fcf / revenue
    s = bracket(margin, [(-0.01,15),(0.02,38),(0.05,52),(0.10,65),(0.15,75),(0.20,83),(0.30,90),(9,96)])
    note = ("FCF negative" if margin < 0 else "Very thin" if margin < 0.02 else
            "Thin" if margin < 0.05 else "Adequate" if margin < 0.10 else
            "Good" if margin < 0.15 else "Strong" if margin < 0.20 else
            "Very strong" if margin < 0.30 else "Exceptional")
    return s, fmt_pct_abs(margin), note, "FLEX"

def score_analyst_upside(current, target):
    if current is None or target is None or current == 0:
        return 50, "N/A", "No analyst target", "FLEX"
    upside = (target - current) / current
    s = bracket(upside, [(-0.20,15),(-0.05,30),(0.0,45),(0.05,55),(0.10,64),
                         (0.20,73),(0.35,82),(0.50,90),(9,96)])
    note = ("Strong downside implied" if upside < -0.20 else "Analysts cautious" if upside < -0.05 else
            "At/near consensus target" if upside < 0.05 else "Modest upside" if upside < 0.10 else
            "Moderate upside" if upside < 0.20 else "Good upside" if upside < 0.35 else
            "Strong upside" if upside < 0.50 else "Very strong upside")
    return s, f"${target:.2f} ({upside:+.0%})", note, "FLEX"

def score_rev_quality(gm, om):
    if gm is None or om is None or gm <= 0:
        return 50, "N/A", "Insufficient data", "FLEX"
    conv = om / gm
    s = bracket(conv, [(0.0,20),(0.2,40),(0.35,55),(0.50,65),(0.65,75),(0.75,83),(0.85,90),(9,96)])
    note = ("High overhead drag" if conv < 0.20 else "Significant SG&A burden" if conv < 0.35 else
            "Moderate overhead" if conv < 0.50 else "Reasonable" if conv < 0.65 else
            "Efficient" if conv < 0.75 else "Very efficient" if conv < 0.85 else "Lean")
    return s, f"{conv*100:.0f}% conv.", note, "FLEX"


# ─────────────────────────────────────────────────────────────────────────────
# Core analysis
# ─────────────────────────────────────────────────────────────────────────────
def analyze(ticker_symbol):
    t = yf.Ticker(ticker_symbol)
    info = t.info

    name  = info.get("shortName") or info.get("longName")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if not name or not price:
        raise ValueError(
            f"No data found for '{ticker_symbol}'. "
            "Check the ticker symbol (e.g. NVDA, AAPL) and try again."
        )

    ev  = info.get("enterpriseValue")
    fcf = info.get("freeCashflow")
    rev = info.get("totalRevenue")
    gm  = info.get("grossMargins")
    om  = info.get("operatingMargins")

    def make_metrics(rows):
        return [
            {"label": label, "value": v, "score": s, "note": note, "tag": tag}
            for label, (s, v, note, tag) in rows
        ]

    val_metrics = make_metrics([
        ("P/E TTM",       score_pe_ttm(info.get("trailingPE"))),
        ("Forward P/E",   score_fwd_pe(info.get("forwardPE"))),
        ("PEG Ratio",     score_peg(info.get("pegRatio"))),
        ("Price / Sales", score_ps(info.get("priceToSalesTrailing12Months"))),
        ("EV / EBITDA",   score_ev_ebitda(info.get("enterpriseToEbitda"))),
        ("EV / FCF",      score_ev_fcf(ev, fcf)),
    ])
    health_metrics = make_metrics([
        ("Debt / Equity",    score_de_ratio(info.get("debtToEquity"))),
        ("Current Ratio",    score_current_ratio(info.get("currentRatio"))),
        ("Net Margin",       score_net_margin(info.get("profitMargins"))),
        ("Return on Equity", score_roe(info.get("returnOnEquity"))),
        ("Gross Margin",     score_gross_margin(gm)),
        ("Operating Margin", score_op_margin(om)),
    ])
    growth_metrics = make_metrics([
        ("Revenue Growth YoY",  score_rev_growth(info.get("revenueGrowth"))),
        ("Earnings Growth YoY", score_earnings_growth(info.get("earningsGrowth"))),
        ("FCF Margin",          score_fcf_margin(fcf, rev)),
        ("Gross Margin",        score_gross_margin(gm)),
        ("Revenue Quality",     score_rev_quality(gm, om)),
        ("Analyst Target",      score_analyst_upside(price, info.get("targetMeanPrice"))),
    ])

    val_score    = round(sum(m["score"] for m in val_metrics)    / len(val_metrics))
    health_score = round(sum(m["score"] for m in health_metrics) / len(health_metrics))
    growth_score = round(sum(m["score"] for m in growth_metrics) / len(growth_metrics))
    overall      = round(val_score * 0.35 + health_score * 0.35 + growth_score * 0.30)

    # Dynamic catalysts & risks
    rg      = info.get("revenueGrowth") or 0
    eg      = info.get("earningsGrowth") or 0
    _gm     = gm or 0
    _om     = om or 0
    _pe     = info.get("trailingPE") or 999
    _fpe    = info.get("forwardPE") or 999
    _de     = info.get("debtToEquity") or 0
    _de_act = _de / 100 if abs(_de) > 5 else _de
    _cr     = info.get("currentRatio") or 0
    _nm     = info.get("profitMargins") or 0
    _roe    = info.get("returnOnEquity") or 0
    _peg    = info.get("pegRatio") or 999
    _fcf    = fcf or 0
    _rev    = rev or 0
    target  = info.get("targetMeanPrice") or price
    upside  = (target - price) / price if price else 0
    rec     = (info.get("recommendationKey") or "").lower()
    n_ana   = info.get("numberOfAnalystOpinions") or 0

    catalysts, risks = [], []

    if rg > 0.20:
        catalysts.append(f"Revenue growing {rg*100:.0f}% YoY – well above market average; top-line momentum is the primary bull thesis")
    elif rg > 0.10:
        catalysts.append(f"Solid revenue growth of {rg*100:.0f}% YoY – above-average for the sector")
    if eg > 0.25:
        catalysts.append(f"Earnings expanding {eg*100:.0f}% YoY – operating leverage translating growth to the bottom line")
    if _gm > 0.55:
        catalysts.append(f"Gross margin of {_gm*100:.0f}% signals durable pricing power and a strong competitive moat")
    if _om > 0.20:
        catalysts.append(f"Operating margin of {_om*100:.0f}% reflects an efficient, scalable business model")
    if _de_act < 0:
        catalysts.append("Net cash position – financial optionality for buybacks, dividends, or M&A without dilution")
    elif _de_act < 0.2:
        catalysts.append("Clean balance sheet with minimal leverage – resilient through economic downturns")
    if _fcf > 0 and _rev > 0 and (_fcf / _rev) > 0.15:
        catalysts.append(f"FCF margin of {_fcf/_rev*100:.0f}% validates earnings quality – cash generation is real")
    if upside > 0.20 and n_ana >= 5:
        catalysts.append(f"Analyst consensus sees {upside*100:.0f}% upside to mean target (${target:.2f}) across {n_ana} analysts")
    if 0 < _peg < 1.0:
        catalysts.append(f"PEG of {_peg:.2f} – growth is not fully reflected in the current price")
    if _roe > 0.25:
        catalysts.append(f"ROE of {_roe*100:.0f}% – management generating strong returns on equity")

    if rg < -0.01:
        risks.append(f"Revenue declining {abs(rg)*100:.0f}% YoY – structural headwind or cyclical pressure")
    elif rg < 0.05 and _pe > 25:
        risks.append("Low revenue growth at a premium valuation – market may be pricing in a recovery that doesn't arrive")
    if _pe > 40 and _fpe > 30:
        risks.append(f"Rich valuation (P/E {_pe:.0f}x TTM / {_fpe:.0f}x fwd) – any earnings miss could de-rate sharply")
    elif _pe > 30:
        risks.append(f"Elevated trailing P/E of {_pe:.0f}x leaves limited margin of safety")
    if _de_act > 1.5:
        risks.append(f"High leverage at {_de_act:.1f}x D/E – interest expense is a drag in a higher-rate environment")
    if 0 < _cr < 1.0:
        risks.append(f"Current ratio of {_cr:.2f}x – short-term obligations exceed current assets")
    if _nm < 0:
        risks.append("Operating at a net loss – path to profitability is the critical question")
    elif _nm < 0.05:
        risks.append(f"Net margin of {_nm*100:.1f}% is thin – limited buffer against cost or revenue shocks")
    if upside < -0.05 and n_ana >= 5:
        risks.append(f"Analyst consensus implies downside to current price (target: ${target:.2f})")
    if eg < -0.05:
        risks.append(f"Earnings contracting {abs(eg)*100:.0f}% YoY – watch for further guidance cuts")
    if _gm < 0.30:
        risks.append(f"Gross margin of {_gm*100:.0f}% signals commodity-like economics or pricing pressure")
    if rec in ("sell", "underperform", "strong_sell"):
        risks.append("Sell/underperform analyst consensus – understand the bear thesis before sizing a position")

    catalysts = (catalysts + ["Insufficient data – review latest filings and earnings call"])[:5]
    risks     = (risks     + ["Insufficient data – review latest filings and earnings call"])[:5]

    return {
        "ticker":         ticker_symbol.upper(),
        "name":           name,
        "sector":         info.get("sector", "N/A"),
        "industry":       info.get("industry", "N/A"),
        "exchange":       info.get("exchange", ""),
        "price":          price,
        "change_pct":     info.get("regularMarketChangePercent"),
        "market_cap":     fmt_large(info.get("marketCap")),
        "revenue":        fmt_large(info.get("totalRevenue")),
        "eps_ttm":        f"${info.get('trailingEps', 0):.2f}" if info.get("trailingEps") else "N/A",
        "high_52w":       info.get("fiftyTwoWeekHigh"),
        "low_52w":        info.get("fiftyTwoWeekLow"),
        "overall":        overall,
        "val_score":      val_score,
        "health_score":   health_score,
        "growth_score":   growth_score,
        "val_metrics":    val_metrics,
        "health_metrics": health_metrics,
        "growth_metrics": growth_metrics,
        "catalysts":      catalysts,
        "risks":          risks,
        "date":           datetime.now().strftime("%d %b %Y"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze")
def api_analyze():
    ticker = request.args.get("ticker", "").upper().strip()
    if not ticker:
        return jsonify({"error": "No ticker provided"}), 400
    try:
        data = analyze(ticker)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 422


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
