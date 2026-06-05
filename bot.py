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
    "/add [date] <product[(description)]> <price> [payer] [beneficiaries]\n"
    "record an expense\n"
    "\n"
    "/addf [date] <product> <price> [payer] [beneficiaries]\n"
    "same as /add, forcing the exact product name\n"
    "\n"
    "/remove <id suffix>\n"
    "remove an expense or income\n"
    "\n"
    "/list [n N | m [K] | d [K]]\n"
    "operations (last 6 by default)\n"
    "\n"
    "/detailedlist [n N | m [K] | d [K]]\n"
    "operations in detail\n"
    "\n"
    "/income [date] <name> <amount>\n"
    "record an income\n"
    "\n"
    "/products\n"
    "list your products with how many times each was bought\n"
    "\n"
    "/alias <alias> <product>\n"
    "define a shortcut for a product (-d <alias> to delete)\n"
    "\n"
    "/balance [n N | m [K] | d [K]]\n"
    "show expenses, debts and money\n"
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


def parse_list_args(args, noun="operations", count_word="Last"):
    # Returns (limit, since, label): how many of the latest operations to show or
    # a datetime from which to keep them, plus a human label for the window.
    # `noun`/`count_word` shape the count-mode label (e.g. "Last 6 operations" vs
    # "Top 6 expenses"). Defaults to the last 6.
    parts = (args or "").split()
    if parts and parts[0] in ("n", "m", "d"):
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
    return 6, None, f"{count_word} 6 {noun}"


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


@dp.message(Command("add"))
async def add(message: Message, command: CommandObject):
    await record_expense(message, command, force=False)


# /addf forces the product exactly as typed, skipping the standardization /add
# applies, for short products that are legitimately similar to existing ones.
@dp.message(Command("addf"))
async def add_forced(message: Message, command: CommandObject):
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

    # /add collapses the product to the sender's existing spelling (same name
    # ignoring case, or a one-letter typo); /addf stores it exactly as typed.
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

    # Own expense: paid and consumed by the sender.
    if len(parts) < 4:
        user_id = message.from_user.id
        expense_id = db.add_expense(user_id, price, product_id, [user_id], when, description)
        name = db.get_name(user_id)
        await reply(
            message,
            operation_message("Added", display_product, price, name, [name], expense_id),
        )
        return

    # Shared expense: "<payer> <beneficiaries>".
    payer = parts[2]
    spec = parts[3]
    names = [payer] + ([] if spec == "all" else spec.split("/"))
    missing = missing_users(names)
    if missing:
        await reply(message, f"Not registered: {', '.join(missing)}")
        return

    payer_id = db.get_user_id(payer)
    if spec == "all":
        beneficiaries = db.all_users()
    else:
        beneficiaries = [(db.get_user_id(name), name) for name in spec.split("/")]

    consumer_ids = [user_id for user_id, name in beneficiaries]
    consumer_names = [name for user_id, name in beneficiaries]
    expense_id = db.add_expense(payer_id, price, product_id, consumer_ids, when, description)

    text = operation_message("Added", display_product, price, payer, consumer_names, expense_id)
    recipients = {message.from_user.id, payer_id} | set(consumer_ids)
    await broadcast(message, recipients, text)


@dp.message(Command("remove"))
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


def short_product(product, has_description=False):
    # Products with a description carry a trailing "+". The whole cell is kept to
    # 7 characters: e.g. "Supermarket" -> "Super..", "Supermarket+" -> "Supe..+".
    suffix = "+" if has_description else ""
    if len(product) + len(suffix) <= 7:
        return product + suffix
    return product[: 7 - 2 - len(suffix)] + ".." + suffix


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


def consumer_cell(consumers, total_users):
    if len(consumers) == 1:
        return consumers[0][:2]
    if len(consumers) == total_users:
        return "All"
    return "Mix"


def format_table(header, rows):
    widths = [max(len(row[i]) for row in [header] + rows) for i in range(len(header))]

    def line(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    separator = "  ".join("-" * width for width in widths)
    return "\n".join([line(header), separator] + [line(row) for row in rows])


def table_block(emoji, label, header, rows):
    # An emoji-labelled table closed by a rule as wide as the table itself.
    table = format_table(header, rows)
    width = max(len(line) for line in table.splitlines())
    return f"{emoji} {label}\n\n{table}\n{'-' * width}"


@dp.message(Command("list"))
async def list_operations(message: Message, command: CommandObject):
    limit, since, label = parse_list_args(command.args)
    operations = db.recent_operations(message.from_user.id, limit, since)
    if not operations:
        await reply(message, "No operations.")
        return
    total_users = db.count_users()
    rows = [
        (
            op["created_at"][8:10],
            short_product(op["product"], bool(op["description"])),
            f"{op['price'] / 100:.0f}",
            op["buyer"][:2],
            consumer_cell(op["consumers"], total_users),
        )
        for op in operations
    ]
    block = table_block("📋", label, ("Day", "Product", "Price", "Pay", "For"), rows)
    await reply(message, block)


@dp.message(Command("detailedlist"))
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
        lines = [f"Day: {day}", f"Product: {op['product']}"]
        if op["description"]:
            lines.append(f"Description: {op['description']}")
        lines += [
            f"Price: {op['price'] / 100:.2f}",
            f"Pay: {op['buyer']}",
            f"For: {', '.join(op['consumers'])}",
            f"ID: {op['id']}",
        ]
        blocks.append("\n".join(lines))
    detail = "\n----\n".join(blocks)
    await reply(message, f"{label}\n{detail}")


@dp.message(Command("income"))
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


@dp.message(Command("balance"))
async def balance(message: Message, command: CommandObject):
    user_id = message.from_user.id
    limit, since, label = parse_list_args(command.args, noun="expenses", count_word="Top")
    summary = db.expense_summary(user_id, limit, since)
    rows = db.balance(user_id)
    cash = db.cash_balance(user_id)
    net = sum(r["net"] for r in rows)

    # Three sections separated by a blank line. Dynamic text is escaped per
    # section so the <b>/<code> tags below pass through untouched.
    sections = []

    if summary:
        expense_rows = [
            (short_product(s["product"], bool(s["has_description"])), f"{s['total'] / 100:.2f}")
            for s in summary
        ]
        block = table_block("🧾", label, ("Product", "Total"), expense_rows)
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


@dp.message(Command("products"))
async def products(message: Message, command: CommandObject):
    rows = db.product_counts(message.from_user.id)
    if not rows:
        await reply(message, "No products.")
        return
    table_rows = [(r["product"], str(r["n"]), r["aliases"] or "") for r in rows]
    block = table_block("🏷️", "Products", ("Product", "Count", "Aliases"), table_rows)
    await reply(message, block)


@dp.message(Command("alias"))
async def alias(message: Message, command: CommandObject):
    # "<alias> <product>": typing <alias> in /add then records <product>.
    # "-d <alias>": delete an alias.
    parts = command.args.split()
    user_id = message.from_user.id

    if parts[0] == "-d":
        alias_name = parts[1]
        if db.remove_alias(user_id, alias_name):
            await reply(message, f"Removed alias {alias_name.lower()}")
        else:
            await reply(message, f"No alias {alias_name.lower()}")
        return

    alias_name, product = parts[0], parts[1]
    existing = resolve_product(user_id, product)
    if existing is not None:
        product = existing
    product_id = db.get_or_create_product(user_id, product)
    db.set_alias(user_id, alias_name, product_id)
    await reply(message, f"Alias {alias_name.lower()} -> {product}")


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
