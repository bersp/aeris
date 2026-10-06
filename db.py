import random
import sqlite3
import string
from datetime import datetime

DB_PATH = "bills.db"

# The wallet entries go to when nobody has picked another (see init_db).
DEFAULT_WALLET = 1

# All money is stored as an integer number of cents to avoid floating-point
# rounding errors; the bot layer converts to and from a decimal amount.


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def months_ago(months):
    # Midnight on the 1st of the month `months - 1` back, so months_ago(1) is the
    # start of the current month and months_ago(0) the start of the next one.
    # Reporting windows are built from pairs of these.
    now = datetime.now()
    month_index = now.year * 12 + (now.month - 1) - (months - 1)
    year, month = divmod(month_index, 12)
    return datetime(year, month + 1, 1)


def _add_column(conn, table, column, decl):
    # Add a column to an existing table if it is not there yet, so schema changes
    # apply to databases created before the column existed. True when it was just
    # added, for the one-off backfills that go with a new column.
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column in columns:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


def init_db():
    with _connect() as conn:
        # A wallet is a separate set of books: its own expenses, incomes, payments,
        # debts and cash, never added to another's. Wallets are shared by every
        # user, the way debts are. Wallet 1 is the default, and everything logged
        # before wallets existed lands in it. AUTOINCREMENT keeps a removed
        # wallet's id from being handed out again, so a button or chart link left
        # over from it can never open a different wallet.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO wallets (id, name, symbol) VALUES (?, 'ars', '$')",
            (DEFAULT_WALLET,),
        )
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
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # `description` holds an income's name; `note` is the counterpart of an
        # expense's description. It came later, and until it existed the parser
        # had nowhere to put a "name(note)", so it kept the whole thing as the
        # name. Split those as the column appears, otherwise they stay a separate
        # income source forever.
        if _add_column(conn, "incomes", "note", "TEXT"):
            conn.execute(
                """
                UPDATE incomes
                   SET note = nullif(trim(substr(description,
                                                 instr(description, '(') + 1,
                                                 length(description)
                                                   - instr(description, '(') - 1)), ''),
                       description = trim(substr(description, 1,
                                                 instr(description, '(') - 1))
                 WHERE description LIKE '%(%)'
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
        # Who typed the command that recorded the payment (may differ from the
        # payer, since /pay names both sides). Added later and left nullable, so
        # rows from before it fall back to the payer, exactly as expenses do.
        _add_column(conn, "payments", "created_by", "INTEGER")
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
        for table in ("expenses", "scheduled_expenses", "incomes", "payments"):
            _add_column(conn, table, "wallet_id", f"INTEGER NOT NULL DEFAULT {DEFAULT_WALLET}")


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


# --- wallets ---


def add_wallet(name, symbol):
    with _connect() as conn:
        conn.execute("INSERT INTO wallets (name, symbol) VALUES (?, ?)", (name, symbol))


def get_wallet(wallet_id):
    with _connect() as conn:
        return conn.execute(
            "SELECT id, name, symbol FROM wallets WHERE id = ?", (wallet_id,)
        ).fetchone()


def find_wallet(name):
    # The wallet with this name ignoring case, or None.
    with _connect() as conn:
        return conn.execute(
            "SELECT id, name, symbol FROM wallets WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()


def all_wallets():
    with _connect() as conn:
        return conn.execute("SELECT id, name, symbol FROM wallets ORDER BY id").fetchall()


def active_wallet(user_id):
    # The wallet the user's entries go to when they name none: the one they last
    # switched to with /wallet, or the default.
    return get_wallet(int(get_pref(user_id, "wallet", DEFAULT_WALLET)))


# Each kind of entry a wallet holds, and the table it lives in.
_WALLET_TABLES = {
    "expense": "expenses",
    "income": "incomes",
    "payment": "payments",
    "scheduled": "scheduled_expenses",
}


def _wallet_usage(conn, wallet_id):
    return {
        kind: conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()["n"]
        for kind, table in _WALLET_TABLES.items()
    }


def wallet_usage(wallet_id):
    # How many entries of each kind a wallet holds, counting every user's.
    with _connect() as conn:
        return _wallet_usage(conn, wallet_id)


def delete_wallet(wallet_id):
    # Delete a wallet with everything in it, for every user, and send whoever had
    # it active back to the default. Returns how many entries of each kind went
    # with it, counted inside the same transaction so the figure is exact.
    with _connect() as conn:
        usage = _wallet_usage(conn, wallet_id)
        conn.execute(
            "DELETE FROM consumers WHERE expense_seq IN "
            "(SELECT seq FROM expenses WHERE wallet_id = ?)",
            (wallet_id,),
        )
        conn.execute(
            "DELETE FROM scheduled_consumers WHERE scheduled_seq IN "
            "(SELECT seq FROM scheduled_expenses WHERE wallet_id = ?)",
            (wallet_id,),
        )
        for table in _WALLET_TABLES.values():
            conn.execute(f"DELETE FROM {table} WHERE wallet_id = ?", (wallet_id,))
        conn.execute(
            "DELETE FROM preferences WHERE key = 'wallet' AND value = ?", (str(wallet_id),)
        )
        conn.execute("DELETE FROM wallets WHERE id = ?", (wallet_id,))
        return usage


def _wallet_symbol(conn, wallet_id):
    return conn.execute(
        "SELECT symbol FROM wallets WHERE id = ?", (wallet_id,)
    ).fetchone()["symbol"]


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


# A product row outlives what put it there: removing its last expense, rejecting a
# scheduled one or rewriting an entry all leave the name behind. A leftover name
# must not go on collapsing typos onto itself nor keep the next expense that uses
# it from being flagged as new, so the catalog is everything still pointed at.
LIVE_PRODUCT = """
       id IN (SELECT product_id FROM expenses)
    OR id IN (SELECT product_id FROM scheduled_expenses)
    OR id IN (SELECT product_id FROM aliases)
    OR id IN (SELECT product_id FROM product_groups)
"""


def user_products(user_id):
    # The user's product names (their catalog), in creation order. Used to resolve
    # a typed product to an existing one.
    with _connect() as conn:
        return [
            row["name"]
            for row in conn.execute(
                f"SELECT name FROM products WHERE user_id = ? AND ({LIVE_PRODUCT}) "
                "ORDER BY id",
                (user_id,),
            )
        ]


def find_product(user_id, name):
    # The user's product with this name ignoring case, as a row with its id and
    # stored spelling, or None. /rename works on the row rather than the name,
    # since a merge has to move the things pointing at it.
    with _connect() as conn:
        return conn.execute(
            "SELECT id, name FROM products "
            f"WHERE user_id = ? AND name = ? COLLATE NOCASE AND ({LIVE_PRODUCT})",
            (user_id, name),
        ).fetchone()


def product_usage(product_id):
    # How many expenses and pending scheduled expenses a product is used by.
    with _connect() as conn:
        expenses = conn.execute(
            "SELECT COUNT(*) AS n FROM expenses WHERE product_id = ?", (product_id,)
        ).fetchone()["n"]
        scheduled = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_expenses WHERE product_id = ?",
            (product_id,),
        ).fetchone()["n"]
        return expenses, scheduled


def rename_product(user_id, product_id, new_name, target_id=None):
    # Rename one of the user's products. `target_id` is the product that already
    # carries the new name: everything pointing at the renamed one is repointed at
    # it and the old row goes away, so the two become a single product. A product
    # belongs to at most one group, so a merged one keeps the group the target is
    # already in and inherits the old one's only when it has none.
    with _connect() as conn:
        if target_id is None:
            conn.execute(
                "UPDATE products SET name = ? WHERE id = ? AND user_id = ?",
                (new_name, product_id, user_id),
            )
            return
        for table in ("expenses", "scheduled_expenses", "aliases"):
            conn.execute(
                f"UPDATE {table} SET product_id = ? WHERE product_id = ?",
                (target_id, product_id),
            )
        conn.execute(
            "UPDATE product_groups SET product_id = ? WHERE product_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM product_groups WHERE user_id = ? AND product_id = ?)",
            (target_id, product_id, user_id, target_id),
        )
        conn.execute("DELETE FROM product_groups WHERE product_id = ?", (product_id,))
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


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
    # The user's catalog: every product name they can type, how many expenses use
    # it and their aliases for it, most-bought first. This is the same set
    # user_products resolves against, so what the list shows is what a typo
    # collapses onto; a product held only by an alias or a group counts zero.
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT name AS product,
                   (SELECT COUNT(*) FROM expenses e WHERE e.product_id = products.id) AS n,
                   (SELECT GROUP_CONCAT(a.alias, ', ') FROM aliases a
                    WHERE a.product_id = products.id) AS aliases
            FROM products
            WHERE user_id = ? AND ({LIVE_PRODUCT})
            ORDER BY n DESC, name COLLATE NOCASE
            """,
            (user_id,),
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


def group_usage(user_id, name):
    # How many products are in the user's group and the spelling it is stored
    # under, ignoring case; a count of zero means there is no such group.
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n, MAX(name) AS name FROM product_groups "
            "WHERE user_id = ? AND name = ? COLLATE NOCASE",
            (user_id, name),
        ).fetchone()


