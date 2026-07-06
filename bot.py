import asyncio
import calendar
import functools
import html
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv

import db

load_dotenv()

dp = Dispatcher()

# Public HTTPS URL of the Mini App page (plot.html). A Telegram Web App button
# only opens HTTPS URLs, so this is required for the chart button in /balance.
WEBAPP_URL = os.environ.get("WEBAPP_URL")

# Commands available before registering.
PUBLIC_COMMANDS = ("/start", "/help", "/register")

# Onboarding guide shown by /start and /help. Written as flowing documentation
# (not a terse command list) and sent as HTML. Section titles are bold; each
# command leads with its spec on its own line, prefixed with a ▸ marker so it
# stands out from prose even where inline code renders in the same color as text.
# Literal angle brackets are written as &lt;/&gt; and ampersands as &amp;.
HELP_TEXT = (
    "<b>Aeris</b>\n"
    "\n"
    "Aeris tracks what you spend and earn, and works out who owes what when you "
    "share costs. Run <code>/help</code> to see this again.\n"
    "\n"
    "The leading slash is optional: <code>income salary 6000</code> means "
    "<code>/income salary 6000</code>. An unknown first word means "
    "<code>/expense</code>, so <code>supermarket 1500</code> becomes "
    "<code>/expense supermarket 1500</code>.\n"
    "\n"
    "<b>Register</b>\n"
    "\n"
    "▸ <code>/register &lt;name&gt;</code>\n"
    "Set your name first; others use it to share expenses. Run again to change it.\n"
    "\n"
    "<b>Recording an expense</b>\n"
    "\n"
    "▸ <code>/expense [date] &lt;product[(note)]&gt; &lt;price&gt; [payer] [beneficiaries]</code>\n"
    "The everyday command, also <code>/exp</code>. Simplest is a product and price: "
    "<code>/exp coffee 1500</code>, dated today, paid by you. A leading date sets "
    "the day: <code>05</code>, <code>05/04</code> or <code>05/04/25</code> (e.g. "
    "<code>/exp 05/04 coffee 1500</code>). Parentheses after the product become a "
    "note: "
    "<code>/exp coffee(with Bob) 1500</code>. Aeris remembers products and fixes "
    "typos (<code>cofee</code> → <code>coffee</code>); a new one is marked with a "
    "*.\n"
    "\n"
    "▸ <code>/forceexpense [date] &lt;product&gt; &lt;price&gt; [payer] [beneficiaries]</code>\n"
    "Like <code>/expense</code> but keeps the product exactly as typed, no typo "
    "matching. Short: <code>/fexp</code>.\n"
    "\n"
    "<b>Sharing an expense</b>\n"
    "\n"
    "List who shares the cost after the price, comma-separated: "
    "<code>/exp pizza 8000 Alice,Bob</code> means you paid, Alice and Bob split it. "
    "<code>all</code> means everyone registered: "
    "<code>/exp supermarket 20000 all</code>. Another payer goes first: "
    "<code>/exp pizza 8000 Bob Alice,Carol</code>.\n"
    "\n"
    "<b>Scheduling</b>\n"
    "\n"
    "A date after today defers an expense: nothing is recorded now, and on that "
    "day at 9am Aeris sends you its details with Add and Reject buttons. "
    "<code>/exp 20/12 gift 5000</code> asks you on the 20th.\n"
    "\n"
    "▸ <code>/scheduleexpense &lt;count&gt;&lt;m|d&gt; &lt;start date&gt; &lt;product&gt; &lt;price&gt; [payer] [beneficiaries]</code>\n"
    "Pay in installments, also <code>/sexp</code>. The first field sets how many "
    "charges and the gap between them: <code>6m</code> is six monthly, "
    "<code>6d</code> six daily. <code>/sexp 6m 15/08 rent 5000</code> asks you to "
    "confirm rent on the 15th of each month for six months.\n"
    "\n"
    "<b>Removing an entry</b>\n"
    "\n"
    "▸ <code>/remove &lt;id suffix&gt;</code>\n"
    "Delete something logged by mistake, also <code>/rm</code>. Pass the last few "
    "characters of its id, shown in replies and <code>/detailedlist</code>: "
    "<code>/rm fk</code>.\n"
    "\n"
    "<b>Reviewing your spending</b>\n"
    "\n"
    "▸ <code>/list [n N | m [K] | d [K]]</code>\n"
    "Your operations as a compact table, this month by default. Other windows: "
    "<code>/list n 10</code> (last ten), <code>/list m 3</code> (three months), "
    "<code>/list d 7</code> (seven days).\n"
    "\n"
    "▸ <code>/detailedlist [n N | m [K] | d [K]]</code>\n"
    "Same entries, fully detailed. Short: <code>/dlist</code>.\n"
    "\n"
    "▸ <code>/balance [m [K]]</code>\n"
    "Expenses by month, who owes whom, and your cash. A month count goes back "
    "further: <code>/balance m 3</code>.\n"
    "\n"
    "<b>Settling up</b>\n"
    "\n"
    "▸ <code>/pay [payer] &lt;payee&gt; &lt;amount&gt;</code>\n"
    "Record a cash payment against a debt. <code>/pay Bob 5000</code> means you paid "
    "Bob. A payment between others names the payer first: "
    "<code>/pay Alice Bob 5000</code>.\n"
    "\n"
    "<b>Income</b>\n"
    "\n"
    "▸ <code>/income [date] &lt;name&gt; &lt;amount&gt;</code>\n"
    "Money coming in, also <code>/inc</code>: <code>/inc salary 300000</code>. Dates "
    "work as in expenses.\n"
    "\n"
    "▸ <code>/incomes [m [K]]</code>\n"
    "Your income per month with a total; takes a month count like "
    "<code>/balance</code>.\n"
    "\n"
    "<b>Aliases and products</b>\n"
    "\n"
    "▸ <code>/alias &lt;alias&gt; &lt;product&gt;</code>\n"
    "A shortcut for a long name: <code>/alias g groceries</code> lets <code>g</code> "
    "stand for <code>groceries</code>.\n"
    "\n"
    "▸ <code>/rmalias &lt;alias&gt;</code>\n"
    "Removes an alias.\n"
    "\n"
    "▸ <code>/products</code>\n"
    "Every product you bought, how often, and its aliases.\n"
    "\n"
    "<b>Groups</b>\n"
    "\n"
    "▸ <code>/group &lt;name&gt; &lt;product,product,...&gt;</code>\n"
    "Fold products into one balance line: "
    "<code>/group transport taxi,subway,train</code> shows their total as "
    "<code>transport</code>. Run again with the same name to add more.\n"
    "\n"
    "▸ <code>/ungroup &lt;name&gt;</code>\n"
    "Undo a group.\n"
    "\n"
    "▸ <code>/groups</code>\n"
    "Your groups and their products.\n"
    "\n"
    "<b>Display</b>\n"
    "\n"
    "▸ <code>/tablewidth [number]</code>\n"
    "Resize the tables if they wrap: <code>/tablewidth 40</code>. No number restores "
    "the default."
)


