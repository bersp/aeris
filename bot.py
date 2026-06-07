import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

import db

load_dotenv()

dp = Dispatcher()

# Commands available before registering.
PUBLIC_COMMANDS = ("/start", "/help", "/register")

# Command reference shown by /start and /help. Each command and its description
# go on their own lines, with a blank line between entries, so the message stays
# readable even when the client wraps long lines.
HELP_TEXT = (
    "Aeris\n"
    "\n"
    "Commands:\n"
    "\n"
    "/register <name>\n"
    "register or rename yourself\n"
    "\n"
    "/expense [date] <product[(description)]> <price> [payer] [beneficiaries]\n"
    "record an expense; the product tries to match a similar existing one "
    "(also /exp)\n"
    "\n"
    "/forceexpense [date] <product> <price> [payer] [beneficiaries]\n"
    "same as /expense but stores the product exactly as typed (also /fexp)\n"
    "\n"
    "/remove <id suffix>\n"
    "remove an expense or income (also /rm)\n"
    "\n"
    "/list [n N | m [K] | d [K]]\n"
    "operations (last month by default)\n"
    "\n"
    "/detailedlist [n N | m [K] | d [K]]\n"
    "operations in detail (also /dlist)\n"
    "\n"
    "/income [date] <name> <amount>\n"
    "record an income (also /inc)\n"
    "\n"
    "/incomebalance [m [K]]\n"
    "incomes per month (last K months) (also /incb)\n"
    "\n"
    "/products\n"
    "list your products with how many times each was bought\n"
    "\n"
    "/alias <alias> <product>\n"
    "define a shortcut for a product\n"
    "\n"
    "/rmalias <alias>\n"
    "delete a product alias\n"
    "\n"
    "/balance [m [K]]\n"
    "expenses per month (last K months), debts and money\n"
    "\n"
    "/tablewidth [number]\n"
    "set the width of the /list and /balance tables (no number restores the default)\n"
    "\n"
    "/pay [payer] <payee> <amount>\n"
    "record a payment"
)


# Every bot reply is sent as monospace so it is visually distinct from user text.
async def reply(message, text):
    await message.answer(f"<code>{html.escape(text)}</code>", parse_mode="HTML")


async def broadcast(message, user_ids, text):
    body = f"<code>{html.escape(text)}</code>"
    for user_id in user_ids:
        await message.bot.send_message(user_id, body, parse_mode="HTML")


class RequireRegistration(BaseMiddleware):
    async def __call__(self, handler, message: Message, data):
        command = message.text.split(maxsplit=1)[0] if message.text else ""
        if command not in PUBLIC_COMMANDS and not db.is_registered(message.from_user.id):
            await reply(message, "Register first with /register <name>")
            return
        return await handler(message, data)


dp.message.outer_middleware(RequireRegistration())


def missing_users(names):
    return [name for name in names if db.get_user_id(name) is None]


def parse_date(token):
    # A purchase date as dd/mm/yy, dd/mm or dd (missing parts taken from today),
    # kept at the current time of day so same-day entries stay ordered by when
    # they were added. Returns None if the token is not a date.
    now = datetime.now()
    for fmt in ("%d/%m/%y", "%d/%m", "%d"):
        try:
            parsed = datetime.strptime(token, fmt)
            date = parsed.replace(
                year=parsed.year if "%y" in fmt else now.year,
                month=parsed.month if "%m" in fmt else now.month,
            ).date()
        except ValueError:
            continue
        return datetime.combine(date, now.time())
    return None


def _within_one_edit(a, b):
    # True if a and b are equal or differ by a single character insertion,
    # deletion or substitution (compared case-insensitively).
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def resolve_product(user_id, product):
    # The registered spelling this product should collapse to: a defined alias
    # wins, then an exact match (ignoring case), then the first product differing
    # by a single letter. None if it is a brand-new product. Earliest registered
    # wins.
    aliased = db.alias_product(user_id, product)
    if aliased is not None:
        return aliased
    products = db.user_products(user_id)
    for existing in products:
        if existing.lower() == product.lower():
            return existing
    for existing in products:
        if _within_one_edit(existing, product):
            return existing
    return None


def is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def _days_ago(days):
    start_day = datetime.now().date() - timedelta(days=days - 1)
    return datetime.combine(start_day, datetime.min.time())