def rename_group(user_id, old_name, new_name):
    # Rename one of the user's groups. A group is the set of rows sharing a name,
    # so renaming onto one that exists merges their products into a single group.
    with _connect() as conn:
        conn.execute(
            "UPDATE product_groups SET name = ? "
            "WHERE user_id = ? AND name = ? COLLATE NOCASE",
            (new_name, user_id, old_name),
        )


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


def new_id(when):
    return when.strftime("%y%m%d") + "".join(
        random.choices(string.ascii_lowercase, k=2)
    )


def reid(operation_id, when):
    # The id an operation keeps after being rewritten. The two letters stay, so a
    # suffix someone has already typed still finds it, while the date prefix
    # follows `when`, since that is where the rest of the bot reads the date from.
    return when.strftime("%y%m%d") + operation_id[-2:]


def add_expense(
    payer_id, price, product_id, consumer_ids, wallet_id, when=None, description=None,
    created_by=None, expense_id=None,
):
    # `expense_id` lets a rewritten expense keep the id people already know; left
    # out, the expense gets a fresh one.
    when = when or datetime.now()
    expense_id = expense_id or new_id(when)
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO expenses "
            "(id, payer_id, price, product_id, description, created_at, created_by, "
            " wallet_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                expense_id,
                payer_id,
                price,
                product_id,
                description,
                when.isoformat(timespec="microseconds"),
                created_by if created_by is not None else payer_id,
                wallet_id,
            ),
        )
        conn.executemany(
            "INSERT INTO consumers (expense_seq, user_id) VALUES (?, ?)",
            [(cursor.lastrowid, user_id) for user_id in consumer_ids],
        )
    return expense_id