# Every bot reply is sent as monospace so it is visually distinct from user text.
async def reply(message, text):
    await message.answer(f"<code>{html.escape(text)}</code>", parse_mode="HTML")


async def broadcast(message, user_ids, text):
    body = f"<code>{html.escape(text)}</code>"
    for user_id in user_ids:
        await message.bot.send_message(user_id, body, parse_mode="HTML")


def usage(text):
    # Wrap a command handler so malformed input replies with how to use the
    # command instead of failing. Missing or wrongly-typed arguments surface as
    # one of these errors while parsing (e.g. no price, a non-numeric amount, or
    # no arguments at all), and are turned into a "Usage: ..." hint.
    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            message = args[0] if args else kwargs["message"]
            try:
                return await handler(*args, **kwargs)
            except (IndexError, ValueError, TypeError, AttributeError):
                await reply(message, f"Usage: {text}")

        return wrapper

    return decorator


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


def _add_months(d, k):
    # `d` shifted forward by `k` months, clamping the day to the target month's
    # last day (e.g. 31 Jan + 1 month -> 28 Feb).
    month_index = d.year * 12 + (d.month - 1) + k
    year, month = divmod(month_index, 12)
    last_day = calendar.monthrange(year, month + 1)[1]
    return d.replace(year=year, month=month + 1, day=min(d.day, last_day))


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
async def start(message: Message, command: CommandObject = None):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("help"))
async def help_command(message: Message, command: CommandObject = None):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("register"))
@usage("/register <name>")
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
@usage("/exp [date] <product> <price> [payer] [beneficiaries]")
async def expense(message: Message, command: CommandObject):
    await record_expense(message, command, force=False)


