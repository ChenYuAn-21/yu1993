import os
import sqlite3
from flask import Flask, render_template, jsonify, request, g
from datetime import datetime
from collections import defaultdict

DB_PATH = os.path.join(os.path.dirname(__file__), "finance.db")

app = Flask(__name__)

# -----------------------
# SQLite 連線 & 初始化
# -----------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,                 -- YYYY-MM-DD
        type TEXT NOT NULL CHECK(type IN ('income','expense')),
        amount REAL NOT NULL,
        category TEXT NOT NULL,             -- 食物/交通/購物/娛樂/日用品/投資/其他/訂閱服務
        note TEXT
    );
    CREATE TABLE IF NOT EXISTS investment_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,                 -- 月底快照 YYYY-MM-DD
        asset_class TEXT NOT NULL CHECK(asset_class IN ('stock','etf','bond','fund')),
        market_value REAL NOT NULL,
        net_inflow REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS trackers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month TEXT NOT NULL,                -- YYYY-MM
        name TEXT NOT NULL,
        spent REAL NOT NULL DEFAULT 0,
        budget REAL NOT NULL DEFAULT 0
    );
    """)
    db.commit()

    # 若為全新資料庫，塞入示例資料
    c = db.execute("SELECT COUNT(1) AS c FROM transactions").fetchone()["c"]
    if c == 0:
        db.executemany(
            "INSERT INTO transactions (date,type,amount,category,note) VALUES (?,?,?,?,?)",
            [
                ("2025-01-05","income",52000,"其他","January salary"),
                ("2025-01-08","expense",4200,"食物","Food & dining"),
                ("2025-01-12","expense",399,"訂閱服務","Netflix"),
                ("2025-01-15","expense",3500,"其他","Gifts"),
                ("2025-01-28","expense",1500,"投資","Stock fee"),

                ("2025-02-05","income",52000,"其他","February salary"),
                ("2025-02-09","expense",4800,"食物","Food & dining"),
                ("2025-02-12","expense",399,"訂閱服務","Netflix"),
                ("2025-02-19","expense",2100,"購物","Shopping"),
                ("2025-02-25","expense",1200,"投資","ETF fee"),

                ("2025-03-05","income",52000,"其他","March salary"),
                ("2025-03-07","expense",5000,"食物","Food & dining"),
                ("2025-03-12","expense",399,"訂閱服務","Netflix"),
                ("2025-03-20","expense",3500,"購物","Clothes"),
                ("2025-03-29","expense",1500,"投資","Fund fee"),
            ]
        )
        db.executemany(
            "INSERT INTO investment_positions (date,asset_class,market_value,net_inflow) VALUES (?,?,?,?)",
            [
                ("2025-01-31","stock",120000,5000),
                ("2025-01-31","etf",  80000,0),
                ("2025-01-31","bond", 30000,0),
                ("2025-01-31","fund", 20000,2000),

                ("2025-02-28","stock",126000,3000),
                ("2025-02-28","etf",  83000,0),
                ("2025-02-28","bond", 30200,0),
                ("2025-02-28","fund", 22300,1000),

                ("2025-03-31","stock",129000,2000),
                ("2025-03-31","etf",  85000,0),
                ("2025-03-31","bond", 30500,0),
                ("2025-03-31","fund", 23500,1000),
            ]
        )
        db.executemany(
            "INSERT INTO trackers (month,name,spent,budget) VALUES (?,?,?,?)",
            [
                ("2025-03","食物花費",5000,6000),
                ("2025-03","購物金",3500,4000),
            ]
        )
        db.commit()

with app.app_context():
    init_db()

# -----------------------
# 聚合工具
# -----------------------
def aggregate_monthly_cash():
    db = get_db()
    rows = db.execute("""
        SELECT strftime('%Y-%m', date) AS m,
               SUM(CASE WHEN type='income'  THEN amount ELSE 0 END) AS income,
               SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS expense
        FROM transactions
        GROUP BY m
        ORDER BY m
    """).fetchall()
    result = []
    for r in rows:
        inc = r["income"] or 0.0
        exp = r["expense"] or 0.0
        result.append({
            "month": r["m"], "income": round(inc,2), "expense": round(exp,2),
            "balance": round(inc-exp,2)
        })
    return result

def aggregate_expense_by_category(target_month=None):
    db = get_db()
    params = []
    sql = """
        SELECT category, SUM(amount) AS amt
        FROM transactions
        WHERE type='expense'
    """
    if target_month:
        sql += " AND strftime('%Y-%m', date)=? "
        params.append(target_month)
    sql += " GROUP BY category "
    rows = db.execute(sql, params).fetchall()
    return [{"category": r["category"], "amount": round(r["amt"] or 0.0, 2)} for r in rows]

def portfolio_by_month():
    db = get_db()
    rows = db.execute("""
        SELECT strftime('%Y-%m', date) AS m, asset_class, SUM(market_value) AS mv, SUM(net_inflow) AS inflow
        FROM investment_positions
        GROUP BY m, asset_class
        ORDER BY m
    """).fetchall()
    by_m = defaultdict(lambda: defaultdict(float))
    for r in rows:
        by_m[r["m"]][r["asset_class"]] += r["mv"] or 0.0
        by_m[r["m"]]["_total"] += r["mv"] or 0.0
        by_m[r["m"]]["_net_inflow"] += r["inflow"] or 0.0
    months = sorted(by_m.keys())
    roi = {}
    for i, m in enumerate(months):
        mv_end = by_m[m]["_total"]; inflow = by_m[m]["_net_inflow"]
        if i == 0: roi[m] = None
        else:
            prev_m = months[i-1]; mv_start = by_m[prev_m]["_total"]
            roi[m] = round((mv_end - mv_start - inflow) / mv_start, 4) if mv_start>0 else None
    return by_m, months, roi

def total_assets_trend():
    monthly_cash = {x["month"]: x for x in aggregate_monthly_cash()}
    by_m, months, roi = portfolio_by_month()
    cash_acc = 0.0; trend = []
    for m in months:
        if m in monthly_cash: cash_acc += monthly_cash[m]["balance"]
        mv = by_m[m]["_total"]
        trend.append({"month": m, "cash": round(cash_acc,2), "investment": round(mv,2), "total": round(cash_acc+mv,2)})
    return trend, roi

def current_month():
    db = get_db()
    m1 = db.execute("SELECT MAX(strftime('%Y-%m', date)) AS m FROM investment_positions").fetchone()["m"]
    m2 = db.execute("SELECT MAX(strftime('%Y-%m', date)) AS m FROM transactions").fetchone()["m"]
    candidates = [m for m in [m1, m2] if m]
    return sorted(candidates)[-1] if candidates else datetime.today().strftime("%Y-%m")

# -----------------------
# Routes（頁面 & API）
# -----------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/months")
def api_months():
    db = get_db()
    rows = db.execute("""
        SELECT strftime('%Y-%m', date) AS m FROM transactions
        UNION
        SELECT strftime('%Y-%m', date) AS m FROM investment_positions
        ORDER BY m
    """).fetchall()
    months = [r["m"] for r in rows]
    return jsonify(months)

@app.route("/api/line_income_expense_balance")
def api_line():
    return jsonify(aggregate_monthly_cash())

@app.route("/api/pie_expense_by_category")
def api_pie():
    m = request.args.get("month")
    data = aggregate_expense_by_category(m)
    return jsonify({"month": m or "ALL", "data": data})

# --- KPI：ROI 僅計投資部位 ---
@app.route("/api/kpis")
def api_kpis():
    monthly = aggregate_monthly_cash()
    trend, roi_map = total_assets_trend()
    last_m = trend[-1]["month"] if trend else None
    m = request.args.get("month") or (last_m if last_m else current_month())

    inc = exp = bal = 0.0
    for x in monthly:
        if x["month"] == m:
            inc, exp, bal = x["income"], x["expense"], x["balance"]
            break
    savings_rate = round((bal / inc), 4) if inc > 0 else None
    monthly_roi = roi_map.get(m)  # 只來自 investment_positions
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
    db = get_db()
    m = request.args.get("month") or current_month()
    rows = db.execute("SELECT month,name,spent,budget FROM trackers WHERE month=?", (m,)).fetchall()
    data = [{"month": r["month"], "name": r["name"], "spent": r["spent"], "budget": r["budget"],
             "progress": round(r["spent"]/r["budget"], 4) if r["budget"] else None} for r in rows]
    return jsonify({"month": m, "data": data})

# ------- 交易 CRUD -------
# 新增
@app.route("/api/transactions", methods=["POST"])
def api_add_transaction():
    payload = request.get_json(force=True)
    ttype = payload.get("type")
    date = payload.get("date")
    category = payload.get("category")
    note = payload.get("note", "")
    amount = payload.get("amount")

    if ttype not in ("income","expense"):
        return jsonify({"ok": False, "error": "type must be income or expense"}), 400
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except Exception:
        return jsonify({"ok": False, "error": "date must be YYYY-MM-DD"}), 400
    if category not in ("食物","交通","購物","娛樂","日用品","投資","其他","訂閱服務"):
        return jsonify({"ok": False, "error": "category invalid"}), 400
    try:
        amount = float(amount)
        if amount <= 0: raise ValueError()
    except Exception:
        return jsonify({"ok": False, "error": "amount must be positive"}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO transactions (date,type,amount,category,note) VALUES (?,?,?,?,?)",
        (date, ttype, amount, category, note)
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})

# 讀清單（可依月份）
@app.route("/api/transactions", methods=["GET"])
def api_list_transactions():
    m = request.args.get("month")
    db = get_db()
    if m:
        rows = db.execute("""
            SELECT id, date, type, amount, category, note
            FROM transactions
            WHERE strftime('%Y-%m', date)=?
            ORDER BY date, id
        """,(m,)).fetchall()
    else:
        rows = db.execute("""
            SELECT id, date, type, amount, category, note
            FROM transactions
            ORDER BY date, id
        """).fetchall()
    data = [dict(r) for r in rows]
    return jsonify({"data": data})

# 更新
@app.route("/api/transactions/<int:tid>", methods=["PUT"])
def api_update_transaction(tid):
    payload = request.get_json(force=True)
    fields = []
    params = []
    for key in ("date","type","amount","category","note"):
        if key in payload:
            if key == "type" and payload[key] not in ("income","expense"):
                return jsonify({"ok": False, "error": "type invalid"}), 400
            if key == "amount":
                try:
                    v = float(payload[key])
                    if v <= 0: raise ValueError()
                    payload[key] = v
                except Exception:
                    return jsonify({"ok": False, "error": "amount must be positive"}), 400
            if key == "date":
                try: datetime.strptime(payload[key], "%Y-%m-%d")
                except Exception:
                    return jsonify({"ok": False, "error": "date must be YYYY-MM-DD"}), 400
            if key == "category" and payload[key] not in ("食物","交通","購物","娛樂","日用品","投資","其他","訂閱服務"):
                return jsonify({"ok": False, "error": "category invalid"}), 400
            fields.append(f"{key}=?"); params.append(payload[key])
    if not fields:
        return jsonify({"ok": False, "error": "no fields"}), 400
    params.append(tid)
    db = get_db()
    db.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id=?", params)
    db.commit()
    return jsonify({"ok": True})

# 刪除
@app.route("/api/transactions/<int:tid>", methods=["DELETE"])
def api_delete_transaction(tid):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id=?", (tid,))
    db.commit()
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(debug=True)
