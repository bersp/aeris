import random
import sqlite3
import string
from datetime import datetime

DB_PATH = "bills.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                payer_id INTEGER NOT NULL,
                price REAL NOT NULL,
                product TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consumers (
                expense_seq INTEGER NOT NULL,
                user_id INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incomes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                payer_id INTEGER NOT NULL,
                payee_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


# --- users ---


def set_name(user_id, name):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (user_id, name) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user_id, name),
        )


def get_user_id(name):
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE name = ?", (name,)
        ).fetchone()
        return row["user_id"] if row else None


def get_name(user_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT name FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["name"] if row else None


def is_registered(user_id):
    with _connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            is not None
        )


def all_users():
    with _connect() as conn:
        return conn.execute(
            "SELECT user_id, name FROM users ORDER BY name"
        ).fetchall()


def count_users():
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


# --- operations ---


def _new_id(when):
    return when.strftime("%y%m%d") + "".join(
        random.choices(string.ascii_lowercase, k=2)
    )


def add_expense(payer_id, price, product, consumer_ids, when=None):
    when = when or datetime.now()
    expense_id = _new_id(when)
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO expenses (id, payer_id, price, product, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (expense_id, payer_id, price, product, when.isoformat(timespec="microseconds")),
        )
        conn.executemany(
            "INSERT INTO consumers (expense_seq, user_id) VALUES (?, ?)",
            [(cursor.lastrowid, user_id) for user_id in consumer_ids],
        )
    return expense_id


def add_income(user_id, amount, description, when=None):
    when = when or datetime.now()
    income_id = _new_id(when)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO incomes (id, user_id, amount, description, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (income_id, user_id, amount, description, when.isoformat(timespec="microseconds")),
        )
    return income_id


def add_payment(payer_id, payee_id, amount):
    when = datetime.now()
    payment_id = _new_id(when)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO payments (id, payer_id, payee_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (payment_id, payer_id, payee_id, amount, when.isoformat(timespec="microseconds")),
        )
    return payment_id


def remove_operation(user_id, suffix):
    # Remove the user's expense or income whose id ends with `suffix`. When both
    # an expense and an income match, the most recent one wins. Returns a dict
    # tagged with "kind", or None.
    like = f"%{suffix}"
    with _connect() as conn:
        expense = conn.execute(
            "SELECT seq, id, created_at, price, product, payer_id "
            "FROM expenses WHERE payer_id = ? AND id LIKE ? ORDER BY seq DESC LIMIT 1",
            (user_id, like),
        ).fetchone()
        income = conn.execute(
            "SELECT seq, id, created_at, amount, description "
            "FROM incomes WHERE user_id = ? AND id LIKE ? ORDER BY seq DESC LIMIT 1",
            (user_id, like),
        ).fetchone()

        candidates = [
            (kind, row)
            for kind, row in (("expense", expense), ("income", income))
            if row is not None
        ]
        if not candidates:
            return None
        kind, row = max(candidates, key=lambda c: c[1]["created_at"])

        if kind == "income":
            conn.execute("DELETE FROM incomes WHERE seq = ?", (row["seq"],))
            return {
                "kind": "income",
                "id": row["id"],
                "description": row["description"],
                "amount": row["amount"],
            }

        payer = conn.execute(
            "SELECT name FROM users WHERE user_id = ?", (row["payer_id"],)
        ).fetchone()["name"]
        consumers = conn.execute(
            "SELECT c.user_id, u.name FROM consumers c JOIN users u ON u.user_id = c.user_id "
            "WHERE c.expense_seq = ?",
            (row["seq"],),
        ).fetchall()
        conn.execute("DELETE FROM consumers WHERE expense_seq = ?", (row["seq"],))
        conn.execute("DELETE FROM expenses WHERE seq = ?", (row["seq"],))
        return {
            "kind": "expense",
            "id": row["id"],
            "product": row["product"],
            "price": row["price"],
            "payer_id": row["payer_id"],
            "payer": payer,
            "consumer_ids": [c["user_id"] for c in consumers],
            "consumers": [c["name"] for c in consumers],
        }