# /forceexpense forces the product exactly as typed, skipping the standardization
# /expense applies, for short products that are legitimately similar to existing
# ones.
@dp.message(Command("forceexpense", "fexp"))
@usage("/fexp [date] <product> <price> [payer] [beneficiaries]")
async def force_expense(message: Message, command: CommandObject):
    await record_expense(message, command, force=True)


@dp.message(Command("scheduleexpense", "sexp"))
@usage("/sexp <count><m|d> <start date> <product> <price> [payer] [beneficiaries]")
async def schedule_expense(message: Message, command: CommandObject):
    # Pay something in installments: <count><m|d> sets how many charges and the
    # gap between them (months or days); the first lands on the start date and
    # each installment is confirmed on its own day like a deferred expense.
    head, rest = command.args.split(maxsplit=1)
    plan = re.fullmatch(r"(\d+)([md])", head)
    count, unit = int(plan.group(1)), plan.group(2)
    if count < 1:
        raise ValueError

    try:
        e = parse_expense_input(message.from_user.id, rest, force=False)
    except InputError as error:
        await reply(message, str(error))
        return
    if e["when"] is None:
        raise ValueError  # a start date is required
    start = e["when"].date()

    plan_id = db._new_id(datetime.combine(start, datetime.min.time()))
    dues = [
        _add_months(start, i) if unit == "m" else start + timedelta(days=i)
        for i in range(count)
    ]
    for i, due in enumerate(dues):
        db.add_scheduled_expense(
            message.from_user.id, e["payer_id"], e["price"], e["product_id"],
            e["consumer_ids"], due.isoformat(), e["description"],
            plan_id=plan_id, plan_index=i + 1, plan_total=count,
        )

    lines = [
        f"Scheduled {count} installments of "
        f"{pretty_product(e['display_product'])} (${format_price(e['price'])} each)",
        f"Paid by {e['payer_name']} for {', '.join(e['consumer_names'])}",
    ]
    for i, due in enumerate(dues):
        lines.append(f"▸ {day_from_iso(due.isoformat())}   {i + 1}/{count}")
    await reply(message, "\n".join(lines))


class InputError(Exception):
    # A user-facing complaint that already reads well (e.g. "Not registered: X"),
    # as opposed to the malformed-input errors that /usage turns into a hint.
    pass


def parse_expense_input(sender_id, args, force):
    # Parse the shared "[date] <product[(note)]> <price> [payer] [beneficiaries]"
    # form used by /expense and /scheduleexpense, resolve the product and the
    # participants, and return them as a dict. Raises InputError for problems that
    # have their own message; lets malformed input raise so /usage handles it.

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
        raise InputError("Product cannot be a number.")
    price = parse_amount(parts[1])

    # /expense collapses the product to the sender's existing spelling (same name
    # ignoring case, or a one-letter typo); /forceexpense stores it exactly as
    # typed.
    # Either way a product not seen before is flagged with a trailing "*" in the
    # confirmation (display only, never stored).
    if force:
        is_new = product not in db.user_products(sender_id)
        display_product = f"{product}*" if is_new else product
    else:
        existing = resolve_product(sender_id, product)
        is_new = existing is None
        if existing is not None:
            product = existing
        display_product = f"{product}*" if is_new else product

    product_id = db.get_or_create_product(sender_id, product)

    # Work out who pays and who consumes. Personal expense (no third field): the
    # sender pays and consumes. Shared: a single trailing field is the
    # beneficiaries with the sender as payer; two fields are "<payer>
    # <beneficiaries>". Beneficiaries are comma-separated names, or "all".
    if len(parts) < 3:
        payer_id = sender_id
        payer_name = db.get_name(payer_id)
        consumer_ids = [payer_id]
        consumer_names = [payer_name]
    else:
        if len(parts) < 4:
            payer_name = db.get_name(sender_id)
            spec = parts[2]
        else:
            payer_name = parts[2]
            spec = parts[3]
        names = [payer_name] + ([] if spec == "all" else spec.split(","))
        missing = missing_users(names)
        if missing:
            raise InputError(f"Not registered: {', '.join(missing)}")
        payer_id = db.get_user_id(payer_name)
        if spec == "all":
            beneficiaries = db.all_users()
        else:
            beneficiaries = [(db.get_user_id(name), name) for name in spec.split(",")]
        consumer_ids = [uid for uid, name in beneficiaries]
        consumer_names = [name for uid, name in beneficiaries]

    return {
        "when": when,
        "description": description,
        "product_id": product_id,
        "display_product": display_product,
        "price": price,
        "payer_id": payer_id,
        "payer_name": payer_name,
        "consumer_ids": consumer_ids,
        "consumer_names": consumer_names,
    }


