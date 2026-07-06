import random
import sqlite3
import string
from datetime import datetime

DB_PATH = "bills.db"

# All money is stored as an integer number of cents to avoid floating-point
# rounding errors; the bot layer converts to and from a decimal amount.


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(conn, table, column, decl):
    # Add a column to an existing table if it is not there yet, so schema changes
    # apply to databases created before the column existed.
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
        # Each user owns their own catalog of products. Aliases (a future feature)
        # will be a per-user mapping pointing at a product id, which is why expenses
        # reference products by id rather than by name.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL
            )
            """
        )
        # A per-user shortcut typed in place of a product name, e.g. "super" for
        # the user's "Supermarket" product.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aliases (
                user_id INTEGER NOT NULL,
                alias TEXT NOT NULL,
                product_id INTEGER NOT NULL
            )
            """
        )
        # A group folds several products into one line on the balance. A group is
        # the set of rows sharing user_id and name; a product belongs to at most
        # one group. Named "product_groups" because GROUP is a SQL keyword.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_groups (
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                product_id INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                payer_id INTEGER NOT NULL,
                price INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # Who typed the command that created the expense (may differ from the
        # payer). Added later, so backfilled as a nullable column; NULL rows fall
        # back to the payer when displayed.
        _add_column(conn, "expenses", "created_by", "INTEGER")
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
                amount INTEGER NOT NULL,
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
                amount INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # A generic per-user key-value store for settings (e.g. table_width).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
            """
        )
        # An expense scheduled for a future day. On its due_date at 09:00 the
        # creator is prompted (Add/Reject) and, if confirmed, it becomes a real
        # expense. A simple deferred expense has plan_id NULL; installments share
        # a plan_id and carry their index/total (e.g. "2/6").
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_expenses (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                payer_id INTEGER NOT NULL,
                price INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                description TEXT,
                due_date TEXT NOT NULL,
                plan_id TEXT,
                plan_index INTEGER,
                plan_total INTEGER,
                notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_consumers (
                scheduled_seq INTEGER NOT NULL,
                user_id INTEGER NOT NULL
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


# --- preferences ---


def get_pref(user_id, key, default=None):
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM preferences WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        return row["value"] if row else default


def set_pref(user_id, key, value):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, str(value)),
        )


# --- products ---


def get_or_create_product(user_id, name):
    # The id of the user's product with this exact name, creating it if needed.
    # Name resolution (case, typos, future aliases) happens before this is called.
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM products WHERE user_id = ? AND name = ?", (user_id, name)
        ).fetchone()
        if row:
            return row["id"]
        cursor = conn.execute(
            "INSERT INTO products (user_id, name) VALUES (?, ?)", (user_id, name)
        )
        return cursor.lastrowid


def user_products(user_id):
    # The user's product names (their catalog), in creation order. Used to resolve
    # a typed product to an existing one.
    with _connect() as conn:
        return [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM products WHERE user_id = ? ORDER BY id", (user_id,)
            )
        ]


def set_alias(user_id, alias, product_id):
    # Point a (lowercased) alias at one of the user's products, replacing any
    # previous target for that alias.
    alias = alias.lower()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM aliases WHERE user_id = ? AND alias = ?", (user_id, alias)
        )
        conn.execute(
            "INSERT INTO aliases (user_id, alias, product_id) VALUES (?, ?, ?)",
            (user_id, alias, product_id),
        )


def remove_alias(user_id, alias):
    # Delete one of the user's aliases. Returns whether it existed.
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM aliases WHERE user_id = ? AND alias = ?", (user_id, alias.lower())
        )
        return cursor.rowcount > 0


def alias_product(user_id, alias):
    # The product name an alias resolves to for this user, or None.
    with _connect() as conn:
        row = conn.execute(
            "SELECT p.name FROM aliases a JOIN products p ON p.id = a.product_id "
            "WHERE a.user_id = ? AND a.alias = ?",
            (user_id, alias.lower()),
        ).fetchone()
        return row["name"] if row else None


def product_counts(user_id):
    # Each product the user paid for or consumed, with how many expenses used it
    # and the user's aliases for it, most-bought first.
    with _connect() as conn:
        return conn.execute(
            """
            SELECT p.name AS product, COUNT(*) AS n,
                   (SELECT GROUP_CONCAT(a.alias, ', ') FROM aliases a
                    WHERE a.product_id = e.product_id AND a.user_id = ?) AS aliases
            FROM expenses e
            JOIN products p ON p.id = e.product_id
            WHERE e.payer_id = ?
               OR e.seq IN (SELECT expense_seq FROM consumers WHERE user_id = ?)
            GROUP BY e.product_id
            ORDER BY n DESC, p.name COLLATE NOCASE
            """,
            (user_id, user_id, user_id),
        ).fetchall()


# --- groups ---


def set_group(user_id, name, product_ids):
    # Put the given products into the user's group `name`, creating it. A product
    # belongs to at most one group, so any previous membership is replaced.
    with _connect() as conn:
        conn.executemany(
            "DELETE FROM product_groups WHERE user_id = ? AND product_id = ?",
            [(user_id, product_id) for product_id in product_ids],
        )
        conn.executemany(
            "INSERT INTO product_groups (user_id, name, product_id) VALUES (?, ?, ?)",
            [(user_id, name, product_id) for product_id in product_ids],
        )


def remove_group(user_id, name):
    # Delete one of the user's groups (its products become ungrouped). Returns
    # whether it existed.
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM product_groups WHERE user_id = ? AND name = ?", (user_id, name)
        )
        return cursor.rowcount > 0


def list_groups(user_id):
    # Each of the user's groups with its member product names.
    with _connect() as conn:
        return conn.execute(
            """
            SELECT g.name AS name, GROUP_CONCAT(p.name, ', ') AS products
            FROM product_groups g
            JOIN products p ON p.id = g.product_id
            WHERE g.user_id = ?
            GROUP BY g.name
            ORDER BY g.name COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()


