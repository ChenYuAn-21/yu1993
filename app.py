import os
import sqlite3
from datetime import datetime
from collections import defaultdict

from flask import Flask, render_template, jsonify, request, g

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
    db.executescript(
        """
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,                 -- YYYY-MM-DD
        type TEXT NOT NULL CHECK(type IN ('income','expense')),
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        note TEXT
    );

    CREATE TABLE IF NOT EXISTS investment_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        asset_class TEXT NOT NULL CHECK(asset_class IN ('stock','etf','bond','fund')),
        market_value REAL NOT NULL,
        net_inflow REAL NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS trackers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month TEXT NOT NULL,
        name TEXT NOT NULL,
        spent REAL NOT NULL DEFAULT 0,
        budget REAL NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        target REAL NOT NULL,
        saved REAL NOT NULL
    );
    """
    )
    # 確保 transactions 有 goal_id 欄位（舊 DB 也能安全遷移）
    info = db.execute("PRAGMA table_info(transactions)").fetchall()
    cols = [r["name"] for r in info]
    if "goal_id" not in cols:
        db.execute("ALTER TABLE transactions ADD COLUMN goal_id INTEGER")
    db.commit()

    # 只有第一次時塞入示例資料
    cur = db.execute("SELECT COUNT(1) AS c FROM transactions")
    if cur.fetchone()["c"] == 0:
        db.executemany(
            "INSERT INTO transactions (date,type,amount,category,note) VALUES (?,?,?,?,?)",
            [
                ("2025-01-05", "income", 52000, "其他", "January salary"),
                ("2025-01-08", "expense", 4200, "食物", "Food & dining"),
                ("2025-01-12", "expense", 399, "訂閱服務", "Netflix"),
                ("2025-01-15", "expense", 3500, "其他", "Gifts"),
                ("2025-01-28", "expense", 1500, "投資", "Stock fee"),
                ("2025-02-05", "income", 52000, "其他", "February salary"),
                ("2025-02-09", "expense", 4800, "食物", "Food & dining"),
                ("2025-02-12", "expense", 399, "訂閱服務", "Netflix"),
                ("2025-02-19", "expense", 2100, "購物", "Shopping"),
                ("2025-02-25", "expense", 1200, "投資", "ETF fee"),
                ("2025-03-05", "income", 52000, "其他", "March salary"),
                ("2025-03-07", "expense", 5000, "食物", "Food & dining"),
                ("2025-03-12", "expense", 399, "訂閱服務", "Netflix"),
                ("2025-03-20", "expense", 3500, "購物", "Clothes"),
                ("2025-03-29", "expense", 1500, "投資", "Fund fee"),
            ],
        )
        db.executemany(
            "INSERT INTO investment_positions (date,asset_class,market_value,net_inflow) VALUES (?,?,?,?)",
            [
                ("2025-01-31", "stock", 120000, 5000),
                ("2025-01-31", "etf", 80000, 0),
                ("2025-01-31", "bond", 30000, 0),
                ("2025-01-31", "fund", 20000, 2000),
                ("2025-02-28", "stock", 126000, 3000),
                ("2025-02-28", "etf", 83000, 0),
                ("2025-02-28", "bond", 30200, 0),
                ("2025-02-28", "fund", 22300, 1000),
                ("2025-03-31", "stock", 129000, 2000),
                ("2025-03-31", "etf", 85000, 0),
                ("2025-03-31", "bond", 30500, 0),
                ("2025-03-31", "fund", 23500, 1000),
            ],
        )
        db.executemany(
            "INSERT INTO trackers (month,name,spent,budget) VALUES (?,?,?,?)",
            [
                ("2025-03", "食物花費", 5000, 6000),
                ("2025-03", "購物金", 3500, 4000),
            ],
        )
        db.commit()


with app.app_context():
    init_db()