async def record_expense(message, command, force):
    try:
        e = parse_expense_input(message.from_user.id, command.args, force)
    except InputError as error:
        await reply(message, str(error))
        return

    when = e["when"]
    creator_id = message.from_user.id
    creator_name = db.get_name(creator_id)

    # A future date defers the expense: it is stored as scheduled and confirmed on
    # its day (see run_scheduler / the sched: callback) instead of recorded now.
    if when is not None and when.date() > datetime.now().date():
        scheduled_id = db.add_scheduled_expense(
            creator_id, e["payer_id"], e["price"], e["product_id"], e["consumer_ids"],
            when.date().isoformat(), e["description"],
        )
        detail = expense_detail(
            day_str(when), scheduled_id, e["display_product"], e["price"], e["description"],
            e["payer_name"], e["consumer_names"], creator_name,
        )
        await reply(message, f"Scheduled Expense\n{detail}")
        return

    expense_id = db.add_expense(
        e["payer_id"], e["price"], e["product_id"], e["consumer_ids"], when,
        e["description"], created_by=creator_id,
    )
    detail = expense_detail(
        day_str(when), expense_id, e["display_product"], e["price"], e["description"],
        e["payer_name"], e["consumer_names"], creator_name,
    )
    recipients = {creator_id, e["payer_id"]} | set(e["consumer_ids"])
    await broadcast(message, recipients, f"Added Expense\n{detail}")


@dp.message(Command("remove", "rm"))
@usage("/rm <id suffix>")
async def remove(message: Message, command: CommandObject):
    suffix = command.args.strip()
    removed = db.remove_operation(message.from_user.id, suffix)
    if removed is None:
        await reply(message, "Nothing to remove.")
        return
    if removed["kind"] == "income":
        detail = income_detail(
            day_from_id(removed["id"]), removed["id"], removed["description"], removed["amount"]
        )
        await reply(message, f"Removed Income\n{detail}")
        return
    if removed["kind"] == "payment":
        detail = payment_detail(
            day_from_id(removed["id"]), removed["id"],
            removed["payer"], removed["payee"], removed["amount"],
        )
        recipients = {message.from_user.id, removed["payer_id"], removed["payee_id"]}
        await broadcast(message, recipients, f"Removed Payment\n{detail}")
        return
    detail = expense_detail(
        day_from_id(removed["id"]), removed["id"], removed["product"], removed["price"],
        removed["description"], removed["payer"], removed["consumers"], removed["added_by"],
    )
    recipients = {message.from_user.id, removed["payer_id"]} | set(removed["consumer_ids"])
    await broadcast(message, recipients, f"Removed Expense\n{detail}")


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


def expense_detail(day, op_id, product, price, description, buyer, consumers, added_by=None):
    # One expense rendered in the /detailedlist style: a header line with the day
    # and id, the product and price, an optional note, and who paid for whom. When
    # `added_by` is given it is appended to the participants line so the reader
    # knows who logged it.
    lines = [
        f"▸ {day}   #{op_id}",
        f"- {pretty_product(product)} — ${format_price(price)}",
    ]
    if description:
        lines.append(f"- {description}")
    line = f"- {buyer} → {', '.join(consumers)}"
    if added_by:
        line += f"  (by {added_by})"
    lines.append(line)
    return "\n".join(lines)