# --- operations ---


def _new_id(when):
    return when.strftime("%y%m%d") + "".join(
        random.choices(string.ascii_lowercase, k=2)
    )


def add_expense(
    payer_id, price, product_id, consumer_ids, when=None, description=None, created_by=None
):
    when = when or datetime.now()
    expense_id = _new_id(when)
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO expenses "
            "(id, payer_id, price, product_id, description, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                expense_id,
                payer_id,
                price,
                product_id,
                description,
                when.isoformat(timespec="microseconds"),
                created_by if created_by is not None else payer_id,
            ),
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


# --- scheduled expenses ---


def add_scheduled_expense(
    created_by, payer_id, price, product_id, consumer_ids, due_date,
    description=None, plan_id=None, plan_index=None, plan_total=None,
):
    # Store an expense to be confirmed on `due_date` (a YYYY-MM-DD string). The
    # id embeds the due date so it reads like a real expense id.
    scheduled_id = _new_id(datetime.fromisoformat(due_date))
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO scheduled_expenses "
            "(id, created_by, payer_id, price, product_id, description, due_date, "
            " plan_id, plan_index, plan_total, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scheduled_id, created_by, payer_id, price, product_id, description,
                due_date, plan_id, plan_index, plan_total,
                datetime.now().isoformat(timespec="microseconds"),
            ),
        )
        conn.executemany(
            "INSERT INTO scheduled_consumers (scheduled_seq, user_id) VALUES (?, ?)",
            [(cursor.lastrowid, uid) for uid in consumer_ids],
        )
    return scheduled_id


def _scheduled_dict(conn, row):
    # Flesh out a scheduled_expenses row with product/payer/creator names and its
    # consumers, in the shape the bot renders and records from.
    product = conn.execute(
        "SELECT name FROM products WHERE id = ?", (row["product_id"],)
    ).fetchone()["name"]
    payer = conn.execute(
        "SELECT name FROM users WHERE user_id = ?", (row["payer_id"],)
    ).fetchone()["name"]
    creator = conn.execute(
        "SELECT name FROM users WHERE user_id = ?", (row["created_by"],)
    ).fetchone()
    consumers = conn.execute(
        "SELECT c.user_id, u.name FROM scheduled_consumers c "
        "JOIN users u ON u.user_id = c.user_id WHERE c.scheduled_seq = ?",
        (row["seq"],),
    ).fetchall()
    return {
        "seq": row["seq"],
        "id": row["id"],
        "created_by": row["created_by"],
        "payer_id": row["payer_id"],
        "price": row["price"],
        "product_id": row["product_id"],
        "product": product,
        "description": row["description"],
        "due_date": row["due_date"],
        "plan_id": row["plan_id"],
        "plan_index": row["plan_index"],
        "plan_total": row["plan_total"],
        "payer": payer,
        "added_by": creator["name"] if creator else payer,
        "consumer_ids": [c["user_id"] for c in consumers],
        "consumers": [c["name"] for c in consumers],
    }