def _months_ago(months):
    now = datetime.now()
    month_index = now.year * 12 + (now.month - 1) - (months - 1)
    year, month = divmod(month_index, 12)
    return datetime(year, month + 1, 1)


def parse_list_args(args, noun="operations", count_word="Last", default="n 6"):
    # Returns (limit, since, label): how many of the latest operations to show or
    # a datetime from which to keep them, plus a human label for the window.
    # `noun`/`count_word` shape the count-mode label (e.g. "Last 6 operations" vs
    # "Top 6 expenses"). `default` is used when no valid mode is given.
    parts = (args or "").split()
    if not (parts and parts[0] in ("n", "m", "d")):
        parts = default.split()
    mode = parts[0]
    number = int(parts[1]) if len(parts) > 1 else None
    if mode == "n":
        n = number or 6
        return n, None, f"{count_word} {n} {noun}"
    if mode == "m":
        k = number or 1
        return None, _months_ago(k), "Last month" if k == 1 else f"Last {k} months"
    k = number or 1
    return None, _days_ago(k), "Last day" if k == 1 else f"Last {k} days"


@dp.message(Command("start"))
async def start(message: Message):
    await reply(message, HELP_TEXT)


@dp.message(Command("help"))
async def help_command(message: Message):
    await reply(message, HELP_TEXT)


@dp.message(Command("register"))
async def register(message: Message, command: CommandObject):
    name = command.args.strip()
    user_id = message.from_user.id
    old_name = db.get_name(user_id)
    db.set_name(user_id, name)
    if old_name is None:
        await reply(message, f"Registered as {name}")
    else:
        await reply(message, f"Renamed from {old_name} to {name}")


@dp.message(Command("expense", "exp"))
async def expense(message: Message, command: CommandObject):
    await record_expense(message, command, force=False)


# /forceexpense forces the product exactly as typed, skipping the standardization
# /expense applies, for short products that are legitimately similar to existing
# ones.
@dp.message(Command("forceexpense", "fexp"))
async def force_expense(message: Message, command: CommandObject):
    await record_expense(message, command, force=True)


async def record_expense(message, command, force):
    args = command.args

    # Optional description in parentheses attached to the product, e.g.
    # "others(some note)". It may contain spaces, so pull it out before splitting.
    match = re.search(r"\(([^)]*)\)", args)
    description = (match.group(1).strip() if match else "") or None
    if match:
        args = args[: match.start()] + args[match.end() :]
    parts = args.split()

    # Optional purchase date as the first argument (today if omitted).
    when = parse_date(parts[0])
    if when is not None:
        parts = parts[1:]

    product = parts[0]
    if is_number(product):
        await reply(message, "Product cannot be a number.")
        return
    price = parse_amount(parts[1])

    # /expense collapses the product to the sender's existing spelling (same name
    # ignoring case, or a one-letter typo); /forceexpense stores it exactly as
    # typed.
    # Either way a product not seen before is flagged with a trailing "*" in the
    # confirmation (display only, never stored).
    if force:
        is_new = product not in db.user_products(message.from_user.id)
        display_product = f"{product}*" if is_new else product
    else:
        existing = resolve_product(message.from_user.id, product)
        is_new = existing is None
        if existing is not None:
            product = existing
        display_product = f"{product}*" if is_new else product

    product_id = db.get_or_create_product(message.from_user.id, product)

    # Personal expense: paid and consumed by the sender.
    if len(parts) < 3:
        user_id = message.from_user.id
        expense_id = db.add_expense(user_id, price, product_id, [user_id], when, description)
        name = db.get_name(user_id)
        await reply(
            message,
            operation_message("Added", display_product, price, name, [name], expense_id),
        )
        return

    # Shared expense. A single trailing field is the beneficiaries and the sender
    # is the payer; two fields are "<payer> <beneficiaries>". Beneficiaries are
    # comma-separated names, or "all" for every registered user.
    if len(parts) < 4:
        payer = db.get_name(message.from_user.id)
        spec = parts[2]
    else:
        payer = parts[2]
        spec = parts[3]
    names = [payer] + ([] if spec == "all" else spec.split(","))
    missing = missing_users(names)
    if missing:
        await reply(message, f"Not registered: {', '.join(missing)}")
        return

    payer_id = db.get_user_id(payer)
    if spec == "all":
        beneficiaries = db.all_users()
    else:
        beneficiaries = [(db.get_user_id(name), name) for name in spec.split(",")]

    consumer_ids = [user_id for user_id, name in beneficiaries]
    consumer_names = [name for user_id, name in beneficiaries]
    expense_id = db.add_expense(payer_id, price, product_id, consumer_ids, when, description)

    text = operation_message("Added", display_product, price, payer, consumer_names, expense_id)
    recipients = {message.from_user.id, payer_id} | set(consumer_ids)
    await broadcast(message, recipients, text)


