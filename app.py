from flask import Flask, render_template, jsonify, request
from datetime import datetime
from collections import defaultdict

app = Flask(__name__)

# =========================
# 假資料（可改接資料庫）
# =========================

# 交易資料：income / expense
TRANSACTIONS = [
    # (date, type, amount, category, note)
    ("2025-01-05", "income", 52000, "Salary", "January salary"),
    ("2025-01-08", "expense", 4200, "食衣住行育樂", "Food & dining"),
    ("2025-01-12", "expense", 399, "訂閱服務", "Netflix"),
    ("2025-01-15", "expense", 3500, "其他", "Gifts"),
    ("2025-01-28", "expense", 1500, "投資", "Stock fee"),

    ("2025-02-05", "income", 52000, "Salary", "February salary"),
    ("2025-02-09", "expense", 4800, "食衣住行育樂", "Food & dining"),
    ("2025-02-12", "expense", 399, "訂閱服務", "Netflix"),
    ("2025-02-19", "expense", 2100, "其他", "Shopping"),
    ("2025-02-25", "expense", 1200, "投資", "ETF fee"),

    ("2025-03-05", "income", 52000, "Salary", "March salary"),
    ("2025-03-07", "expense", 5000, "食衣住行育樂", "Food & dining"),
    ("2025-03-12", "expense", 399, "訂閱服務", "Netflix"),
    ("2025-03-20", "expense", 3500, "其他", "Clothes"),
    ("2025-03-29", "expense", 1500, "投資", "Fund fee"),
]

# 投資持倉市值（月末快照 + 當月淨投入）
INVESTMENT_POS = [
    # (date(當月月底), asset_class, market_value, net_inflow)
    ("2025-01-31", "stock", 120000, 5000),
    ("2025-01-31", "etf",   80000,  0),
    ("2025-01-31", "bond",  30000,  0),
    ("2025-01-31", "fund",  20000,  2000),

    ("2025-02-28", "stock", 126000, 3000),
    ("2025-02-28", "etf",   83000,  0),
    ("2025-02-28", "bond",  30200,  0),
    ("2025-02-28", "fund",  22300,  1000),

    ("2025-03-31", "stock", 129000, 2000),
    ("2025-03-31", "etf",   85000,  0),
    ("2025-03-31", "bond",  30500,  0),
    ("2025-03-31", "fund",  23500,  1000),
]

# 追蹤項目（本月）
TRACKERS = [
    # (month, name, spent, budget)
    ("2025-03", "食物花費", 5000, 6000),
    ("2025-03", "購物金",  3500, 4000),
]


# =========================
# 工具：彙整與計算
# =========================
def ym(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%Y-%m")

def end_of_month(date_str):
    return date_str  # 假設已是月底日期

def aggregate_monthly_cash():
    monthly = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    for d, t, amt, cat, note in TRANSACTIONS:
        m = ym(d)
        monthly[m][t] += amt
    # 加上結餘
    result = []
    for m in sorted(monthly.keys()):
        inc = monthly[m]["income"]
        exp = monthly[m]["expense"]
        result.append({
            "month": m,
            "income": round(inc, 2),
            "expense": round(exp, 2),
            "balance": round(inc - exp, 2)
        })
    return result

def aggregate_expense_by_category(target_month=None):
    cate = defaultdict(float)
    for d, t, amt, cat, note in TRANSACTIONS:
        if t != "expense":
            continue
        if target_month and ym(d) != target_month:
            continue
        cate[cat] += amt
    return [{"category": k, "amount": round(v, 2)} for k, v in cate.items()]

def portfolio_by_month():
    # 回傳每月總市值，以及分類分布
    by_m = defaultdict(lambda: defaultdict(float))
    for d, asset_class, mv, inflow in INVESTMENT_POS:
        m = d[:7]
        by_m[m][asset_class] += mv
        by_m[m]["_total"] += mv
        by_m[m]["_net_inflow"] += inflow

    # 期初/期末供 ROI 用
    months = sorted(by_m.keys())
    roi = {}
    for i, m in enumerate(months):
        mv_end = by_m[m]["_total"]
        inflow = by_m[m]["_net_inflow"]
        if i == 0:
            roi[m] = None  # 第一個月沒有期初
        else:
            prev_m = months[i-1]
            mv_start = by_m[prev_m]["_total"]
            # 簡化 ROI： (期末 - 期初 - 淨投入) / 期初
            roi[m] = round((mv_end - mv_start - inflow) / mv_start, 4) if mv_start > 0 else None

    return by_m, months, roi

def total_assets_trend():
    # 簡化：累積結餘（現金） + 投資市值
    monthly_cash = {x["month"]: x for x in aggregate_monthly_cash()}
    by_m, months, roi = portfolio_by_month()

    # 現金：逐月累積（收入-支出）
    cash_acc = 0.0
    trend = []
    for m in months:
        if m in monthly_cash:
            cash_acc += monthly_cash[m]["balance"]
        mv = by_m[m]["_total"]
        trend.append({"month": m, "cash": round(cash_acc, 2), "investment": round(mv, 2), "total": round(cash_acc + mv, 2)})
    return trend, roi

def current_month():
    # 以投資資料最新月為準
    last = max([d[:7] for d, *_ in INVESTMENT_POS])
    return last

# =========================
# Routes（頁面 & APIs）
# =========================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/line_income_expense_balance")
def api_line():
    return jsonify(aggregate_monthly_cash())

@app.route("/api/pie_expense_by_category")
def api_pie():
    m = request.args.get("month")  # 可指定月份
    data = aggregate_expense_by_category(m)
    return jsonify({
        "month": m or "ALL",
        "data": data
    })

@app.route("/api/bar_portfolio_allocation")
def api_bar():
    by_m, months, _ = portfolio_by_month()
    if not months:
        return jsonify({"month": None, "data": []})
    target = request.args.get("month") or months[-1]
    comp = by_m[target]
    data = [{"asset_class": k, "market_value": v} for k, v in comp.items() if not k.startswith("_")]
    return jsonify({"month": target, "data": data})

@app.route("/api/kpis")
def api_kpis():
    monthly = aggregate_monthly_cash()
    trend, roi_map = total_assets_trend()
    last_m = trend[-1]["month"] if trend else None
    # 當月 KPI
    m = request.args.get("month") or (last_m if last_m else current_month())
    # 當月收入/支出/現金流/儲蓄率
    inc = exp = bal = 0.0
    for x in monthly:
        if x["month"] == m:
            inc, exp, bal = x["income"], x["expense"], x["balance"]
            break
    savings_rate = round((bal / inc), 4) if inc > 0 else None
    monthly_roi = roi_map.get(m)  # 可能為 None（首月）
    assets_total = next((t["total"] for t in trend if t["month"] == m), None)
    return jsonify({
        "month": m,
        "income": inc,
        "expense": exp,
        "cash_flow": bal,
        "savings_rate": savings_rate,
        "monthly_roi": monthly_roi,
        "assets_total": assets_total
    })

@app.route("/api/trackers")
def api_trackers():
    # 可用 query 參數指定月份
    m = request.args.get("month") or current_month()
    rows = [t for t in TRACKERS if t[0] == m]
    data = [{"month": r[0], "name": r[1], "spent": r[2], "budget": r[3],
             "progress": round(r[2]/r[3], 4) if r[3] else None} for r in rows]
    return jsonify({"month": m, "data": data})

if __name__ == "__main__":
    app.run(debug=True)