def unnotified_scheduled(max_due_date):
    # Scheduled expenses due on or before `max_due_date` (YYYY-MM-DD) that have
    # not been announced yet, oldest first.
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_expenses WHERE notified = 0 AND due_date <= ? "
            "ORDER BY due_date, seq",
            (max_due_date,),
        ).fetchall()
        return [_scheduled_dict(conn, row) for row in rows]


def mark_scheduled_notified(seq):
    with _connect() as conn:
        conn.execute(
            "UPDATE scheduled_expenses SET notified = 1 WHERE seq = ?", (seq,)
        )


def pop_scheduled(scheduled_id):
    # Read a scheduled expense by id and delete it (with its consumers). Returns
    # its dict, or None if it was already resolved. Deleting up front keeps a
    # double Add/Reject tap from recording it twice.
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM scheduled_expenses WHERE id = ?", (scheduled_id,)
        ).fetchone()
        if row is None:
            return None
        item = _scheduled_dict(conn, row)
        conn.execute(
            "DELETE FROM scheduled_consumers WHERE scheduled_seq = ?", (row["seq"],)
        )
        conn.execute("DELETE FROM scheduled_expenses WHERE seq = ?", (row["seq"],))
        return item


def list_scheduled(user_id):
    # Pending scheduled expenses the user takes part in (as creator, payer or
    # consumer), oldest due first.
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_expenses WHERE created_by = ? OR payer_id = ? "
            "OR seq IN (SELECT scheduled_seq FROM scheduled_consumers WHERE user_id = ?) "
            "ORDER BY due_date, seq",
            (user_id, user_id, user_id),
        ).fetchall()
        return [_scheduled_dict(conn, row) for row in rows]