@dp.message(Command("remove", "rm"))
async def remove(message: Message, command: CommandObject):
    suffix = command.args.strip()
    removed = db.remove_operation(message.from_user.id, suffix)
    if removed is None:
        await reply(message, "Nothing to remove.")
        return
    if removed["kind"] == "income":
        await reply(
            message,
            f"Removed income {removed['description']} (${format_price(removed['amount'])})\n"
            f"ID: {removed['id']}",
        )
        return
    text = operation_message(
        "Removed",
        removed["product"],
        removed["price"],
        removed["payer"],
        removed["consumers"],
        removed["id"],
    )
    recipients = {message.from_user.id, removed["payer_id"]} | set(removed["consumer_ids"])
    await broadcast(message, recipients, text)


def parse_amount(text):
    # A decimal amount typed by the user, stored as an integer number of cents.
    return round(float(text) * 100)


def format_price(cents):
    return f"{cents / 100:.2f}".rstrip("0").rstrip(".")


DEFAULT_TABLE_WIDTH = 30  # used until a user picks their own with /tablewidth
MIN_TABLE_WIDTH = 20  # below this the Product column can't hold its header


def table_width(user_id):
    return int(db.get_pref(user_id, "table_width", DEFAULT_TABLE_WIDTH))


def truncate(text, width):
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def pretty_product(name):
    # Product names are typed as a single token, so underscores stand in for the
    # spaces they are shown with, e.g. "my_product" -> "my product".
    return name.replace("_", " ")


def product_cell(name, description, width):
    # Product name plus its description in parentheses, kept to `width` chars.
    # When it overflows, the description is cut and a "...)" tail is preserved:
    # "Supermarket (drink by sugar t...)".
    name = pretty_product(name)
    if not description:
        return truncate(name, width)
    full = f"{name} ({description})"
    if len(full) <= width:
        return full
    keep = width - len(name) - len(" (") - len("...)")
    if keep < 1:
        return truncate(name, width)
    return f"{name} ({description[:keep]}...)"


def operation_message(verb, product, price, payer_name, consumer_names, op_id):
    return (
        f"{verb} {product} (${format_price(price)})\n"
        f"Paid by {payer_name} for {', '.join(consumer_names)}\n"
        f"ID: {op_id}"
    )


def payment_message(payer_name, payee_name, amount, payment_id):
    return (
        f"Payment (${format_price(amount)})\n"
        f"From {payer_name} to {payee_name}\n"
        f"ID: {payment_id}"
    )


