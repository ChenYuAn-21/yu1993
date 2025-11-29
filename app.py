import os
import sqlite3
from datetime import datetime

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


def ensure_column(db, table, column, col_def):
    """如果指定欄位不存在，就自動 ALTER TABLE 加上去。"""
    info = db.execute(f"PRAGMA table_info({table})").fetchall()
    cols = [r["name"] for r in info]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def init_db():
    db = get_db()
    db.executescript(
        """
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('income','expense')),
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        note TEXT
    );

    -- 保留原本投資部位表（目前 KPI 不用它，但不刪除）
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

    # 舊資料表補欄位
    ensure_column(db, "transactions", "goal_id", "INTEGER")
    ensure_column(db, "goals", "saved", "REAL NOT NULL DEFAULT 0")
    ensure_column(db, "trackers", "category", "TEXT")  # 用來對應支出類別

    db.commit()


with app.app_context():
    init_db()


# -----------------------
# 工具函式
# -----------------------
def current_month():
    """根據 transactions 的最大日期來決定目前的月份。"""
    db = get_db()
    row = db.execute(
        "SELECT MAX(strftime('%Y-%m', date)) AS m FROM transactions"
    ).fetchone()
    if row and row["m"]:
        return row["m"]
    return datetime.today().strftime("%Y-%m")


def aggregate_monthly_cash():
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


# ===== 投資報酬率（用 transactions 的「投資」類別） =====
def calculate_investment_roi(month=None):
    """
    用 transactions 表中 category='投資' 的收支計算投資報酬率。
    ROI = (投資相關收入總額 - 投資相關支出總額) / 投資相關支出總額
    """
    db = get_db()
    if month is None:
        month = current_month()

    row = db.execute(
        """
        SELECT
            SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) AS invested,
            SUM(CASE WHEN type='income'  THEN amount ELSE 0 END) AS returned
        FROM transactions
        WHERE category='投資'
          AND strftime('%Y-%m', date)=?
        """,
        (month,),
    ).fetchone()

    invested = row["invested"] or 0.0
    returned = row["returned"] or 0.0

    if invested <= 0:
        return month, None

    roi = (returned - invested) / invested
    return month, round(roi, 4)


# ===== 資產總額（用所有交易的累積現金） =====
def calculate_total_assets(month=None):
    """
    資產總額簡化定義為：截至該月份為止的累積淨現金流。
    total_assets = Σ(所有收入) - Σ(所有支出)，日期<=該月份。
    """
    db = get_db()
    if month is None:
        month = current_month()

    row = db.execute(
        """
        SELECT
            SUM(CASE WHEN type='income' THEN amount ELSE -amount END) AS net
        FROM transactions
        WHERE strftime('%Y-%m', date) <= ?
        """,
        (month,),
    ).fetchone()

    net = row["net"] or 0.0
    return month, round(net, 2)


# -----------------------
# 頁面 & 基本 API
# -----------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/months")
def api_months():
    db = get_db()
    rows = db.execute(
        """
        SELECT DISTINCT strftime('%Y-%m', date) AS m
        FROM transactions
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
    回傳當月 KPI，包括：
    - income / expense / cash_flow
    - savings_rate 儲蓄率
    - monthly_roi 投資報酬率（用「投資」類別交易算）
    - assets_total 資產總額（用交易累積現金算）
    """
    monthly = aggregate_monthly_cash()
    m_param = request.args.get("month")
    m = m_param or (monthly[-1]["month"] if monthly else current_month())

    inc = exp = bal = 0.0
    for x in monthly:
        if x["month"] == m:
            inc, exp, bal = x["income"], x["expense"], x["balance"]
            break

    savings_rate = round((bal / inc), 4) if inc > 0 else None

    _, monthly_roi = calculate_investment_roi(m)
    _, assets_total = calculate_total_assets(m)

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


# -----------------------
# 財務追蹤 Trackers API
# -----------------------
@app.route("/api/trackers", methods=["GET", "POST"])
def api_trackers():
    """
    財務追蹤：
    - GET：依月份列出所有追蹤項目，
        若有設定 category，就自動用該月該類別的支出當作 spent
        否則使用資料表原本的 spent（相容舊版）
    - POST：新增追蹤項目（name, budget, category, month）
    """
    db = get_db()

    if request.method == "GET":
        m = request.args.get("month") or current_month()

        rows = db.execute(
            """
            SELECT id, month, name, spent, budget, category
            FROM trackers
            WHERE month=?
            ORDER BY id
            """,
            (m,),
        ).fetchall()

        data = []
        for r in rows:
            # 若有綁定支出類別，就用該類別的實際支出算本月花費
            if r["category"]:
                spent_row = db.execute(
                    """
                    SELECT COALESCE(SUM(amount),0) AS s
                    FROM transactions
                    WHERE type='expense'
                      AND strftime('%Y-%m', date)=?
                      AND category=?
                    """,
                    (m, r["category"]),
                ).fetchone()
                spent_dynamic = spent_row["s"] or 0.0
            else:
                # 沒有綁定類別就使用資料表原本的 spent 欄位（相容舊資料）
                spent_dynamic = r["spent"] or 0.0

            budget = r["budget"] or 0.0
            progress = round(spent_dynamic / budget, 4) if budget > 0 else None

            data.append(
                {
                    "id": r["id"],
                    "month": r["month"],
                    "name": r["name"],
                    "category": r["category"],
                    "spent": round(spent_dynamic, 2),
                    "budget": budget,
                    "progress": progress,
                }
            )

        return jsonify({"month": m, "data": data})

    # POST：新增財務追蹤項目
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    budget_raw = payload.get("budget")
    category = payload.get("category")  # 可為 None → 不綁定特定類別
    month = payload.get("month") or current_month()

    if not name:
        return jsonify({"ok": False, "error": "追蹤項目名稱不可空白"}), 400

    try:
        budget = float(budget_raw)
        if budget <= 0:
            raise ValueError()
    except Exception:
        return jsonify({"ok": False, "error": "預算需為正數"}), 400

    db.execute(
        "INSERT INTO trackers (month, name, spent, budget, category) VALUES (?,?,?,?,?)",
        (month, name, 0.0, budget, category),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/trackers/<int:tid>", methods=["DELETE"])
def api_tracker_delete(tid):
    """刪除單一財務追蹤項目。"""
    db = get_db()
    db.execute("DELETE FROM trackers WHERE id=?", (tid,))
    db.commit()
    return jsonify({"ok": True})


# -----------------------
# 交易 CRUD（含 goal_id）
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
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/transactions/<int:tid>", methods=["PUT", "DELETE"])
def api_transaction_detail(tid):
    db = get_db()

    if request.method == "DELETE":
        db.execute("DELETE FROM transactions WHERE id=?", (tid,))
        db.commit()
        return jsonify({"ok": True})

    # PUT：更新交易
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
    db.commit()
    return jsonify({"ok": True})


# -----------------------
# 理財目標 Goals API
# -----------------------
@app.route("/api/goals", methods=["GET", "POST"])
def api_goals():
    db = get_db()

    # 讀取：用交易動態累計 saved_from_tx
    if request.method == "GET":
        rows = db.execute(
            """
            SELECT
                g.id,
                g.name,
                g.target,
                g.saved AS saved_base,
                COALESCE(SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END),0) AS saved_from_tx
            FROM goals g
            LEFT JOIN transactions t
                ON t.goal_id = g.id
            GROUP BY g.id, g.name, g.target, g.saved
            ORDER BY g.id
            """
        ).fetchall()

        data = []
        for r in rows:
            target = r["target"] or 0.0
            base = r["saved_base"] or 0.0
            from_tx = r["saved_from_tx"] or 0.0
            total_saved = base + from_tx
            percent = round(total_saved / target * 100, 1) if target > 0 else 0.0
            data.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "target": target,
                    "saved": total_saved,
                    "percent": percent,
                }
            )
        return jsonify({"data": data})

    # POST：新增理財目標（saved 為使用者輸入的起始累積，可空白）
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    target_raw = payload.get("target")
    saved_raw = payload.get("saved")

    if not name:
        return jsonify({"ok": False, "error": "目標名稱不可空白"}), 400

    # 空白 / None 視為 0
    if saved_raw is None or str(saved_raw).strip() == "":
        saved_raw = 0

    try:
        target = float(target_raw)
        saved = float(saved_raw)
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