def remove_operation(user_id, suffix):
    # Remove the user's expense, income or payment whose id ends with `suffix`.
    # When several match, the most recent one wins. Returns a dict tagged with
    # "kind", or None.
    like = f"%{suffix}"
    with _connect() as conn:
        expense = conn.execute(
            "SELECT e.seq, e.id, e.created_at, e.price, e.description, e.created_by, "
            "p.name AS product, e.payer_id "
            "FROM expenses e JOIN products p ON p.id = e.product_id "
            "WHERE (e.payer_id = ? OR e.seq IN "
            "(SELECT expense_seq FROM consumers WHERE user_id = ?)) "
            "AND e.id LIKE ? ORDER BY e.seq DESC LIMIT 1",
            (user_id, user_id, like),
        ).fetchone()
        income = conn.execute(
            "SELECT seq, id, created_at, amount, description "
            "FROM incomes WHERE user_id = ? AND id LIKE ? ORDER BY seq DESC LIMIT 1",
            (user_id, like),
        ).fetchone()
        payment = conn.execute(
            "SELECT seq, id, created_at, amount, payer_id, payee_id "
            "FROM payments WHERE (payer_id = ? OR payee_id = ?) AND id LIKE ? "
            "ORDER BY seq DESC LIMIT 1",
            (user_id, user_id, like),
        ).fetchone()

        candidates = [
            (kind, row)
            for kind, row in (("expense", expense), ("income", income), ("payment", payment))
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

        if kind == "payment":
            payer = conn.execute(
                "SELECT name FROM users WHERE user_id = ?", (row["payer_id"],)
            ).fetchone()["name"]
            payee = conn.execute(
                "SELECT name FROM users WHERE user_id = ?", (row["payee_id"],)
            ).fetchone()["name"]
            conn.execute("DELETE FROM payments WHERE seq = ?", (row["seq"],))
            return {
                "kind": "payment",
                "id": row["id"],
                "amount": row["amount"],
                "payer_id": row["payer_id"],
                "payee_id": row["payee_id"],
                "payer": payer,
                "payee": payee,
            }

        payer = conn.execute(
            "SELECT name FROM users WHERE user_id = ?", (row["payer_id"],)
        ).fetchone()["name"]
        creator = conn.execute(
            "SELECT name FROM users WHERE user_id = ?", (row["created_by"],)
        ).fetchone()
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
            "description": row["description"],
            "payer_id": row["payer_id"],
            "payer": payer,
            "added_by": creator["name"] if creator else payer,
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
            SELECT e.seq, e.id, e.created_at, p.name AS product, e.description, e.price,
                   u.name AS buyer, COALESCE(creator.name, u.name) AS added_by
            FROM expenses e
            JOIN users u ON u.user_id = e.payer_id
            LEFT JOIN users creator ON creator.user_id = e.created_by
            JOIN products p ON p.id = e.product_id
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
                    "description": expense["description"],
                    "price": expense["price"],
                    "buyer": expense["buyer"],
                    "added_by": expense["added_by"],
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
                    "description": None,
                    "price": payment["amount"],
                    "buyer": payment["buyer"],
                    "added_by": None,
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


def expense_summary(user_id, since=None, until=None):
    # The user's own share (each expense's price split among its consumers),
    # ordered largest first. `since`/`until` bound the window (since inclusive,
    # until exclusive). Payments are not expenses, so they are excluded. Products
    # the user has put in a group collapse into a single row labelled with the
    # group name; ungrouped products keep their own row.
    query = """
        SELECT COALESCE(g.name, p.name) AS product,
               SUM(e.price * 1.0 / counts.n) AS total,
               MAX(e.description IS NOT NULL AND e.description != '') AS has_description
        FROM expenses e
        JOIN products p ON p.id = e.product_id
        LEFT JOIN product_groups g
            ON g.product_id = e.product_id AND g.user_id = ?
        JOIN consumers c ON c.expense_seq = e.seq
        JOIN (
            SELECT expense_seq, COUNT(*) AS n FROM consumers GROUP BY expense_seq
        ) counts ON counts.expense_seq = e.seq
        WHERE c.user_id = ?
    """
    params = [user_id, user_id]
    if since is not None:
        query += " AND e.created_at >= ?"
        params.append(since.isoformat())
    if until is not None:
        query += " AND e.created_at < ?"
        params.append(until.isoformat())
    query += " GROUP BY COALESCE(g.name, p.name) ORDER BY total DESC"
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def daily_expense_totals(user_id, since=None, until=None):
    # The user's own share of expenses summed per calendar day, oldest first.
    # `since`/`until` bound the window (since inclusive, until exclusive). Days
    # with no expense simply don't appear; the caller fills the gaps.
    query = """
        SELECT substr(e.created_at, 1, 10) AS day,
               SUM(e.price * 1.0 / counts.n) AS total
        FROM expenses e
        JOIN consumers c ON c.expense_seq = e.seq
        JOIN (
            SELECT expense_seq, COUNT(*) AS n FROM consumers GROUP BY expense_seq
        ) counts ON counts.expense_seq = e.seq
        WHERE c.user_id = ?
    """
    params = [user_id]
    if since is not None:
        query += " AND e.created_at >= ?"
        params.append(since.isoformat())
    if until is not None:
        query += " AND e.created_at < ?"
        params.append(until.isoformat())
    query += " GROUP BY day ORDER BY day"
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def income_summary(user_id, since=None, until=None):
    # The user's incomes grouped by description and ordered largest first.
    # `since`/`until` bound the window (since inclusive, until exclusive).
    query = "SELECT description, SUM(amount) AS total FROM incomes WHERE user_id = ?"
    params = [user_id]
    if since is not None:
        query += " AND created_at >= ?"
        params.append(since.isoformat())
    if until is not None:
        query += " AND created_at < ?"
        params.append(until.isoformat())
    query += " GROUP BY description ORDER BY total DESC"
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


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
            HAVING ROUND(net) != 0
            """,
            (user_id, user_id, user_id, user_id),
        ).fetchall()