def income_detail(day, op_id, name, amount):
    # An income in the same style as expense_detail: header line plus one line
    # with the income name and amount.
    return f"▸ {day}   #{op_id}\n- {pretty_product(name)} — ${format_price(amount)}"


def payment_detail(day, op_id, payer_name, payee_name, amount):
    # A payment in the same style: header line plus who paid whom and how much.
    return f"▸ {day}   #{op_id}\n- {payer_name} → {payee_name} — ${format_price(amount)}"


def day_str(when=None):
    return (when or datetime.now()).strftime("%d/%m/%y")


def day_from_id(op_id):
    # The dd/mm/yy an operation was dated, read back from its id, which begins
    # with the date as yymmdd (see db._new_id).
    return f"{op_id[4:6]}/{op_id[2:4]}/{op_id[0:2]}"


def day_from_iso(iso_date):
    # dd/mm/yy from a YYYY-MM-DD date string (a scheduled expense's due_date).
    return f"{iso_date[8:10]}/{iso_date[5:7]}/{iso_date[2:4]}"


def plan_label(item):
    # "installment K/N" for an installment plan, "simple" for a one-off deferral.
    if item["plan_id"]:
        return f"installment {item['plan_index']}/{item['plan_total']}"
    return "simple"


def scheduled_detail(item, marker=None):
    # A scheduled expense in the expense_detail style, dated its due day. `marker`
    # is appended to the header line (used by /scheduled to flag simple vs
    # installment).
    day = day_from_iso(item["due_date"])
    if marker:
        day = f"{day}   {marker}"
    return expense_detail(
        day, item["id"], item["product"], item["price"], item["description"],
        item["payer"], item["consumers"], item["added_by"],
    )


def scheduled_prompt(item):
    # The 9am message asking the creator to confirm a due scheduled expense, with
    # Add/Reject inline buttons carrying the scheduled id.
    if item["plan_id"]:
        header = f"Add installment {item['plan_index']}/{item['plan_total']}?"
    else:
        header = "Add this expense?"
    text = f"<code>{html.escape(header + chr(10) + scheduled_detail(item))}</code>"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Add", callback_data=f"sched:add:{item['id']}"),
            InlineKeyboardButton(text="Reject", callback_data=f"sched:reject:{item['id']}"),
        ]]
    )
    return text, markup


def confirm_scheduled(item):
    # Turn a due scheduled expense into a real one, dated its due day. Returns the
    # confirmation text and the set of participants to notify.
    due = datetime.fromisoformat(item["due_date"])
    when = datetime.combine(due.date(), datetime.now().time())
    expense_id = db.add_expense(
        item["payer_id"], item["price"], item["product_id"], item["consumer_ids"],
        when, item["description"], created_by=item["created_by"],
    )
    detail = expense_detail(
        day_str(when), expense_id, item["product"], item["price"], item["description"],
        item["payer"], item["consumers"], item["added_by"],
    )
    recipients = {item["created_by"], item["payer_id"]} | set(item["consumer_ids"])
    return f"Added Expense\n{detail}", recipients


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
@usage("/list [n N | m K | d K]")
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
@usage("/dlist [n N | m K | d K]")
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
        blocks.append(
            expense_detail(
                day,
                op["id"],
                op["product"],
                op["price"],
                op["description"],
                op["buyer"],
                op["consumers"],
                op["added_by"],
            )
        )
    body = "\n\n".join(blocks)
    await reply(message, f"📖 {label}\n\n{body}")


@dp.message(Command("income", "inc"))
@usage("/inc [date] <name> <amount>")
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
    detail = income_detail(day_str(when), income_id, name, amount)
    await reply(message, f"Added Income\n{detail}")