def format_table(header, rows, widths=None):
    if widths is None:
        widths = [max(len(row[i]) for row in [header] + rows) for i in range(len(header))]

    def line(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    separator = "  ".join("-" * width for width in widths)
    return "\n".join([line(header), separator] + [line(row) for row in rows])


def table_block(emoji, label, header, rows, widths=None):
    # An emoji-labelled table closed by a rule as wide as the table itself.
    table = format_table(header, rows, widths)
    width = max(len(line) for line in table.splitlines())
    return f"{emoji} {label}\n\n{table}\n{'-' * width}"


@dp.message(Command("list"))
async def list_operations(message: Message, command: CommandObject):
    limit, since, label = parse_list_args(command.args, default="m 1")
    operations = db.recent_operations(message.from_user.id, limit, since)
    if not operations:
        await reply(message, "No operations.")
        return
    width = table_width(message.from_user.id)
    day_w = max(len("Day"), *(len(op["created_at"][8:10]) for op in operations))
    prices = [f"{op['price'] / 100:.0f}" for op in operations]
    price_w = max(len("Price"), *(len(p) for p in prices))
    product_w = width - day_w - price_w - 4
    rows = [
        (
            op["created_at"][8:10],
            product_cell(op["product"], op["description"], product_w),
            price,
        )
        for op, price in zip(operations, prices)
    ]
    widths = [day_w, product_w, price_w]
    block = table_block("📋", label, ("Day", "Product", "Price"), rows, widths)
    await reply(message, block)


@dp.message(Command("detailedlist", "dlist"))
async def detailed_list(message: Message, command: CommandObject):
    limit, since, label = parse_list_args(command.args)
    operations = db.recent_operations(message.from_user.id, limit, since)
    if not operations:
        await reply(message, "No operations.")
        return
    blocks = []
    for op in operations:
        created_at = op["created_at"]
        day = f"{created_at[8:10]}/{created_at[5:7]}/{created_at[2:4]}"
        lines = [
            f"🗓 {day}   #{op['id']}",
            f"🛒 {pretty_product(op['product'])} — ${format_price(op['price'])}",
        ]
        if op["description"]:
            lines.append(f"📝 {op['description']}")
        lines.append(f"👤 {op['buyer']} → {', '.join(op['consumers'])}")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    await reply(message, f"📖 {label}\n\n{body}")


@dp.message(Command("income", "inc"))
async def income(message: Message, command: CommandObject):
    parts = command.args.split()

    # Optional date as the first argument (today if omitted).
    when = parse_date(parts[0])
    if when is not None:
        parts = parts[1:]

    name = parts[0]
    if is_number(name):
        await reply(message, "Name cannot be a number.")
        return
    amount = parse_amount(parts[1])
    income_id = db.add_income(message.from_user.id, amount, name, when)
    await reply(message, f"Income {name} (${format_price(amount)})\nID: {income_id}")


def parse_balance_months(args):
    # /balance only takes a month count: "m K" or "m" (1). Anything else falls
    # back to the current month.
    parts = (args or "").split()
    if parts and parts[0] == "m":
        return int(parts[1]) if len(parts) > 1 else 1
    return 1


@dp.message(Command("balance"))
async def balance(message: Message, command: CommandObject):
    user_id = message.from_user.id
    months = parse_balance_months(command.args)
    rows = db.balance(user_id)
    cash = db.cash_balance(user_id)
    net = sum(r["net"] for r in rows)

    # Sections separated by a blank line: one expense table per month, then
    # debts, then the cash summary. Dynamic text is escaped per section so the
    # <b>/<code> tags below pass through untouched.
    sections = []

    # One table per month, oldest first; each product's share is over that
    # month's own total.
    for i in range(months, 0, -1):
        start, end = _months_ago(i), _months_ago(i - 1)
        summary = db.expense_summary(user_id, start, end)
        if not summary:
            continue
        month_total = sum(s["total"] for s in summary)
        amounts = [f"{s['total'] / 100:.0f}" for s in summary]
        parens = [f"({round(s['total'] / month_total * 100)}%)" for s in summary]
        amount_w = max(len(a) for a in amounts)
        paren_w = max(len(p) for p in parens)
        totals = [
            f"{amount:>{amount_w}} {paren:>{paren_w}}"
            for amount, paren in zip(amounts, parens)
        ]
        total_w = max(len("Total"), *(len(t) for t in totals))
        product_w = table_width(user_id) - total_w - 2
        expense_rows = [
            (product_cell(s["product"], None, product_w), total)
            for s, total in zip(summary, totals)
        ]
        widths = [product_w, total_w]
        block = table_block(
            "🧾", start.strftime("%B %Y"), ("Product", "Total"), expense_rows, widths
        )
        sections.append(html.escape(block))

    if rows:
        debts = []
        for r in rows:
            if r["net"] > 0:
                debts.append(f"🤝 {r['name']} owes you ${r['net'] / 100:.2f}")
            else:
                debts.append(f"🤝 You owe {r['name']} ${-r['net'] / 100:.2f}")
        sections.append(html.escape("\n".join(debts)))

    sections.append(
        f"<b>{html.escape(f'💵 Cash: ${cash / 100:.2f}')}</b>\n"
        f"<b>{html.escape(f'💰 After debts: ${(cash + net) / 100:.2f}')}</b>"
    )

    body = "\n\n".join(sections)
    await message.answer(f"<code>{body}</code>", parse_mode="HTML")


@dp.message(Command("incomebalance", "incb"))
async def income_balance(message: Message, command: CommandObject):
    user_id = message.from_user.id
    months = parse_balance_months(command.args)
    sections = []

    # One table per month, oldest first; each income's share is over that
    # month's own total.
    for i in range(months, 0, -1):
        start, end = _months_ago(i), _months_ago(i - 1)
        summary = db.income_summary(user_id, start, end)
        if not summary:
            continue
        month_total = sum(s["total"] for s in summary)
        amounts = [f"{s['total'] / 100:.0f}" for s in summary]
        parens = [f"({round(s['total'] / month_total * 100)}%)" for s in summary]
        amount_w = max(len(a) for a in amounts)
        paren_w = max(len(p) for p in parens)
        totals = [
            f"{amount:>{amount_w}} {paren:>{paren_w}}"
            for amount, paren in zip(amounts, parens)
        ]
        total_w = max(len("Total"), *(len(t) for t in totals))
        name_w = table_width(user_id) - total_w - 2
        income_rows = [
            (product_cell(s["description"], None, name_w), total)
            for s, total in zip(summary, totals)
        ]
        widths = [name_w, total_w]
        block = table_block(
            "📈", start.strftime("%B %Y"), ("Income", "Total"), income_rows, widths
        )
        block += f"\n\nTotal: ${month_total / 100:.2f}"
        sections.append(html.escape(block))

    if not sections:
        await reply(message, "No incomes.")
        return
    body = "\n\n".join(sections)
    await message.answer(f"<code>{body}</code>", parse_mode="HTML")


@dp.message(Command("products"))
async def products(message: Message, command: CommandObject):
    rows = db.product_counts(message.from_user.id)
    if not rows:
        await reply(message, "No products.")
        return
    table_rows = [(pretty_product(r["product"]), str(r["n"]), r["aliases"] or "") for r in rows]
    block = table_block("🏷️", "Products", ("Product", "Count", "Aliases"), table_rows)
    await reply(message, block)


@dp.message(Command("alias"))
async def alias(message: Message, command: CommandObject):
    # "<alias> <product>": typing <alias> in /expense then records <product>.
    parts = command.args.split()
    user_id = message.from_user.id

    alias_name, product = parts[0], parts[1]
    existing = resolve_product(user_id, product)
    if existing is not None:
        product = existing
    product_id = db.get_or_create_product(user_id, product)
    db.set_alias(user_id, alias_name, product_id)
    await reply(message, f"Alias {alias_name.lower()} -> {product}")


@dp.message(Command("rmalias"))
async def rmalias(message: Message, command: CommandObject):
    alias_name = command.args.strip()
    user_id = message.from_user.id
    if db.remove_alias(user_id, alias_name):
        await reply(message, f"Removed alias {alias_name.lower()}")
    else:
        await reply(message, f"No alias {alias_name.lower()}")


@dp.message(Command("tablewidth"))
async def tablewidth(message: Message, command: CommandObject):
    value = (command.args or "").strip()
    if not value:
        db.set_pref(message.from_user.id, "table_width", DEFAULT_TABLE_WIDTH)
        await reply(message, f"Table width reset to {DEFAULT_TABLE_WIDTH}")
        return
    if not value.isdigit():
        await reply(message, "Usage: /tablewidth [number]")
        return
    width = int(value)
    if width < MIN_TABLE_WIDTH:
        await reply(message, f"Width must be at least {MIN_TABLE_WIDTH}")
        return
    db.set_pref(message.from_user.id, "table_width", width)
    await reply(message, f"Table width set to {width}")


@dp.message(Command("pay"))
async def pay(message: Message, command: CommandObject):
    parts = command.args.split()
    amount = parse_amount(parts[-1])

    # "<payer> <payee> <amount>" lets anyone record a payment between two users;
    # "<payee> <amount>" means the sender is the payer.
    if len(parts) == 3:
        payer_name, payee_name = parts[0], parts[1]
        names = [payer_name, payee_name]
    else:
        payee_name = parts[0]
        payer_name = db.get_name(message.from_user.id)
        names = [payee_name]

    missing = missing_users(names)
    if missing:
        await reply(message, f"Not registered: {', '.join(missing)}")
        return

    payer_id = db.get_user_id(payer_name) if len(parts) == 3 else message.from_user.id
    payee_id = db.get_user_id(payee_name)
    payment_id = db.add_payment(payer_id, payee_id, amount)

    text = payment_message(payer_name, payee_name, amount, payment_id)
    await broadcast(message, {message.from_user.id, payer_id, payee_id}, text)


async def main():
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    bot = Bot(token=os.environ["BOT_TOKEN"])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