def add_income(
    user_id, amount, description, wallet_id, when=None, income_id=None, note=None
):
    when = when or datetime.now()
    income_id = income_id or new_id(when)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO incomes "
            "(id, user_id, amount, description, note, created_at, wallet_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                income_id, user_id, amount, description, note,
                when.isoformat(timespec="microseconds"), wallet_id,
            ),
        )
    return income_id


def add_payment(payer_id, payee_id, amount, wallet_id, created_by=None):
    when = datetime.now()
    payment_id = new_id(when)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO payments "
            "(id, payer_id, payee_id, amount, created_at, created_by, wallet_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payment_id, payer_id, payee_id, amount,
                when.isoformat(timespec="microseconds"),
                created_by if created_by is not None else payer_id,
                wallet_id,
            ),
        )
    return payment_id


# --- scheduled expenses ---


def add_scheduled_expense(
    created_by, payer_id, price, product_id, consumer_ids, due_date, wallet_id,
    description=None, plan_id=None, plan_index=None, plan_total=None,
    scheduled_id=None,
):
    # Store an expense to be confirmed on `due_date` (a YYYY-MM-DD string). The
    # id embeds the due date so it reads like a real expense id.
    scheduled_id = scheduled_id or new_id(datetime.fromisoformat(due_date))
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO scheduled_expenses "
            "(id, created_by, payer_id, price, product_id, description, due_date, "
            " plan_id, plan_index, plan_total, created_at, wallet_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scheduled_id, created_by, payer_id, price, product_id, description,
                due_date, plan_id, plan_index, plan_total,
                datetime.now().isoformat(timespec="microseconds"), wallet_id,
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
        "wallet_id": row["wallet_id"],
        "symbol": _wallet_symbol(conn, row["wallet_id"]),
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