def parse_balance_months(args):
    # /balance only takes a month count: "m K" or "m" (1). Anything else falls
    # back to the current month.
    parts = (args or "").split()
    if parts and parts[0] == "m":
        return int(parts[1]) if len(parts) > 1 else 1
    return 1


@dp.message(Command("balance"))
@usage("/balance [m K]")
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
    markup = build_chart_markup(user_id, months)
    await message.answer(f"<code>{body}</code>", parse_mode="HTML", reply_markup=markup)


def build_chart_markup(user_id, months):
    # Builds the "Open chart" inline button for a user's cumulative spending. By
    # default it overlays the current month and the previous one for comparison;
    # a month count (from "/balance m K") overlays that many months. The data is
    # computed here and handed to the static page (plot.html) through the URL
    # fragment, so the page needs no backend and the numbers never reach the web
    # server's logs. Returns None when plotting is not configured (no WEBAPP_URL).
    if not WEBAPP_URL:
        return None

    # Always at least two months, so the current one has something to compare
    # against.
    count = max(months, 2)
    today = datetime.now().date()

    # One cumulative daily series per month, most recent first. Each series is a
    # running total per day from the 1st, empty days filled so the line stays
    # continuous; the current month stops at today, past months run to month end.
    series = []
    for i in range(count):
        start, end = _months_ago(i + 1), _months_ago(i)
        totals = {
            row["day"]: row["total"]
            for row in db.daily_expense_totals(user_id, start, end)
        }
        last = today if i == 0 else (end.date() - timedelta(days=1))
        labels, values, running = [], [], 0.0
        day = start.date()
        while day <= last:
            running += totals.get(day.isoformat(), 0) / 100
            labels.append(day.day)
            values.append(round(running, 2))
            day += timedelta(days=1)
        series.append(
            {"label": start.strftime("%B %Y"), "labels": labels, "values": values}
        )

    # The current month's total income, drawn as a reference line on the chart.
    income_cents = sum(
        row["total"]
        for row in db.income_summary(user_id, _months_ago(1), _months_ago(0))
    )
    payload = {
        "title": _months_ago(1).strftime("%B %Y"),
        "series": series,
        "income": round(income_cents / 100, 2),
    }
    # The data rides in the URL fragment as a named parameter (#d=...). Telegram
    # appends its own fragment parameters when opening the Mini App, so a named
    # one lets the page pick ours out and ignore the rest.
    url = f"{WEBAPP_URL}#d={urllib.parse.quote(json.dumps(payload))}"
    button = InlineKeyboardButton(text="Open chart", web_app=WebAppInfo(url=url))
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


@dp.message(Command("incomes"))
@usage("/incomes [m K]")
async def incomes(message: Message, command: CommandObject):
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
@usage("/alias <alias> <product>")
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
@usage("/rmalias <alias>")
async def rmalias(message: Message, command: CommandObject):
    alias_name = command.args.strip()
    user_id = message.from_user.id
    if db.remove_alias(user_id, alias_name):
        await reply(message, f"Removed alias {alias_name.lower()}")
    else:
        await reply(message, f"No alias {alias_name.lower()}")


@dp.message(Command("group"))
@usage("/group <name> <product,product,...>")
async def group(message: Message, command: CommandObject):
    # Fold the comma-separated products into the group `name`, collapsing them into
    # a single line on the balance. Run again with the same name to add more.
    parts = command.args.split()
    user_id = message.from_user.id

    name = parts[0]
    products = parts[1].split(",")
    product_ids = []
    member_names = []
    for product in products:
        existing = resolve_product(user_id, product)
        if existing is not None:
            product = existing
        product_ids.append(db.get_or_create_product(user_id, product))
        member_names.append(product)
    db.set_group(user_id, name, product_ids)
    members = ", ".join(pretty_product(n) for n in member_names)
    await reply(message, f"Group {name} -> {members}")


@dp.message(Command("ungroup"))
@usage("/ungroup <name>")
async def ungroup(message: Message, command: CommandObject):
    name = command.args.strip()
    if db.remove_group(message.from_user.id, name):
        await reply(message, f"Removed group {name}")
    else:
        await reply(message, f"No group {name}")