# -----------------------
# 聚合計算
# -----------------------
def aggregate_monthly_cash():
    """月度：收入、支出、結餘（收入-支出）"""
    db = get_db()
    rows = db.execute(
        """
        SELECT strftime('%Y-%m', date) AS m,
               SUM(CASE WHEN type='income'  THEN amount ELSE 0 END) AS income,
               SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS expense
        FROM transactions
        GROUP BY m
        ORDER BY m
    """
    ).fetchall()
    result = []
    for r in rows:
        inc = r["income"] or 0.0
        exp = r["expense"] or 0.0
        result.append(
            {
                "month": r["m"],
                "income": round(inc, 2),
                "expense": round(exp, 2),
                "balance": round(inc - exp, 2),
            }
        )
    return result


def aggregate_expense_by_category(target_month=None):
    """支出分類：依類別加總（支出）"""
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
    return [
        {"category": r["category"], "amount": round(r["amt"] or 0.0, 2)} for r in rows
    ]


def portfolio_by_month():
    """投資部位：只看 investment_positions（不含消費）"""
    db = get_db()
    rows = db.execute(
        """
        SELECT strftime('%Y-%m', date) AS m, asset_class,
               SUM(market_value) AS mv, SUM(net_inflow) AS inflow
        FROM investment_positions
        GROUP BY m, asset_class
        ORDER BY m
    """
    ).fetchall()
    by_m = defaultdict(lambda: defaultdict(float))
    for r in rows:
        by_m[r["m"]][r["asset_class"]] += r["mv"] or 0.0
        by_m[r["m"]]["_total"] += r["mv"] or 0.0
        by_m[r["m"]]["_net_inflow"] += r["inflow"] or 0.0

    months = sorted(by_m.keys())
    roi = {}
    for i, m in enumerate(months):
        mv_end = by_m[m]["_total"]
        inflow = by_m[m]["_net_inflow"]
        if i == 0:
            roi[m] = None
        else:
            prev_m = months[i - 1]
            mv_start = by_m[prev_m]["_total"]
            roi[m] = (
                round((mv_end - mv_start - inflow) / mv_start, 4) if mv_start > 0 else None
            )
    return by_m, months, roi


def total_assets_trend():
    """累積現金 + 投資市值 → 資產總額趨勢"""
    monthly_cash = {x["month"]: x for x in aggregate_monthly_cash()}
    by_m, months, roi = portfolio_by_month()
    cash_acc = 0.0
    trend = []
    for m in months:
        if m in monthly_cash:
            cash_acc += monthly_cash[m]["balance"]
        mv = by_m[m]["_total"]
        trend.append(
            {
                "month": m,
                "cash": round(cash_acc, 2),
                "investment": round(mv, 2),
                "total": round(cash_acc + mv, 2),
            }
        )
    return trend, roi


def current_month():
    db = get_db()
    m1 = db.execute(
        "SELECT MAX(strftime('%Y-%m', date)) AS m FROM investment_positions"
    ).fetchone()["m"]
    m2 = db.execute(
        "SELECT MAX(strftime('%Y-%m', date)) AS m FROM transactions"
    ).fetchone()["m"]
    candidates = [m for m in (m1, m2) if m]
    if not candidates:
        return datetime.today().strftime("%Y-%m")
    return sorted(candidates)[-1]


# -----------------------
# 頁面 & API
# -----------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/months")
def api_months():
    db = get_db()
    rows = db.execute(
        """
        SELECT strftime('%Y-%m', date) AS m FROM transactions
        UNION
        SELECT strftime('%Y-%m', date) AS m FROM investment_positions
        ORDER BY m
    """
    ).fetchall()
    return jsonify([r["m"] for r in rows])


@app.route("/api/line_income_expense_balance")
def api_line():
    return jsonify(aggregate_monthly_cash())


@app.route("/api/pie_expense_by_category")
def api_pie():
    m = request.args.get("month")
    data = aggregate_expense_by_category(m)
    return jsonify({"month": m or "ALL", "data": data})


@app.route("/api/kpis")
def api_kpis():
    """
    1. 當月收入 = 所有 type='income' 金額加總
    2. 當月支出 = 所有 type='expense' 金額加總
    3. 月現金流 = 收入 − 支出
    4. 儲蓄率   = (收入 − 支出) / 收入
    5. 投資報酬率 = 只用 investment_positions 計算
    """
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
    monthly_roi = roi_map.get(m)
    assets_total = next((t["total"] for t in trend if t["month"] == m), None)

    return jsonify(
        {
            "month": m,
            "income": inc,
            "expense": exp,
            "cash_flow": bal,
            "savings_rate": savings_rate,
            "monthly_roi": monthly_roi,
            "assets_total": assets_total,
        }
    )