def pop_scheduled(scheduled_id, user_id):
    # Read the scheduled expense `user_id` created and delete it (with its
    # consumers). Returns its dict, or None if it was already resolved or belongs
    # to someone else. Deleting up front keeps a double Add/Reject tap from
    # recording it twice, and scoping to the creator means the id in a button is
    # not enough on its own to resolve one.
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM scheduled_expenses WHERE id = ? AND created_by = ?",
            (scheduled_id, user_id),
        ).fetchone()
        if row is None:
            return None
        item = _scheduled_dict(conn, row)
        conn.execute(
            "DELETE FROM scheduled_consumers WHERE scheduled_seq = ?", (row["seq"],)
        )
        conn.execute("DELETE FROM scheduled_expenses WHERE seq = ?", (row["seq"],))
        return item


def list_scheduled(user_id, wallet_id):
    # Pending scheduled expenses of a wallet the user takes part in (as creator,
    # payer or consumer), oldest due first.
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_expenses WHERE (created_by = ? OR payer_id = ? "
            "OR seq IN (SELECT scheduled_seq FROM scheduled_consumers WHERE user_id = ?)) "
            "AND wallet_id = ? ORDER BY due_date, seq",
            (user_id, user_id, user_id, wallet_id),
        ).fetchall()
        return [_scheduled_dict(conn, row) for row in rows]


# The kinds of operation /rm and /mod act on. Each entry is the query scoping rows
# to the ones a user wrote, how many times their id fills into it, and the prefix
# its columns need (only the expense query joins another table with an `id`).
#
# Scoped by authorship, not participation: an entry is changed by whoever logged
# it, so sharing an expense or being paid does not hand over the right to rewrite
# or delete it. `created_by` came after the rows that have none, which fall back to
# the payer the same way they do everywhere they are displayed.
_OPERATION_SCOPES = {
    "expense": (
        "SELECT e.seq, e.id, e.created_at, e.price, e.description, e.created_by, "
        "p.name AS product, e.product_id, e.payer_id, e.wallet_id "
        "FROM expenses e JOIN products p ON p.id = e.product_id "
        "WHERE COALESCE(e.created_by, e.payer_id) = ?",
        1,
        "e.",
    ),
    "income": (
        "SELECT seq, id, created_at, amount, description, note, wallet_id "
        "FROM incomes WHERE user_id = ?",
        1,
        "",
    ),
    "payment": (
        "SELECT seq, id, created_at, amount, payer_id, payee_id, wallet_id "
        "FROM payments WHERE COALESCE(created_by, payer_id) = ?",
        1,
        "",
    ),
    "scheduled": (
        "SELECT * FROM scheduled_expenses WHERE created_by = ?",
        1,
        "",
    ),
}


def _scoped_operation(conn, kind, user_id, column, value):
    # The latest row of `kind` the user takes part in whose `column` matches: the
    # id when searching by suffix, the seq when acting on a chosen one.
    query, times, prefix = _OPERATION_SCOPES[kind]
    operator = "LIKE" if column == "id" else "="
    return conn.execute(
        f"{query} AND {prefix}{column} {operator} ? ORDER BY {prefix}seq DESC LIMIT 1",
        (user_id,) * times + (value,),
    ).fetchone()