@dp.message(Command("groups"))
async def groups(message: Message, command: CommandObject):
    rows = db.list_groups(message.from_user.id)
    if not rows:
        await reply(message, "No groups.")
        return
    table_rows = [(r["name"], r["products"]) for r in rows]
    block = table_block("👥", "Groups", ("Group", "Products"), table_rows)
    await reply(message, block)


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
@usage("/pay [payer] <payee> <amount>")
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

    detail = payment_detail(day_str(), payment_id, payer_name, payee_name, amount)
    text = f"Added Payment\n{detail}"
    await broadcast(message, {message.from_user.id, payer_id, payee_id}, text)


@dp.callback_query(F.data.startswith("sched:"))
async def on_scheduled_action(query: CallbackQuery):
    # Handle the Add/Reject buttons on a due scheduled expense. The prompt goes
    # only to the creator, so this only fires for them. Popping the row first
    # keeps a double tap from recording it twice.
    _, action, scheduled_id = query.data.split(":", 2)
    item = db.pop_scheduled(scheduled_id)
    if item is None:
        await query.answer("Already handled.")
        return
    if action == "add":
        text, recipients = confirm_scheduled(item)
        body = f"<code>{html.escape(text)}</code>"
        await query.message.edit_text(body, parse_mode="HTML")
        for user_id in recipients - {query.from_user.id}:
            await query.bot.send_message(user_id, body, parse_mode="HTML")
    else:
        text = "Rejected Expense\n" + scheduled_detail(item)
        await query.message.edit_text(f"<code>{html.escape(text)}</code>", parse_mode="HTML")
    await query.answer()


# First word -> handler, for the bare-text shortcut below.
_COMMAND_GROUPS = (
    (start, ("start",)),
    (help_command, ("help",)),
    (register, ("register",)),
    (expense, ("expense", "exp")),
    (force_expense, ("forceexpense", "fexp")),
    (schedule_expense, ("scheduleexpense", "sexp")),
    (remove, ("remove", "rm")),
    (pay, ("pay",)),
    (list_operations, ("list",)),
    (detailed_list, ("detailedlist", "dlist")),
    (income, ("income", "inc")),
    (balance, ("balance",)),
    (incomes, ("incomes",)),
    (alias, ("alias",)),
    (rmalias, ("rmalias",)),
    (products, ("products",)),
    (group, ("group",)),
    (ungroup, ("ungroup",)),
    (groups, ("groups",)),
    (tablewidth, ("tablewidth",)),
)
TEXT_COMMANDS = {word: handler for handler, words in _COMMAND_GROUPS for word in words}


# Plain text with no leading slash runs /expense, unless its first word names a
# command, in which case that command runs with the rest as its arguments.
# Each command still produces its own usage hint on bad input.
@dp.message(F.text & ~F.text.startswith("/"))
async def text_shortcut(message: Message):
    first, _, rest = message.text.partition(" ")
    handler = TEXT_COMMANDS.get(first.lower())
    if handler is not None:
        command = CommandObject(command=first.lower(), args=rest.strip() or None)
    else:
        handler = expense
        command = CommandObject(command="exp", args=message.text.strip() or None)
    await handler(message, command=command)


async def dispatch_due_scheduled(bot):
    # Prompt the creator of every scheduled expense whose day has come. An item is
    # due from 09:00 on its due_date; before 9am today's items wait, but any item
    # from a past day (e.g. the bot was down) is sent right away.
    now = datetime.now()
    max_due = now.date() if now.hour >= 9 else now.date() - timedelta(days=1)
    for item in db.unnotified_scheduled(max_due.isoformat()):
        text, markup = scheduled_prompt(item)
        await bot.send_message(
            item["created_by"], text, parse_mode="HTML", reply_markup=markup
        )
        db.mark_scheduled_notified(item["seq"])


async def run_scheduler(bot):
    # Background loop that fires due scheduled-expense prompts once a minute.
    while True:
        try:
            await dispatch_due_scheduled(bot)
        except Exception:
            logging.exception("scheduler error")
        await asyncio.sleep(60)


async def main():
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    bot = Bot(token=os.environ["BOT_TOKEN"])
    asyncio.create_task(run_scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