@app.route("/api/trackers")
def api_trackers():
    db = get_db()
    m = request.args.get("month") or current_month()
    rows = db.execute(
        "SELECT month,name,spent,budget FROM trackers WHERE month=?", (m,)
    ).fetchall()
    data = []
    for r in rows:
        progress = (
            round(r["spent"] / r["budget"], 4) if r["budget"] and r["budget"] > 0 else None
        )
        data.append(
            {
                "month": r["month"],
                "name": r["name"],
                "spent": r["spent"],
                "budget": r["budget"],
                "progress": progress,
            }
        )
    return jsonify({"month": m, "data": data})


# -----------------------
# 交易 CRUD（含目標連動）
# -----------------------
@app.route("/api/transactions", methods=["GET", "POST"])
def api_transactions():
    db = get_db()

    if request.method == "GET":
        m = request.args.get("month")
        if m:
            rows = db.execute(
                """
                SELECT id, date, type, amount, category, note, goal_id
                FROM transactions
                WHERE strftime('%Y-%m', date)=?
                ORDER BY date, id
            """,
                (m,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT id, date, type, amount, category, note, goal_id
                FROM transactions
                ORDER BY date, id
            """
            ).fetchall()
        return jsonify({"data": [dict(r) for r in rows]})

    # POST：新增交易
    payload = request.get_json(force=True)
    ttype = payload.get("type")
    date = payload.get("date")
    category = payload.get("category")
    note = payload.get("note", "")
    amount = payload.get("amount")
    goal_id = payload.get("goal_id")

    if ttype not in ("income", "expense"):
        return jsonify({"ok": False, "error": "type must be income or expense"}), 400
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except Exception:
        return jsonify({"ok": False, "error": "date must be YYYY-MM-DD"}), 400
    if category not in ("食物", "交通", "購物", "娛樂", "日用品", "投資", "其他", "訂閱服務"):
        return jsonify({"ok": False, "error": "category invalid"}), 400
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError()
    except Exception:
        return jsonify({"ok": False, "error": "amount must be positive"}), 400

    # 目標驗證
    if goal_id in ("", None):
        goal_id_val = None
    else:
        try:
            goal_id_val = int(goal_id)
        except Exception:
            return jsonify({"ok": False, "error": "goal_id invalid"}), 400
        exists = db.execute("SELECT 1 FROM goals WHERE id=?", (goal_id_val,)).fetchone()
        if not exists:
            return jsonify({"ok": False, "error": "目標不存在"}), 400

    cur = db.execute(
        "INSERT INTO transactions (date,type,amount,category,note,goal_id) VALUES (?,?,?,?,?,?)",
        (date, ttype, amount, category, note, goal_id_val),
    )

    # 如果這筆「收入」有指定目標 → 自動累加目標 saved
    if ttype == "income" and goal_id_val is not None:
        db.execute(
            "UPDATE goals SET saved = saved + ? WHERE id=?",
            (amount, goal_id_val),
        )

    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/transactions/<int:tid>", methods=["PUT", "DELETE"])
def api_transaction_detail(tid):
    db = get_db()

    # DELETE：刪除交易，同時回沖目標金額
    if request.method == "DELETE":
        row = db.execute(
            "SELECT type, amount, goal_id FROM transactions WHERE id=?", (tid,)
        ).fetchone()
        if row:
            if row["goal_id"] is not None and row["type"] == "income":
                amt = row["amount"] or 0.0
                db.execute(
                    """
                    UPDATE goals
                    SET saved = CASE WHEN saved - ? >= 0 THEN saved - ? ELSE 0 END
                    WHERE id=?
                    """,
                    (amt, amt, row["goal_id"]),
                )
        db.execute("DELETE FROM transactions WHERE id=?", (tid,))
        db.commit()
        return jsonify({"ok": True})

    # PUT：更新交易（包含可能更換目標或金額）
    payload = request.get_json(force=True)
    old = db.execute(
        "SELECT type, amount, goal_id FROM transactions WHERE id=?", (tid,)
    ).fetchone()
    if not old:
        return jsonify({"ok": False, "error": "transaction not found"}), 404

    fields = []
    params = []

    for key in ("date", "type", "amount", "category", "note", "goal_id"):
        if key not in payload:
            continue
        val = payload[key]

        if key == "type":
            if val not in ("income", "expense"):
                return jsonify({"ok": False, "error": "type invalid"}), 400

        if key == "amount":
            try:
                v = float(val)
                if v <= 0:
                    raise ValueError()
                val = v
            except Exception:
                return (
                    jsonify({"ok": False, "error": "amount must be positive"}),
                    400,
                )

        if key == "date":
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except Exception:
                return (
                    jsonify({"ok": False, "error": "date must be YYYY-MM-DD"}),
                    400,
                )

        if key == "category":
            if val not in (
                "食物",
                "交通",
                "購物",
                "娛樂",
                "日用品",
                "投資",
                "其他",
                "訂閱服務",
            ):
                return jsonify({"ok": False, "error": "category invalid"}), 400

        if key == "goal_id":
            if val in ("", None):
                val = None
            else:
                try:
                    gid = int(val)
                except Exception:
                    return jsonify({"ok": False, "error": "goal_id invalid"}), 400
                exists = db.execute("SELECT 1 FROM goals WHERE id=?", (gid,)).fetchone()
                if not exists:
                    return jsonify({"ok": False, "error": "目標不存在"}), 400
                val = gid

        fields.append(f"{key}=?")
        params.append(val)

    if not fields:
        return jsonify({"ok": False, "error": "no fields"}), 400

    params.append(tid)
    db.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id=?", params)

    # 更新後重新查詢，調整目標 saved
    new = db.execute(
        "SELECT type, amount, goal_id FROM transactions WHERE id=?", (tid,)
    ).fetchone()

    # 把舊紀錄對目標的貢獻先扣掉
    if old["goal_id"] is not None and old["type"] == "income":
        amt = old["amount"] or 0.0
        db.execute(
            """
            UPDATE goals
            SET saved = CASE WHEN saved - ? >= 0 THEN saved - ? ELSE 0 END
            WHERE id=?
            """,
            (amt, amt, old["goal_id"]),
        )

    # 再把新紀錄的貢獻加回去
    if new["goal_id"] is not None and new["type"] == "income":
        amt2 = new["amount"] or 0.0
        db.execute(
            "UPDATE goals SET saved = saved + ? WHERE id=?",
            (amt2, new["goal_id"]),
        )

    db.commit()
    return jsonify({"ok": True})


# -----------------------
# 理財目標 Goals API
# -----------------------
@app.route("/api/goals", methods=["GET", "POST"])
def api_goals():
    db = get_db()
    if request.method == "GET":
        rows = db.execute(
            "SELECT id, name, target, saved FROM goals ORDER BY id"
        ).fetchall()
        data = []
        for r in rows:
            target = r["target"] or 0.0
            saved = r["saved"] or 0.0
            percent = round(saved / target * 100, 1) if target > 0 else 0.0
            data.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "target": target,
                    "saved": saved,
                    "percent": percent,
                }
            )
        return jsonify({"data": data})

    # 新增目標（含目前已存金額）
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    target = payload.get("target")
    saved = payload.get("saved", 0)

    if not name:
        return jsonify({"ok": False, "error": "目標名稱不可空白"}), 400
    try:
        target = float(target)
        saved = float(saved)
        if target <= 0 or saved < 0:
            raise ValueError()
    except Exception:
        return jsonify({"ok": False, "error": "金額需為正數"}), 400

    db.execute(
        "INSERT INTO goals (name, target, saved) VALUES (?, ?, ?)",
        (name, target, saved),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/goals/<int:gid>", methods=["DELETE"])
def api_goal_delete(gid):
    db = get_db()
    db.execute("DELETE FROM goals WHERE id=?", (gid,))
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True)