def _operation_dict(conn, kind, row):
    # One operation in the shape the bot renders, tagged with its "kind" and
    # carrying the "seq" the confirmation button needs to act on it later.
    if kind == "scheduled":
        item = _scheduled_dict(conn, row)
        item["kind"] = "scheduled"
        return item

    if kind == "income":
        return {
            "kind": "income",
            "seq": row["seq"],
            "id": row["id"],
            "created_at": row["created_at"],
            "description": row["description"],
            "note": row["note"],
            "amount": row["amount"],
            "wallet_id": row["wallet_id"],
            "symbol": _wallet_symbol(conn, row["wallet_id"]),
        }

    def name_of(user):
        return conn.execute(
            "SELECT name FROM users WHERE user_id = ?", (user,)
        ).fetchone()

    if kind == "payment":
        return {
            "kind": "payment",
            "seq": row["seq"],
            "id": row["id"],
            "amount": row["amount"],
            "payer_id": row["payer_id"],
            "payee_id": row["payee_id"],
            "payer": name_of(row["payer_id"])["name"],
            "payee": name_of(row["payee_id"])["name"],
            "wallet_id": row["wallet_id"],
            "symbol": _wallet_symbol(conn, row["wallet_id"]),
        }

    payer = name_of(row["payer_id"])["name"]
    creator = name_of(row["created_by"])
    consumers = conn.execute(
        "SELECT c.user_id, u.name FROM consumers c JOIN users u ON u.user_id = c.user_id "
        "WHERE c.expense_seq = ?",
        (row["seq"],),
    ).fetchall()
    return {
        "kind": "expense",
        "seq": row["seq"],
        "id": row["id"],
        "created_at": row["created_at"],
        "product": row["product"],
        "product_id": row["product_id"],
        "price": row["price"],
        "description": row["description"],
        "payer_id": row["payer_id"],
        "created_by": row["created_by"],
        "wallet_id": row["wallet_id"],
        "symbol": _wallet_symbol(conn, row["wallet_id"]),
        "payer": payer,
        "added_by": creator["name"] if creator else payer,
        "consumer_ids": [c["user_id"] for c in consumers],
        "consumers": [c["name"] for c in consumers],
    }


def find_operation(user_id, suffix):
    # The user's expense, income, payment or scheduled expense whose id ends with
    # `suffix`; when several match, the most recent one wins. Read-only, so the
    # bot can show what it is about to delete and ask first. Returns a dict tagged
    # with "kind", or None.
    with _connect() as conn:
        candidates = []
        for kind in _OPERATION_SCOPES:
            row = _scoped_operation(conn, kind, user_id, "id", f"%{suffix}")
            if row is not None:
                candidates.append((kind, row))
        if not candidates:
            return None
        kind, row = max(candidates, key=lambda candidate: candidate[1]["created_at"])
        return _operation_dict(conn, kind, row)


def get_operation(user_id, kind, seq):
    # One specific operation of the user's, by kind and seq.
    if kind not in _OPERATION_SCOPES:
        return None
    with _connect() as conn:
        row = _scoped_operation(conn, kind, user_id, "seq", seq)
        return _operation_dict(conn, kind, row) if row is not None else None


def delete_operation(user_id, kind, seq):
    # Delete the operation the user confirmed, scoping the lookup to them again
    # rather than trusting what came back from the button. Returns the deleted
    # operation, or None when it is already gone, so a double tap does nothing.
    if kind not in _OPERATION_SCOPES:
        return None
    with _connect() as conn:
        row = _scoped_operation(conn, kind, user_id, "seq", seq)
        if row is None:
            return None
        item = _operation_dict(conn, kind, row)
        if kind == "scheduled":
            conn.execute(
                "DELETE FROM scheduled_consumers WHERE scheduled_seq = ?", (seq,)
            )
            conn.execute("DELETE FROM scheduled_expenses WHERE seq = ?", (seq,))
        elif kind == "income":
            conn.execute("DELETE FROM incomes WHERE seq = ?", (seq,))
        elif kind == "payment":
            conn.execute("DELETE FROM payments WHERE seq = ?", (seq,))
        else:
            conn.execute("DELETE FROM consumers WHERE expense_seq = ?", (seq,))
            conn.execute("DELETE FROM expenses WHERE seq = ?", (seq,))
        return item