def recent_operations(user_id, limit=6, since=None):
    # Operations the user takes part in (as payer/consumer of an expense, or as
    # payer/payee of a payment), ordered oldest first. With `since` only the ones
    # from that datetime on are kept; with `limit` only the last that many. A
    # payment is shown as a "pay" product whose single consumer is the payee.
    with _connect() as conn:
        expenses = conn.execute(
            """
            SELECT e.seq, e.id, e.created_at, e.product, e.price, u.name AS buyer
            FROM expenses e
            JOIN users u ON u.user_id = e.payer_id
            WHERE e.payer_id = ?
               OR e.seq IN (SELECT expense_seq FROM consumers WHERE user_id = ?)
            """,
            (user_id, user_id),
        ).fetchall()
        operations = []
        for expense in expenses:
            consumers = [
                row["name"]
                for row in conn.execute(
                    "SELECT u.name FROM consumers c JOIN users u ON u.user_id = c.user_id "
                    "WHERE c.expense_seq = ?",
                    (expense["seq"],),
                )
            ]
            operations.append(
                {
                    "id": expense["id"],
                    "created_at": expense["created_at"],
                    "product": expense["product"],
                    "price": expense["price"],
                    "buyer": expense["buyer"],
                    "consumers": consumers,
                }
            )
        payments = conn.execute(
            """
            SELECT p.id, p.created_at, p.amount, payer.name AS buyer, payee.name AS payee
            FROM payments p
            JOIN users payer ON payer.user_id = p.payer_id
            JOIN users payee ON payee.user_id = p.payee_id
            WHERE p.payer_id = ? OR p.payee_id = ?
            """,
            (user_id, user_id),
        ).fetchall()
        for payment in payments:
            operations.append(
                {
                    "id": payment["id"],
                    "created_at": payment["created_at"],
                    "product": "pay",
                    "price": payment["amount"],
                    "buyer": payment["buyer"],
                    "consumers": [payment["payee"]],
                }
            )
    # Ordered oldest first so the latest is printed last.
    operations.sort(key=lambda op: op["created_at"])
    if since is not None:
        cutoff = since.isoformat()
        operations = [op for op in operations if op["created_at"] >= cutoff]
    if limit is not None:
        operations = operations[-limit:]
    return operations


def monthly_expense_summary(user_id, limit=6):
    # The user's own share (each expense's price split among its consumers) of
    # this month's expenses, grouped by product and ordered largest first.
    # Payments are not expenses, so they are excluded.
    with _connect() as conn:
        return conn.execute(
            """
            SELECT e.product AS product,
                   SUM(e.price * 1.0 / counts.n) AS total
            FROM expenses e
            JOIN consumers c ON c.expense_seq = e.seq
            JOIN (
                SELECT expense_seq, COUNT(*) AS n FROM consumers GROUP BY expense_seq
            ) counts ON counts.expense_seq = e.seq
            WHERE c.user_id = ?
              AND strftime('%Y-%m', e.created_at) = strftime('%Y-%m', 'now', 'localtime')
            GROUP BY e.product
            ORDER BY total DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def cash_balance(user_id):
    # Real money the user holds: incomes received, minus expenses they paid in
    # full, plus payments received from others, minus payments made to others.
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COALESCE(SUM(amount), 0) FROM incomes  WHERE user_id  = ?)
            - (SELECT COALESCE(SUM(price),  0) FROM expenses WHERE payer_id = ?)
            + (SELECT COALESCE(SUM(amount), 0) FROM payments WHERE payee_id = ?)
            - (SELECT COALESCE(SUM(amount), 0) FROM payments WHERE payer_id = ?)
            AS cash
            """,
            (user_id, user_id, user_id, user_id),
        ).fetchone()
        return row["cash"]


def balance(user_id):
    # Net amount per other user: positive means they owe the user, negative means
    # the user owes them. Each expense splits its price equally among consumers,
    # and a payment reduces the debt in its own direction.
    with _connect() as conn:
        return conn.execute(
            """
            WITH shares AS (
                SELECT e.payer_id, c.user_id AS consumer_id,
                       e.price * 1.0 / counts.n AS share
                FROM expenses e
                JOIN consumers c ON c.expense_seq = e.seq
                JOIN (
                    SELECT expense_seq, COUNT(*) AS n FROM consumers GROUP BY expense_seq
                ) counts ON counts.expense_seq = e.seq
                WHERE c.user_id != e.payer_id
            )
            SELECT u.name AS name, SUM(d.delta) AS net
            FROM (
                SELECT consumer_id AS other_id,  share  AS delta FROM shares   WHERE payer_id    = ?
                UNION ALL
                SELECT payer_id    AS other_id, -share  AS delta FROM shares   WHERE consumer_id = ?
                UNION ALL
                SELECT payee_id    AS other_id,  amount AS delta FROM payments WHERE payer_id    = ?
                UNION ALL
                SELECT payer_id    AS other_id, -amount AS delta FROM payments WHERE payee_id    = ?
            ) d
            JOIN users u ON u.user_id = d.other_id
            GROUP BY d.other_id
            HAVING ROUND(net, 2) != 0
            """,
            (user_id, user_id, user_id, user_id),
        ).fetchall()
