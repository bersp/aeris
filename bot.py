import asyncio
import html
import logging
import os
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

import db

load_dotenv()

dp = Dispatcher()

# Commands available before registering.
PUBLIC_COMMANDS = ("/start", "/register")


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


def parse_list_args(args):
    # Returns (limit, since): how many of the latest operations to show, or a
    # datetime from which to keep them. Defaults to the last 6.
    parts = (args or "").split()
    if not parts:
        return 6, None
    mode = parts[0]
    number = int(parts[1]) if len(parts) > 1 else None
    if mode == "n":
        return number or 6, None
    if mode == "m":
        return None, _months_ago(number or 1)
    if mode == "d":
        return None, _days_ago(number or 1)
    return 6, None


@dp.message(Command("start"))
async def start(message: Message):
    await reply(
        message,
        "Aeris\n\n"
        "Commands:\n"
        "/register <name> - register with a name\n"
        "/rename <name> - change your name\n"
        "/add [date] <product> <price> [payer] [beneficiaries] - record an expense\n"
        "/remove <id suffix> - remove an expense or income\n"
        "/list [n N | m [K] | d [K]] - operations (last 6 by default)\n"
        "/detailedlist [n N | m [K] | d [K]] - operations in detail\n"
        "/income [date] <name> <amount> - record an income\n"
        "/balance - show debts and money\n"
        "/pay [payer] <payee> <amount> - record a payment",
    )


@dp.message(Command("register"))
async def register(message: Message, command: CommandObject):
    name = command.args.strip()
    db.set_name(message.from_user.id, name)
    await reply(message, f"Registered as {name}")


@dp.message(Command("rename"))
async def rename(message: Message, command: CommandObject):
    name = command.args.strip()
    db.set_name(message.from_user.id, name)
    await reply(message, f"Renamed to {name}")


@dp.message(Command("add"))
async def add(message: Message, command: CommandObject):
    parts = command.args.split()

    # Optional purchase date as the first argument (today if omitted).
    when = parse_date(parts[0])
    if when is not None:
        parts = parts[1:]

    product = parts[0]
    if is_number(product):
        await reply(message, "Product cannot be a number.")
        return
    price = float(parts[1])

    # Own expense: paid and consumed by the sender.
    if len(parts) < 4:
        user_id = message.from_user.id
        expense_id = db.add_expense(user_id, price, product, [user_id], when)
        name = db.get_name(user_id)
        await reply(
            message, operation_message("Added", product, price, name, [name], expense_id)
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
    expense_id = db.add_expense(payer_id, price, product, consumer_ids, when)

    text = operation_message("Added", product, price, payer, consumer_names, expense_id)
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


def format_price(price):
    return f"{price:.2f}".rstrip("0").rstrip(".")


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


@dp.message(Command("list"))
async def list_operations(message: Message, command: CommandObject):
    limit, since = parse_list_args(command.args)
    operations = db.recent_operations(message.from_user.id, limit, since)
    if not operations:
        await reply(message, "No operations.")
        return
    total_users = db.count_users()
    rows = [
        (
            op["created_at"][8:10],
            op["product"],
            f"{op['price']:.0f}",
            op["buyer"][:2],
            consumer_cell(op["consumers"], total_users),
        )
        for op in operations
    ]
    table = format_table(("Day", "Product", "Price", "Pay", "For"), rows)
    await reply(message, table)


@dp.message(Command("detailedlist"))
async def detailed_list(message: Message, command: CommandObject):
    limit, since = parse_list_args(command.args)
    operations = db.recent_operations(message.from_user.id, limit, since)
    if not operations:
        await reply(message, "No operations.")
        return
    blocks = []
    for op in operations:
        created_at = op["created_at"]
        day = f"{created_at[8:10]}/{created_at[5:7]}/{created_at[2:4]}"
        blocks.append(
            f"Day: {day}\n"
            f"Product: {op['product']}\n"
            f"Price: {op['price']:.2f}\n"
            f"Pay: {op['buyer']}\n"
            f"For: {', '.join(op['consumers'])}\n"
            f"ID: {op['id']}"
        )
    detail = "\n----\n".join(blocks)
    await reply(message, detail)


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
    amount = float(parts[1])
    income_id = db.add_income(message.from_user.id, amount, name, when)
    await reply(message, f"Income {name} (${format_price(amount)})\nID: {income_id}")


@dp.message(Command("balance"))
async def balance(message: Message):
    user_id = message.from_user.id
    summary = db.monthly_expense_summary(user_id)
    rows = db.balance(user_id)
    cash = db.cash_balance(user_id)
    net = sum(r["net"] for r in rows)

    # Three sections separated by a blank line. Dynamic text is escaped per
    # section so the <b>/<code> tags below pass through untouched.
    sections = []

    if summary:
        expense_rows = [(s["product"], f"{s['total']:.2f}") for s in summary]
        table = format_table(("Product", "Total"), expense_rows)
        sections.append(html.escape(table))

    if rows:
        debts = []
        for r in rows:
            if r["net"] > 0:
                debts.append(f"{r['name']} owes you ${r['net']:.2f}")
            else:
                debts.append(f"You owe {r['name']} ${-r['net']:.2f}")
        sections.append(html.escape("\n".join(debts)))

    sections.append(
        f"<b>{html.escape(f'Cash: ${cash:.2f}')}</b>\n"
        f"<b>{html.escape(f'After debts: ${cash + net:.2f}')}</b>"
    )

    body = "\n\n".join(sections)
    await message.answer(f"<code>{body}</code>", parse_mode="HTML")


@dp.message(Command("pay"))
async def pay(message: Message, command: CommandObject):
    parts = command.args.split()
    amount = float(parts[-1])

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