def first_operation_date(user_id, wallet_id):
    # The YYYY-MM-DD of the user's oldest operation of any kind in a wallet, or
    # None when they have none yet. Bounds how far back the web UI offers to look.
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT MIN(created_at) AS first FROM (
                SELECT e.created_at FROM expenses e
                WHERE (e.payer_id = :user
                       OR e.seq IN (SELECT expense_seq FROM consumers WHERE user_id = :user))
                  AND e.wallet_id = :wallet
                UNION ALL
                SELECT created_at FROM incomes
                WHERE user_id = :user AND wallet_id = :wallet
                UNION ALL
                SELECT created_at FROM payments
                WHERE (payer_id = :user OR payee_id = :user) AND wallet_id = :wallet
            )
            """,
            {"user": user_id, "wallet": wallet_id},
        ).fetchone()
    return row["first"][:10] if row["first"] else None


def recent_operations(user_id, wallet_id, limit=6, since=None, until=None):
    # Operations of a wallet the user takes part in (as payer/consumer of an expense, or as
    # payer/payee of a payment), ordered oldest first. `since`/`until` bound the
    # window (since inclusive, until exclusive); with `limit` only the last that
    # many are kept. A payment is shown as a "pay" product whose single consumer
    # is the payee, so each operation also carries its `kind` for callers that
    # need to tell the two apart.
    with _connect() as conn:
        expenses = conn.execute(
            """
            SELECT e.seq, e.id, e.created_at, p.name AS product, e.description, e.price,
                   u.name AS buyer, COALESCE(creator.name, u.name) AS added_by
            FROM expenses e
            JOIN users u ON u.user_id = e.payer_id
            LEFT JOIN users creator ON creator.user_id = e.created_by
            JOIN products p ON p.id = e.product_id
            WHERE (e.payer_id = ?
                   OR e.seq IN (SELECT expense_seq FROM consumers WHERE user_id = ?))
              AND e.wallet_id = ?
            """,
            (user_id, user_id, wallet_id),
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
                    "kind": "expense",
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
            WHERE (p.payer_id = ? OR p.payee_id = ?) AND p.wallet_id = ?
            """,
            (user_id, user_id, wallet_id),
        ).fetchall()
        for payment in payments:
            operations.append(
                {
                    "kind": "payment",
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
    if until is not None:
        cutoff = until.isoformat()
        operations = [op for op in operations if op["created_at"] < cutoff]
    if limit is not None:
        operations = operations[-limit:]
    return operations


def expense_summary(user_id, wallet_id, since=None, until=None):
    # The user's own share in a wallet (each expense's price split among its consumers),
    # ordered largest first. `since`/`until` bound the window (since inclusive,
    # until exclusive). Payments are not expenses, so they are excluded. Products
    # the user has put in a group collapse into a single row labelled with the
    # group name; ungrouped products keep their own row.
    query = """
        SELECT COALESCE(g.name, p.name) AS product,
               SUM(e.price * 1.0 / counts.n) AS total
        FROM expenses e
        JOIN products p ON p.id = e.product_id
        LEFT JOIN product_groups g
            ON g.product_id = e.product_id AND g.user_id = ?
        JOIN consumers c ON c.expense_seq = e.seq
        JOIN (
            SELECT expense_seq, COUNT(*) AS n FROM consumers GROUP BY expense_seq
        ) counts ON counts.expense_seq = e.seq
        WHERE c.user_id = ? AND e.wallet_id = ?
    """
    params = [user_id, user_id, wallet_id]
    if since is not None:
        query += " AND e.created_at >= ?"
        params.append(since.isoformat())
    if until is not None:
        query += " AND e.created_at < ?"
        params.append(until.isoformat())
    query += " GROUP BY COALESCE(g.name, p.name) ORDER BY total DESC"
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def daily_expense_totals(user_id, wallet_id, since=None, until=None):
    # The user's own share of a wallet's expenses summed per calendar day, oldest first.
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
        WHERE c.user_id = ? AND e.wallet_id = ?
    """
    params = [user_id, wallet_id]
    if since is not None:
        query += " AND e.created_at >= ?"
        params.append(since.isoformat())
    if until is not None:
        query += " AND e.created_at < ?"
        params.append(until.isoformat())
    query += " GROUP BY day ORDER BY day"
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def recent_incomes(user_id, wallet_id, limit=6, since=None, until=None):
    # The user's individual incomes in a wallet, ordered oldest first so the latest is printed
    # last. `since`/`until` bound the window (since inclusive, until exclusive);
    # with `limit` only the last that many are kept.
    query = (
        "SELECT id, created_at, description, note, amount FROM incomes "
        "WHERE user_id = ? AND wallet_id = ?"
    )
    params = [user_id, wallet_id]
    if since is not None:
        query += " AND created_at >= ?"
        params.append(since.isoformat())
    if until is not None:
        query += " AND created_at < ?"
        params.append(until.isoformat())
    query += " ORDER BY created_at"
    with _connect() as conn:
        incomes = [dict(row) for row in conn.execute(query, params)]
    if limit is not None:
        incomes = incomes[-limit:]
    return incomes


def income_summary(user_id, wallet_id, since=None, until=None):
    # The user's incomes in a wallet grouped by description and ordered largest first.
    # `since`/`until` bound the window (since inclusive, until exclusive).
    query = (
        "SELECT description, SUM(amount) AS total FROM incomes "
        "WHERE user_id = ? AND wallet_id = ?"
    )
    params = [user_id, wallet_id]
    if since is not None:
        query += " AND created_at >= ?"
        params.append(since.isoformat())
    if until is not None:
        query += " AND created_at < ?"
        params.append(until.isoformat())
    query += " GROUP BY description ORDER BY total DESC"
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def income_usage(user_id, name):
    # How many of the user's incomes carry this name and the spelling they are
    # stored under, ignoring case; a count of zero means no income has it.
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n, MAX(description) AS name FROM incomes "
            "WHERE user_id = ? AND description = ? COLLATE NOCASE",
            (user_id, name),
        ).fetchone()


def rename_income(user_id, old_name, new_name):
    # Give every income of the user's named `old_name` the new name, ignoring
    # case. Incomes are grouped by name, so renaming onto one that already exists
    # folds the two sources into one.
    with _connect() as conn:
        conn.execute(
            "UPDATE incomes SET description = ? "
            "WHERE user_id = ? AND description = ? COLLATE NOCASE",
            (new_name, user_id, old_name),
        )


def cash_balance(user_id, wallet_id):
    # Real money the user holds in a wallet: incomes received, minus expenses they
    # paid in full, plus payments received from others, minus payments made to
    # others.
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COALESCE(SUM(amount), 0) FROM incomes
               WHERE user_id  = :user AND wallet_id = :wallet)
            - (SELECT COALESCE(SUM(price),  0) FROM expenses
               WHERE payer_id = :user AND wallet_id = :wallet)
            + (SELECT COALESCE(SUM(amount), 0) FROM payments
               WHERE payee_id = :user AND wallet_id = :wallet)
            - (SELECT COALESCE(SUM(amount), 0) FROM payments
               WHERE payer_id = :user AND wallet_id = :wallet)
            AS cash
            """,
            {"user": user_id, "wallet": wallet_id},
        ).fetchone()
        return row["cash"]


def balance(user_id, wallet_id):
    # Net amount per other user in a wallet: positive means they owe the user,
    # negative means the user owes them. Each expense splits its price equally
    # among consumers, and a payment reduces the debt in its own direction.
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
                WHERE c.user_id != e.payer_id AND e.wallet_id = :wallet
            ),
            wallet_payments AS (
                SELECT * FROM payments WHERE wallet_id = :wallet
            )
            SELECT u.name AS name, SUM(d.delta) AS net
            FROM (
                SELECT consumer_id AS other_id,  share  AS delta FROM shares          WHERE payer_id    = :user
                UNION ALL
                SELECT payer_id    AS other_id, -share  AS delta FROM shares          WHERE consumer_id = :user
                UNION ALL
                SELECT payee_id    AS other_id,  amount AS delta FROM wallet_payments WHERE payer_id    = :user
                UNION ALL
                SELECT payer_id    AS other_id, -amount AS delta FROM wallet_payments WHERE payee_id    = :user
            ) d
            JOIN users u ON u.user_id = d.other_id
            GROUP BY d.other_id
            HAVING ROUND(net) != 0
            """,
            {"user": user_id, "wallet": wallet_id},
        ).fetchall()
