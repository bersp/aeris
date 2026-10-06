import asyncio
import calendar
import functools
import html
import logging
import math
import os
import re
import secrets
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv

import api
import db

load_dotenv()

dp = Dispatcher()

# Public HTTPS URL of the Mini App page (plot.html). A Telegram Web App button
# only opens HTTPS URLs, so this is required for the chart button in /balance.
WEBAPP_URL = os.environ.get("WEBAPP_URL")

# Public HTTPS URL of the expense browser Mini App (ui.html), opened by /ui, and
# the port its read-only API listens on. Unlike the chart, this page holds far too
# much data to fit in a URL, so it reads from that API instead. The API is bound
# to loopback and expects the web server serving ui.html to proxy it; setting
# UI_URL is what turns both halves on.
UI_URL = os.environ.get("UI_URL")
API_PORT = int(os.environ.get("API_PORT", 8081))

# Instant View documentation sent by /start and /help. Publish your own page
# from documentation.md with publish_telegraph.py and set its URL in .env; a
# bare Telegra.ph link opens as Instant View in Telegram.
HELP_URL = os.environ.get("HELP_URL", "https://telegra.ph/Aeris-07-11")

# Optional comma-separated Telegram user ids allowed to talk to the bot. When
# empty, anyone who finds the bot can register.
ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

# Commands available before registering, with or without the leading slash.
PUBLIC_COMMANDS = ("start", "help", "register")


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
        # Messages from outside the allowlist are ignored without a reply.
        if ALLOWED_USER_IDS and message.from_user.id not in ALLOWED_USER_IDS:
            return
        command = message.text.split(maxsplit=1)[0] if message.text else ""
        command = command.lstrip("/").lower()
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


def take_wallet(args):
    # Pull an "@name" wallet marker out of the arguments, wherever it was typed.
    # Returns the wallet it names (None when there is no marker) and the
    # arguments without it.
    match = re.search(r"(?<!\S)@(\S+)", args or "")
    if match is None:
        return None, args
    wallet = db.find_wallet(match.group(1))
    if wallet is None:
        raise InputError(f"No wallet named {match.group(1)}.")
    return wallet, args[: match.start()] + args[match.end() :]


def is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def _days_ago(days):
    start_day = datetime.now().date() - timedelta(days=days - 1)
    return datetime.combine(start_day, datetime.min.time())


def _add_months(d, k):
    # `d` shifted forward by `k` months, clamping the day to the target month's
    # last day (e.g. 31 Jan + 1 month -> 28 Feb).
    month_index = d.year * 12 + (d.month - 1) + k
    year, month = divmod(month_index, 12)
    last_day = calendar.monthrange(year, month + 1)[1]
    return d.replace(year=year, month=month + 1, day=min(d.day, last_day))


def parse_list_args(args, noun="operations", count_word="Last", default="m 1"):
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
        return None, db.months_ago(k), "This month" if k == 1 else f"Last {k} months"
    k = number or 1
    return None, _days_ago(k), "Today" if k == 1 else f"Last {k} days"


@dp.message(Command("start"))
async def start(message: Message, command: CommandObject = None):
    await message.answer(HELP_URL)


@dp.message(Command("help"))
async def help_command(message: Message, command: CommandObject = None):
    await message.answer(HELP_URL)


@dp.message(Command("register"))
@usage("/register <name>")
async def register(message: Message, command: CommandObject):
    name = command.args.strip()
    if not name:
        raise ValueError
    # Names are referenced in other commands, where they are split on whitespace,
    # listed with commas, and told apart from amounts and the "all" keyword.
    if len(name.split()) > 1 or "," in name or is_number(name) or name.lower() == "all":
        await reply(message, 'Name must be a single word, not a number and not "all".')
        return
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

    plan_id = db.new_id(datetime.combine(start, datetime.min.time()))
    dues = [
        _add_months(start, i) if unit == "m" else start + timedelta(days=i)
        for i in range(count)
    ]
    for i, due in enumerate(dues):
        db.add_scheduled_expense(
            message.from_user.id, e["payer_id"], e["price"], e["product_id"],
            e["consumer_ids"], due.isoformat(), e["wallet"]["id"], e["description"],
            plan_id=plan_id, plan_index=i + 1, plan_total=count,
        )

    lines = [
        f"Scheduled {count} installments of "
        f"{pretty_product(e['display_product'])} "
        f"({money(e['price'], e['wallet']['symbol'])} each)",
        f"Paid by {e['payer_name']} for {', '.join(e['consumer_names'])}",
    ]
    for i, due in enumerate(dues):
        lines.append(f"▸ {day_from_iso(due.isoformat())}   {i + 1}/{count}")
    await reply(message, "\n".join(lines))


class InputError(Exception):
    # A user-facing complaint that already reads well (e.g. "Not registered: X"),
    # as opposed to the malformed-input errors that /usage turns into a hint.
    pass


def parse_expense_input(sender_id, args, force, current=None):
    # Parse the shared "[date] <product[(note)]> <price> [payer] [beneficiaries]"
    # form used by /expense and /scheduleexpense, resolve the product and the
    # participants, and return them as a dict. Raises InputError for problems that
    # have their own message; lets malformed input raise so /usage handles it.
    #
    # `current` is an expense being rewritten by /modify. With it, every field is
    # optional and keeps the value it already has, so a note can be added without
    # restating the price or who it was for, and "@usd" alone moves it to a wallet.

    # Optional description in parentheses attached to the product, e.g.
    # "others(some note)". It may contain spaces, so pull it out before splitting.
    # An empty "()" clears a note that is already there.
    match = re.search(r"\(([^)]*)\)", args)
    if match:
        description = match.group(1).strip() or None
        args = args[: match.start()] + args[match.end() :]
    else:
        description = current["description"] if current else None

    # An "@wallet" marker files it in that wallet. Without one it goes to the
    # sender's active wallet, or stays where it is when rewriting. Taken after the
    # note, so an "@" inside the note is left alone.
    wallet, args = take_wallet(args)
    if wallet is None:
        wallet = db.get_wallet(current["wallet_id"]) if current else db.active_wallet(sender_id)
    parts = args.split()

    # Optional purchase date as the first argument (today if omitted, or the date
    # it already has when rewriting).
    when = parse_date(parts[0]) if parts else None
    if when is not None:
        parts = parts[1:]
    elif current is not None:
        when = current["when"]

    product = parts[0] if parts else None
    if product is not None and is_number(product):
        raise InputError("Product cannot be a number.")
    # The price sits after the product, so rewriting one means restating the other.
    if len(parts) > 1:
        price = parse_amount(parts[1])
    elif current is not None:
        price = current["price"]
    else:
        raise IndexError

    # /expense collapses the product to the sender's existing spelling (same name
    # ignoring case, or a one-letter typo); /forceexpense stores it exactly as
    # typed.
    # Either way a product not seen before is flagged with a trailing "*" in the
    # confirmation (display only, never stored). A product left out of a rewrite
    # keeps the very one it has: resolving its name again could move it onto an
    # alias's target or a differently cased product.
    if product is None:
        product_id = current["product_id"]
        display_product = current["product"]
    elif force:
        is_new = product not in db.user_products(sender_id)
        display_product = f"{product}*" if is_new else product
    else:
        existing = resolve_product(sender_id, product)
        is_new = existing is None
        if existing is not None:
            product = existing
        display_product = f"{product}*" if is_new else product

    if product is not None:
        product_id = db.get_or_create_product(sender_id, product)

    # Work out who pays and who consumes. Personal expense (no third field): the
    # sender pays and consumes. Shared: a single trailing field is the
    # beneficiaries with the sender as payer; two fields are "<payer>
    # <beneficiaries>". Beneficiaries are comma-separated names, or "all".
    if len(parts) < 3 and current is not None:
        payer_id = current["payer_id"]
        payer_name = current["payer"]
        consumer_ids = list(current["consumer_ids"])
        consumer_names = list(current["consumers"])
    elif len(parts) < 3:
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
        "wallet": wallet,
    }


def store_expense(creator_id, e, keep_id=None, plan=None):
    # Write a parsed expense, deferring it when its date is in the future: it is
    # stored as scheduled and confirmed on its day (see run_scheduler / the sched:
    # callback) instead of recorded now. `keep_id` rewrites an existing operation
    # in place, `plan` carries an installment's place in its series. Returns the
    # kind it ended up as and its id.
    when = e["when"]
    if when is not None and when.date() > datetime.now().date():
        plan_id, plan_index, plan_total = plan or (None, None, None)
        return "scheduled", db.add_scheduled_expense(
            creator_id, e["payer_id"], e["price"], e["product_id"], e["consumer_ids"],
            when.date().isoformat(), e["wallet"]["id"], e["description"], plan_id,
            plan_index, plan_total, scheduled_id=keep_id,
        )
    return "expense", db.add_expense(
        e["payer_id"], e["price"], e["product_id"], e["consumer_ids"], e["wallet"]["id"],
        when, e["description"], created_by=creator_id, expense_id=keep_id,
    )


async def record_expense(message, command, force):
    try:
        e = parse_expense_input(message.from_user.id, command.args, force)
    except InputError as error:
        await reply(message, str(error))
        return

    creator_id = message.from_user.id
    creator_name = db.get_name(creator_id)
    kind, operation_id = store_expense(creator_id, e)
    detail = expense_detail(
        day_str(e["when"]), operation_id, e["display_product"], e["price"],
        e["wallet"]["symbol"], e["description"], e["payer_name"], e["consumer_names"],
        creator_name,
    )
    if kind == "scheduled":
        await reply(message, f"Scheduled Expense\n{detail}")
        return
    recipients = {creator_id, e["payer_id"]} | set(e["consumer_ids"])
    await broadcast(message, recipients, f"Added Expense\n{detail}")


OPERATION_LABELS = {
    "expense": "Expense",
    "income": "Income",
    "payment": "Payment",
    "scheduled": "Scheduled Expense",
}


def operation_detail(item):
    # Any operation rendered in the /detailedlist style, whichever kind it is.
    if item["kind"] == "scheduled":
        return scheduled_detail(item, plan_label(item))
    if item["kind"] == "income":
        return income_detail(
            day_from_id(item["id"]), item["id"], item["description"], item["amount"],
            item["symbol"], item["note"],
        )
    if item["kind"] == "payment":
        return payment_detail(
            day_from_id(item["id"]), item["id"],
            item["payer"], item["payee"], item["amount"], item["symbol"],
        )
    return expense_detail(
        day_from_id(item["id"]), item["id"], item["product"], item["price"],
        item["symbol"], item["description"], item["payer"], item["consumers"],
        item["added_by"],
    )


def removal_recipients(item, remover_id):
    # Who hears about a removal. Scheduled expenses are created and rejected
    # privately by their creator, and an income is nobody else's business, so
    # those two only reply to the remover.
    if item["kind"] == "payment":
        return {remover_id, item["payer_id"], item["payee_id"]}
    if item["kind"] == "expense":
        return {remover_id, item["payer_id"]} | set(item["consumer_ids"])
    return {remover_id}


@dp.message(Command("remove", "rm"))
@usage("/rm <id suffix>")
async def remove(message: Message, command: CommandObject):
    # Only what the sender logged is reachable, so an entry someone else recorded
    # is theirs to remove. An id suffix can match several of their operations, and
    # only the most recent is picked, so nothing is deleted until they have seen
    # exactly which.
    suffix = command.args.strip()
    item = db.find_operation(message.from_user.id, suffix)
    if item is None:
        await reply(
            message, f"Nothing to remove: no entry you logged ends with {suffix}."
        )
        return
    target = f"{item['kind']}:{item['seq']}"
    buttons = [
        InlineKeyboardButton(text="Remove", callback_data=f"rm:yes:{target}"),
        InlineKeyboardButton(text="Keep", callback_data=f"rm:no:{target}"),
    ]
    text = f"Remove this {OPERATION_LABELS[item['kind']].lower()}?\n{operation_detail(item)}"
    await message.answer(
        f"<code>{html.escape(text)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


# Rewrites waiting on their confirmation button, keyed by who asked and which
# operation they are about to change. Kept in memory on purpose: a prompt that
# outlives a restart is not worth a table, and the callback says so if it is gone.
PENDING_MODIFICATIONS = {}


def operation_when(item):
    # The datetime an operation is dated by: its due date when scheduled, the
    # moment it was recorded otherwise.
    if item["kind"] == "scheduled":
        return datetime.fromisoformat(item["due_date"])
    return datetime.fromisoformat(item["created_at"])


def modified_detail(item, change, kind, operation_id):
    # What the operation will look like once the change is applied, rendered the
    # same way the real thing is.
    if kind == "income":
        return income_detail(
            day_str(change["when"]), operation_id, change["name"], change["amount"],
            change["wallet"]["symbol"], change["note"],
        )
    return expense_detail(
        day_str(change["when"]), operation_id, change["display_product"], change["price"],
        change["wallet"]["symbol"], change["description"], change["payer_name"],
        change["consumer_names"], item.get("added_by"),
    )


def resulting_kind(item, change):
    # A rewrite can move an expense across the line: dating it ahead defers it,
    # dating a deferred one back records it now.
    if item["kind"] == "income":
        return "income"
    when = change["when"]
    if when is not None and when.date() > datetime.now().date():
        return "scheduled"
    return "expense"


@dp.message(Command("modify", "mod"))
@usage("/mod <id suffix> [date] [product[(note)]] [price] [payer] [beneficiaries]")
async def modify(message: Message, command: CommandObject):
    # Rewrite an entry through the same parsing an /expense or /income goes
    # through, so aliases, typo collapsing and "all" behave identically. Whatever
    # is left out keeps the value it has, and nothing is written until confirmed.
    suffix, _, rest = command.args.strip().partition(" ")
    rest = rest.strip()
    if not suffix or not rest:
        raise ValueError

    sender_id = message.from_user.id
    item = db.find_operation(sender_id, suffix)
    if item is None:
        await reply(
            message, f"Nothing to modify: no entry you logged ends with {suffix}."
        )
        return
    if item["kind"] == "payment":
        await reply(message, "Payments cannot be modified. Remove it with /rm and pay again.")
        return

    current = dict(item, when=operation_when(item))
    try:
        if item["kind"] == "income":
            change = parse_income_input(sender_id, rest, current)
        else:
            change = parse_expense_input(sender_id, rest, False, current)
    except InputError as error:
        await reply(message, str(error))
        return

    PENDING_MODIFICATIONS[(sender_id, item["kind"], item["seq"])] = (item, change)
    kind = resulting_kind(item, change)
    after = modified_detail(item, change, kind, db.reid(item["id"], change["when"]))
    text = (
        f"Modify this {OPERATION_LABELS[item['kind']].lower()}?\n"
        f"{operation_detail(item)}\n\nbecomes\n\n{after}"
    )
    target = f"{item['kind']}:{item['seq']}"
    buttons = [
        InlineKeyboardButton(text="Modify", callback_data=f"mod:yes:{target}"),
        InlineKeyboardButton(text="Keep", callback_data=f"mod:no:{target}"),
    ]
    await message.answer(
        f"<code>{html.escape(text)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


@dp.callback_query(F.data.startswith("mod:"))
async def on_modify_action(query: CallbackQuery):
    # Apply a confirmed rewrite by deleting the old row and storing the new one
    # through the ordinary add path, keeping the id so the suffix people already
    # know still reaches it.
    _, action, kind, seq = query.data.split(":", 3)
    seq = int(seq)
    pending = PENDING_MODIFICATIONS.pop((query.from_user.id, kind, seq), None)
    if pending is None:
        await query.answer("Expired. Run /mod again.")
        return
    item, change = pending

    if action == "no":
        text = f"Kept\n{operation_detail(item)}"
        await query.message.edit_text(
            f"<code>{html.escape(text)}</code>", parse_mode="HTML"
        )
        await query.answer()
        return

    # The wallet it is moving to can be removed while the prompt waits, and
    # writing into it would leave an entry no wallet owns.
    if db.get_wallet(change["wallet"]["id"]) is None:
        await query.answer("That wallet was removed. Run /mod again.")
        return
    if db.delete_operation(query.from_user.id, kind, seq) is None:
        await query.answer("Already gone.")
        return

    keep_id = db.reid(item["id"], change["when"])
    creator_id = item.get("created_by") or query.from_user.id
    if kind == "income":
        new_kind = "income"
        db.add_income(
            creator_id, change["amount"], change["name"], change["wallet"]["id"],
            change["when"], income_id=keep_id, note=change["note"],
        )
    else:
        plan = (item.get("plan_id"), item.get("plan_index"), item.get("plan_total"))
        new_kind, keep_id = store_expense(creator_id, change, keep_id, plan)

    detail = modified_detail(item, change, new_kind, keep_id)
    body = f"<code>{html.escape(f'Modified {OPERATION_LABELS[new_kind]}')}\n{html.escape(detail)}</code>"
    await query.message.edit_text(body, parse_mode="HTML")
    # Everyone who was involved before or is involved now hears about it.
    recipients = removal_recipients(item, query.from_user.id)
    if new_kind == "expense":
        recipients |= {change["payer_id"], *change["consumer_ids"]}
    for user_id in recipients - {query.from_user.id}:
        await query.bot.send_message(user_id, body, parse_mode="HTML")
    await query.answer()


@dp.callback_query(F.data.startswith("rm:"))
async def on_remove_action(query: CallbackQuery):
    # Handle the Remove/Keep buttons. The lookup is scoped to whoever tapped, so
    # the ids in the button cannot be used to reach someone else's operations.
    _, action, kind, seq = query.data.split(":", 3)
    if action == "no":
        item = db.get_operation(query.from_user.id, kind, int(seq))
        text = "Kept." if item is None else f"Kept\n{operation_detail(item)}"
        await query.message.edit_text(
            f"<code>{html.escape(text)}</code>", parse_mode="HTML"
        )
        await query.answer()
        return

    item = db.delete_operation(query.from_user.id, kind, int(seq))
    if item is None:
        await query.answer("Already removed.")
        return
    text = f"Removed {OPERATION_LABELS[kind]}\n{operation_detail(item)}"
    body = f"<code>{html.escape(text)}</code>"
    await query.message.edit_text(body, parse_mode="HTML")
    for user_id in removal_recipients(item, query.from_user.id) - {query.from_user.id}:
        await query.bot.send_message(user_id, body, parse_mode="HTML")
    await query.answer()


# Past this many cents a float cannot hold the amount exactly, and the value is
# heading for an overflow on the way into SQLite's integer column.
MAX_CENTS = 2**53


def parse_amount(text):
    # A decimal amount typed by the user, stored as an integer number of cents.
    # Zero, negative, non-finite and absurd amounts are malformed input. The check
    # is on the cents rather than the amount because the multiplication is what
    # overflows: 1e308 is a finite amount and an infinite number of cents.
    cents = float(text) * 100
    if not math.isfinite(cents) or not 0 < cents <= MAX_CENTS:
        raise ValueError(f"bad amount: {text}")
    return round(cents)


def format_price(cents):
    return f"{cents / 100:.2f}".rstrip("0").rstrip(".")


def money(cents, symbol):
    # An amount with its wallet's symbol, which is how every reply tells a peso
    # from a dollar.
    return f"{symbol}{format_price(cents)}"


DEFAULT_TABLE_WIDTH = 30  # used until a user picks their own with /tablewidth
MIN_TABLE_WIDTH = 20  # below this the Product column can't hold its header
# Above this a table only wraps on a phone, and every row costs its width in
# characters against Telegram's 4096-character message limit.
MAX_TABLE_WIDTH = 60


def table_width(user_id):
    # Clamped on the way out too, so a width stored before these bounds existed
    # cannot blow up a message.
    stored = int(db.get_pref(user_id, "table_width", DEFAULT_TABLE_WIDTH))
    return max(MIN_TABLE_WIDTH, min(stored, MAX_TABLE_WIDTH))


def truncate(text, width):
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


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


def expense_detail(
    day, op_id, product, price, symbol, description, buyer, consumers, added_by=None
):
    # One expense rendered in the /detailedlist style: a header line with the day
    # and id, the product and price, an optional note, and who paid for whom. When
    # `added_by` is given it is appended to the participants line so the reader
    # knows who logged it.
    lines = [
        f"▸ {day}   #{op_id}",
        f"- {pretty_product(product)} — {money(price, symbol)}",
    ]
    if description:
        lines.append(f"- {description}")
    line = f"- {buyer} → {', '.join(consumers)}"
    if added_by:
        line += f"  (by {added_by})"
    lines.append(line)
    return "\n".join(lines)


def income_detail(day, op_id, name, amount, symbol, note=None):
    # An income in the same style as expense_detail: header line, then the income
    # name and amount, then its note when it has one.
    lines = [f"▸ {day}   #{op_id}", f"- {pretty_product(name)} — {money(amount, symbol)}"]
    if note:
        lines.append(f"- {note}")
    return "\n".join(lines)


def payment_detail(day, op_id, payer_name, payee_name, amount, symbol):
    # A payment in the same style: header line plus who paid whom and how much.
    return f"▸ {day}   #{op_id}\n- {payer_name} → {payee_name} — {money(amount, symbol)}"


def day_str(when=None):
    return (when or datetime.now()).strftime("%d/%m/%y")


def day_from_id(op_id):
    # The dd/mm/yy an operation was dated, read back from its id, which begins
    # with the date as yymmdd (see db.new_id).
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
    # is appended after the id on the header line (used by /scheduled to flag
    # simple vs installment).
    op_id = item["id"]
    if marker:
        op_id = f"{op_id}   {marker}"
    return expense_detail(
        day_from_iso(item["due_date"]), op_id, item["product"], item["price"],
        item["symbol"], item["description"], item["payer"], item["consumers"],
        item["added_by"],
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
        item["wallet_id"], when, item["description"], created_by=item["created_by"],
    )
    detail = expense_detail(
        day_str(when), expense_id, item["product"], item["price"], item["symbol"],
        item["description"], item["payer"], item["consumers"], item["added_by"],
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


def month_block(user_id, emoji, label, name_header, summary, name_key, symbol):
    # One month of an expense or income summary: a table with each entry's total
    # and its share of the month, closed by the month total. `summary` rows carry
    # their name under `name_key` and their amount under "total".
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
    rows = [
        (product_cell(s[name_key], None, name_w), total)
        for s, total in zip(summary, totals)
    ]
    block = table_block(emoji, label, (name_header, "Total"), rows, [name_w, total_w])
    return f"{block}\n\nTotal: {money(month_total, symbol)}"


def code(text):
    return f"<code>{html.escape(text)}</code>"


def list_report(user_id, wallet, args):
    limit, since, label = parse_list_args(args)
    operations = db.recent_operations(user_id, wallet["id"], limit, since)
    if not operations:
        return code("No operations."), []
    width = table_width(user_id)
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
    return code(table_block("📋", label, ("Day", "Product", "Price"), rows, widths)), []


def detailed_list_report(user_id, wallet, args):
    limit, since, label = parse_list_args(args)
    operations = db.recent_operations(user_id, wallet["id"], limit, since)
    if not operations:
        return code("No operations."), []
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
                wallet["symbol"],
                op["description"],
                op["buyer"],
                op["consumers"],
                op["added_by"],
            )
        )
    body = "\n\n".join(blocks)
    return code(f"📖 {label}\n\n{body}"), []


def detailed_incomes_report(user_id, wallet, args):
    limit, since, label = parse_list_args(args, noun="incomes")
    incomes = db.recent_incomes(user_id, wallet["id"], limit, since)
    if not incomes:
        return code("No incomes."), []
    blocks = [
        income_detail(day_from_id(inc["id"]), inc["id"], inc["description"],
                      inc["amount"], wallet["symbol"], inc["note"])
        for inc in incomes
    ]
    body = "\n\n".join(blocks)
    return code(f"📖 {label}\n\n{body}"), []


def scheduled_report(user_id, wallet, args):
    # Every upcoming expense the user is part of, one-off deferrals and
    # installments alike, each flagged as "simple" or "installment K/N".
    items = db.list_scheduled(user_id, wallet["id"])
    if not items:
        return code("No scheduled expenses."), []
    blocks = [scheduled_detail(item, plan_label(item)) for item in items]
    return code("📅 Scheduled\n\n" + "\n\n".join(blocks)), []


def parse_balance_months(args):
    # /balance only takes a month count: "m K" or "m" (1). Anything else falls
    # back to the current month.
    parts = (args or "").split()
    if parts and parts[0] == "m":
        return int(parts[1]) if len(parts) > 1 else 1
    return 1


def balance_report(user_id, wallet, args):
    months = parse_balance_months(args)
    rows = db.balance(user_id, wallet["id"])
    cash = db.cash_balance(user_id, wallet["id"])
    net = sum(r["net"] for r in rows)
    symbol = wallet["symbol"]

    # Sections separated by a blank line: one expense table per month, then
    # debts, then the cash summary. Dynamic text is escaped per section so the
    # <b>/<code> tags below pass through untouched.
    sections = []

    # One table per month, oldest first; each product's share is over that
    # month's own total.
    for i in range(months, 0, -1):
        start, end = db.months_ago(i), db.months_ago(i - 1)
        summary = db.expense_summary(user_id, wallet["id"], start, end)
        if not summary:
            continue
        block = month_block(
            user_id, "🧾", start.strftime("%B %Y"), "Product", summary, "product", symbol
        )
        sections.append(html.escape(block))

    if rows:
        debts = []
        for r in rows:
            if r["net"] > 0:
                debts.append(f"🤝 {r['name']} owes you {money(r['net'], symbol)}")
            else:
                debts.append(f"🤝 You owe {r['name']} {money(-r['net'], symbol)}")
        sections.append(html.escape("\n".join(debts)))

    sections.append(
        f"<b>{html.escape(f'💵 Cash: {money(cash, symbol)}')}</b>\n"
        f"<b>{html.escape(f'💰 After debts: {money(cash + net, symbol)}')}</b>"
    )

    body = "\n\n".join(sections)
    return f"<code>{body}</code>", chart_buttons(months, wallet["id"])


def chart_buttons(months, wallet_id):
    # The "Open chart" inline button, as keyboard rows. By default the page
    # overlays the current month and the previous one for comparison; a month
    # count (from "/balance m K") overlays that many. Only that count and the
    # wallet travel in the URL: the page fetches the figures themselves from the
    # API, the same way ui.html does. No rows when plotting is not configured (no
    # WEBAPP_URL).
    if not WEBAPP_URL:
        return []
    # At least two months, so the current one has something to compare against.
    url = f"{WEBAPP_URL}#m={max(months, 2)}&w={wallet_id}"
    return [[InlineKeyboardButton(text="Open chart", web_app=WebAppInfo(url=url))]]


def incomes_report(user_id, wallet, args):
    months = parse_balance_months(args)
    sections = []

    # One table per month, oldest first; each income's share is over that
    # month's own total.
    for i in range(months, 0, -1):
        start, end = db.months_ago(i), db.months_ago(i - 1)
        summary = db.income_summary(user_id, wallet["id"], start, end)
        if not summary:
            continue
        block = month_block(
            user_id, "📈", start.strftime("%B %Y"), "Income", summary, "description",
            wallet["symbol"],
        )
        sections.append(block)

    if not sections:
        return code("No incomes."), []
    return code("\n\n".join(sections)), []


# The reports that show one wallet at a time. Each takes the user, the wallet and
# the command's arguments, and returns its HTML and any keyboard rows of its own.
REPORTS = {
    "list": list_report,
    "dlist": detailed_list_report,
    "dincomes": detailed_incomes_report,
    "scheduled": scheduled_report,
    "balance": balance_report,
    "incomes": incomes_report,
}


def wallet_buttons(report, wallet_id, args):
    # Keyboard rows with one button per wallet under a report, re-rendering it in
    # place for that wallet; the one on show is marked. None with a single wallet, where there is
    # nothing to switch to. The arguments ride along so a "/balance m 3" stays
    # three months deep. Every report reads at most two of them, and Telegram
    # caps a button's data at 64 bytes, so anything past those two is dropped and
    # a pair that still does not fit falls back to the report's default.
    wallets = db.all_wallets()
    if len(wallets) < 2:
        return None
    args = " ".join((args or "").split()[:2])
    if len(f"wal:{report}:{wallets[-1]['id']}:{args}".encode()) > 64:
        args = ""
    buttons = [
        InlineKeyboardButton(
            text=("• " if w["id"] == wallet_id else "") + w["name"],
            callback_data=f"wal:{report}:{w['id']}:{args}",
        )
        for w in wallets
    ]
    # Telegram takes at most eight buttons in a row; four keep the names legible.
    return [buttons[i : i + 4] for i in range(0, len(buttons), 4)]


def render_report(user_id, report, wallet, args):
    text, rows = REPORTS[report](user_id, wallet, args)
    switch = wallet_buttons(report, wallet["id"], args)
    if switch:
        rows = rows + switch
    return text, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def show_report(message, report, args):
    # Send a report for the wallet an "@name" in the arguments names, or the
    # sender's active one.
    user_id = message.from_user.id
    try:
        wallet, args = take_wallet(args)
    except InputError as error:
        await reply(message, str(error))
        return
    wallet = wallet or db.active_wallet(user_id)
    text, markup = render_report(user_id, report, wallet, args)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data.startswith("wal:"))
async def on_wallet_switch(query: CallbackQuery):
    _, report, wallet_id, args = query.data.split(":", 3)
    wallet = db.get_wallet(int(wallet_id))
    if wallet is None:
        await query.answer("That wallet was removed.")
        return
    text, markup = render_report(query.from_user.id, report, wallet, args or None)
    # Tapping the wallet already on show changes nothing, which Telegram refuses
    # to edit.
    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise
    await query.answer()


@dp.message(Command("list"))
@usage("/list [n N | m K | d K] [@wallet]")
async def list_operations(message: Message, command: CommandObject):
    await show_report(message, "list", command.args)


@dp.message(Command("detailedlist", "dlist"))
@usage("/dlist [n N | m K | d K] [@wallet]")
async def detailed_list(message: Message, command: CommandObject):
    await show_report(message, "dlist", command.args)


@dp.message(Command("detailedincomes", "dincomes"))
@usage("/dincomes [n N | m K | d K] [@wallet]")
async def detailed_incomes(message: Message, command: CommandObject):
    await show_report(message, "dincomes", command.args)


@dp.message(Command("scheduled"))
@usage("/scheduled [@wallet]")
async def scheduled(message: Message, command: CommandObject = None):
    await show_report(message, "scheduled", command.args if command else None)


def parse_income_input(sender_id, args, current=None):
    # Parse the "[date] <name[(note)]> <amount>" form used by /income, and by
    # /modify when rewriting one. As with expenses, `current` makes every field
    # optional, keeping the value it already has.

    # The note may contain spaces, so pull it out before splitting, exactly as
    # parse_expense_input does. An empty "()" clears one that is already there.
    match = re.search(r"\(([^)]*)\)", args)
    if match:
        note = match.group(1).strip() or None
        args = args[: match.start()] + args[match.end() :]
    else:
        note = current["note"] if current else None

    # The wallet works as it does for an expense.
    wallet, args = take_wallet(args)
    if wallet is None:
        wallet = db.get_wallet(current["wallet_id"]) if current else db.active_wallet(sender_id)
    parts = args.split()

    # Optional date as the first argument (today if omitted).
    when = parse_date(parts[0]) if parts else None
    if when is not None:
        parts = parts[1:]
    elif current is not None:
        when = current["when"]

    name = parts[0] if parts else current["description"]
    if is_number(name):
        raise InputError("Name cannot be a number.")
    if len(parts) > 1:
        amount = parse_amount(parts[1])
    elif current is not None:
        amount = current["amount"]
    else:
        raise IndexError
    return {"when": when, "name": name, "amount": amount, "note": note, "wallet": wallet}


@dp.message(Command("income", "inc"))
@usage("/inc [date] <name> <amount> [@wallet]")
async def income(message: Message, command: CommandObject):
    try:
        i = parse_income_input(message.from_user.id, command.args)
    except InputError as error:
        await reply(message, str(error))
        return
    income_id = db.add_income(
        message.from_user.id, i["amount"], i["name"], i["wallet"]["id"], i["when"],
        note=i["note"],
    )
    detail = income_detail(
        day_str(i["when"]), income_id, i["name"], i["amount"], i["wallet"]["symbol"],
        i["note"],
    )
    await reply(message, f"Added Income\n{detail}")


@dp.message(Command("balance"))
@usage("/balance [m K] [@wallet]")
async def balance(message: Message, command: CommandObject):
    await show_report(message, "balance", command.args)


@dp.message(Command("ui"))
async def ui(message: Message, command: CommandObject = None):
    # Opens the graphical side of the bot: the same operations the text commands
    # print, but listed, expandable and filterable by month or by an arbitrary
    # range. The page carries no data of its own; it reads the API. Unlike the
    # chart button, which rides on a message that stands on its own, this command
    # is nothing but the button, so an unconfigured URL has to say so rather than
    # send an empty reply.
    if not UI_URL:
        await reply(message, "Web UI is not configured.")
        return
    button = InlineKeyboardButton(text="Open UI", web_app=WebAppInfo(url=UI_URL))
    await message.answer(
        f"<code>{html.escape('🌐 Operations')}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )


@dp.message(Command("incomes"))
@usage("/incomes [m K] [@wallet]")
async def incomes(message: Message, command: CommandObject):
    await show_report(message, "incomes", command.args)


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


# The three things a name can belong to. Users rename themselves with /register
# and an alias is re-pointed with /alias, so neither needs a kind here.
RENAME_KINDS = ("product", "income", "group")

# Renames waiting on their confirmation button, keyed by who asked and a token
# short enough to travel in the button. In memory for the same reason
# PENDING_MODIFICATIONS is.
PENDING_RENAMES = {}


def rename_plan(user_id, kind, old, new):
    # What a rename is about to do, or None when nothing carries the old name.
    # "moved" counts the entries taking the new name and "merged" the ones already
    # under it, since renaming onto a name in use folds the two together. Nothing
    # merges with itself: changing only how a name is written leaves one name.
    restyle = old.lower() == new.lower()
    if kind == "income":
        usage = db.income_usage(user_id, old)
        if not usage["n"]:
            return None
        merged = 0 if restyle else db.income_usage(user_id, new)["n"]
        plan = {"old": usage["name"], "moved": usage["n"], "merged": merged}
    elif kind == "group":
        usage = db.group_usage(user_id, old)
        if not usage["n"]:
            return None
        merged = 0 if restyle else db.group_usage(user_id, new)["n"]
        plan = {"old": usage["name"], "moved": usage["n"], "merged": merged}
    else:
        # A product that is spelled out wins over the aliases and typo collapsing
        # an /expense would apply, so a name that exists can always be renamed:
        # an alias is free to point somewhere else, and this is the one command
        # that means the name itself rather than what it usually stands for.
        # The new name is taken literally either way: it only meets an existing
        # product when it is spelled like one, which is when the two have to merge.
        product = db.find_product(user_id, old)
        if product is None:
            existing = resolve_product(user_id, old)
            product = db.find_product(user_id, existing) if existing else None
        if product is None:
            return None
        # Both spellings can land on the same row (a restyle, or an old name that
        # only reached it through a typo), and a product cannot be merged into
        # itself: that would move its expenses onto itself and then delete it.
        target = db.find_product(user_id, new)
        if target is not None and target["id"] == product["id"]:
            target = None
        expenses, scheduled = db.product_usage(product["id"])
        plan = {
            "old": product["name"],
            "moved": expenses,
            "scheduled": scheduled,
            "merged": db.product_usage(target["id"])[0] if target else 0,
            "product_id": product["id"],
            "target_id": target["id"] if target else None,
        }
    return dict(plan, kind=kind, new=new)


RENAME_NOUNS = {"product": "expense", "income": "income", "group": "product"}


def rename_detail(plan):
    # A rename spelled out: what becomes what, how much it touches, and what it
    # is about to be merged with when the new name is already in use.
    noun = RENAME_NOUNS[plan["kind"]]
    new = pretty_product(plan["new"])
    lines = [f"- {pretty_product(plan['old'])} -> {new}"]
    counted = f"- {plural(plan['moved'], noun)}"
    if plan.get("scheduled"):
        counted += f" and {plural(plan['scheduled'], 'scheduled expense')}"
    lines.append(counted)
    if plan["merged"]:
        lines.append(f"- {new} already has {plural(plan['merged'], noun)}: merging")
    return "\n".join(lines)


@dp.message(Command("rename"))
@usage("/rename [product|income|group] <old> <new>")
async def rename(message: Message, command: CommandObject):
    # Rewrite a name wherever it is used: a product on every expense that carries
    # it, an income source, or a group. Renaming onto a name already in use merges
    # the two, so nothing is written until the counts have been confirmed.
    parts = command.args.split()
    if len(parts) == 3:
        kind, old, new = parts
        if kind not in RENAME_KINDS:
            raise ValueError
    else:
        kind = "product"
        old, new = parts  # anything but a pair is a usage error

    # The new name is typed back as a single token in later commands, where
    # parentheses open a note and a number reads as an amount.
    if is_number(new) or "(" in new or ")" in new:
        await reply(message, "New name cannot be a number or contain parentheses.")
        return

    user_id = message.from_user.id
    plan = rename_plan(user_id, kind, old, new)
    if plan is None:
        await reply(message, f"No {kind} named {pretty_product(old)}.")
        return
    if plan["old"] == new:
        await reply(message, f"{pretty_product(new)} is already its name.")
        return

    token = secrets.token_hex(3)
    PENDING_RENAMES[(user_id, token)] = plan
    text = f"Rename this {kind}?\n{rename_detail(plan)}"
    buttons = [
        InlineKeyboardButton(text="Rename", callback_data=f"ren:yes:{token}"),
        InlineKeyboardButton(text="Keep", callback_data=f"ren:no:{token}"),
    ]
    await message.answer(
        f"<code>{html.escape(text)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


@dp.callback_query(F.data.startswith("ren:"))
async def on_rename_action(query: CallbackQuery):
    # Apply a confirmed rename. The pending plan is keyed by who asked, so the
    # token in the button only resolves one for them.
    _, action, token = query.data.split(":", 2)
    user_id = query.from_user.id
    plan = PENDING_RENAMES.pop((user_id, token), None)
    if plan is None:
        await query.answer("Expired. Run /rename again.")
        return

    if action == "no":
        text = f"Kept {pretty_product(plan['old'])}"
    else:
        if plan["kind"] == "income":
            db.rename_income(user_id, plan["old"], plan["new"])
        elif plan["kind"] == "group":
            db.rename_group(user_id, plan["old"], plan["new"])
        else:
            db.rename_product(
                user_id, plan["product_id"], plan["new"], plan["target_id"]
            )
        text = f"Renamed {plan['kind']}\n{rename_detail(plan)}"
    await query.message.edit_text(
        f"<code>{html.escape(text)}</code>", parse_mode="HTML"
    )
    await query.answer()


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
    await reply(message, f"Group {pretty_product(name)} -> {members}")


@dp.message(Command("ungroup"))
@usage("/ungroup <name>")
async def ungroup(message: Message, command: CommandObject):
    name = command.args.strip()
    if db.remove_group(message.from_user.id, name):
        await reply(message, f"Removed group {pretty_product(name)}")
    else:
        await reply(message, f"No group {pretty_product(name)}")


@dp.message(Command("groups"))
async def groups(message: Message, command: CommandObject):
    rows = db.list_groups(message.from_user.id)
    if not rows:
        await reply(message, "No groups.")
        return
    # Both columns are product-style names, so their underscores read as spaces
    # here exactly as they do in every table.
    table_rows = [(pretty_product(r["name"]), pretty_product(r["products"])) for r in rows]
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
    if width > MAX_TABLE_WIDTH:
        await reply(message, f"Width must be at most {MAX_TABLE_WIDTH}")
        return
    db.set_pref(message.from_user.id, "table_width", width)
    await reply(message, f"Table width set to {width}")


# The words /wallet takes before a wallet's name. A wallet named like one could
# never be switched to, so they are not allowed as names.
WALLET_ACTIONS = ("new", "rm")

# Wallet removals waiting on their confirmation button, keyed by who asked and
# the wallet. In memory for the same reason PENDING_MODIFICATIONS is.
PENDING_WALLET_REMOVALS = set()


def wallet_contents(usage):
    # "3 expenses, 1 income and 2 scheduled expenses", from db.wallet_usage.
    parts = [plural(n, OPERATION_LABELS[kind].lower()) for kind, n in usage.items() if n]
    if not parts:
        return "no entries"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


@dp.message(Command("wallet"))
@usage("/wallet [<name> | new <name> <symbol> | rm <name>]")
async def wallet(message: Message, command: CommandObject):
    # "<name>" switches the sender's active wallet, the one everything they log
    # goes to unless it says "@other"; "new <name> <symbol>" creates one for
    # everybody and "rm <name>" deletes one, after a confirmation. On its own it
    # shows the wallets, like /wallets.
    parts = (command.args or "").split()
    user_id = message.from_user.id
    if not parts:
        await wallets(message, command)
        return

    if parts[0] == "rm":
        await remove_wallet(message, parts[1])
        return

    if parts[0] == "new":
        name, symbol = parts[1], parts[2]
        if name.lower() in WALLET_ACTIONS:
            await reply(message, f"A wallet cannot be called {name}.")
            return
        # Parentheses open a note in the commands an "@name" is typed into, so a
        # name with them could never be reached that way.
        if "(" in name or ")" in name:
            await reply(message, "Wallet name cannot contain parentheses.")
            return
        if db.find_wallet(name) is not None:
            await reply(message, f"Wallet {name} already exists.")
            return
        db.add_wallet(name, symbol)
        await reply(
            message,
            f"Created wallet {name} ({symbol})\n"
            f"Switch to it with /wallet {name}, or add @{name} to a single entry.",
        )
        return

    target = db.find_wallet(parts[0])
    if target is None:
        await reply(message, f"No wallet named {parts[0]}.")
        return
    db.set_pref(user_id, "wallet", target["id"])
    await reply(
        message,
        f"Active wallet: {target['name']} ({target['symbol']})\n"
        "Everything you log goes here until you switch again.",
    )


async def remove_wallet(message, name):
    # Wallets are shared, so removing one takes every user's entries in it. The
    # prompt says how many before anything is touched, like /rm does.
    target = db.find_wallet(name)
    if target is None:
        await reply(message, f"No wallet named {name}.")
        return
    if target["id"] == db.DEFAULT_WALLET:
        await reply(message, f"{target['name']} is the default wallet and cannot be removed.")
        return

    PENDING_WALLET_REMOVALS.add((message.from_user.id, target["id"]))
    usage = db.wallet_usage(target["id"])
    text = (
        f"Remove wallet {target['name']} ({target['symbol']})?\n"
        f"- It holds {wallet_contents(usage)}"
    )
    if any(usage.values()):
        text += "\n- They are deleted for everyone and cannot be recovered"
    buttons = [
        InlineKeyboardButton(text="Remove", callback_data=f"wrm:yes:{target['id']}"),
        InlineKeyboardButton(text="Keep", callback_data=f"wrm:no:{target['id']}"),
    ]
    await message.answer(
        f"<code>{html.escape(text)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


@dp.callback_query(F.data.startswith("wrm:"))
async def on_remove_wallet_action(query: CallbackQuery):
    # Only a removal the tapper asked for themselves is honoured, so the wallet id
    # in the button is not enough on its own to delete one.
    _, action, wallet_id = query.data.split(":", 2)
    wallet_id = int(wallet_id)
    user_id = query.from_user.id
    if (user_id, wallet_id) not in PENDING_WALLET_REMOVALS:
        await query.answer("Expired. Run /wallet rm again.")
        return
    PENDING_WALLET_REMOVALS.discard((user_id, wallet_id))
    target = db.get_wallet(wallet_id)
    if target is None:
        await query.answer("Already removed.")
        return

    label = f"{target['name']} ({target['symbol']})"
    if action == "no":
        await query.message.edit_text(code(f"Kept wallet {label}"), parse_mode="HTML")
        await query.answer()
        return

    removed = wallet_contents(db.delete_wallet(wallet_id))
    await query.message.edit_text(
        code(f"Removed wallet {label}\n- With {removed}"), parse_mode="HTML"
    )
    # Everyone loses the wallet, entries and all, so everyone hears about it.
    notice = code(
        f"Removed wallet {label}\n- With {removed}\n- By {db.get_name(user_id)}"
    )
    # Someone who blocked the bot must not keep the rest from hearing, and the
    # wallet is gone by now either way.
    for other in db.all_users():
        if other["user_id"] == user_id:
            continue
        try:
            await query.bot.send_message(other["user_id"], notice, parse_mode="HTML")
        except Exception:
            logging.exception("could not tell %s about a removed wallet", other["user_id"])
    await query.answer()


@dp.message(Command("wallets"))
async def wallets(message: Message, command: CommandObject = None):
    # Every wallet side by side with the sender's cash in it, before and after
    # debts. Each stays in its own currency: nothing here is added up.
    user_id = message.from_user.id
    active = db.active_wallet(user_id)["id"]
    rows = []
    for w in db.all_wallets():
        cash = db.cash_balance(user_id, w["id"])
        net = sum(r["net"] for r in db.balance(user_id, w["id"]))
        name = ("• " if w["id"] == active else "") + w["name"]
        rows.append((name, money(cash, w["symbol"]), money(cash + net, w["symbol"])))
    block = table_block("👛", "Wallets", ("Wallet", "Cash", "After debts"), rows)
    await reply(message, f"{block}\n\nSwitch with /wallet <name>")


@dp.message(Command("pay"))
@usage("/pay [payer] <payee> <amount> [@wallet]")
async def pay(message: Message, command: CommandObject):
    try:
        wallet, args = take_wallet(command.args)
    except InputError as error:
        await reply(message, str(error))
        return
    wallet = wallet or db.active_wallet(message.from_user.id)
    parts = args.split()
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
    payment_id = db.add_payment(
        payer_id, payee_id, amount, wallet["id"], created_by=message.from_user.id
    )

    detail = payment_detail(
        day_str(), payment_id, payer_name, payee_name, amount, wallet["symbol"]
    )
    text = f"Added Payment\n{detail}"
    await broadcast(message, {message.from_user.id, payer_id, payee_id}, text)


@dp.callback_query(F.data.startswith("sched:"))
async def on_scheduled_action(query: CallbackQuery):
    # Handle the Add/Reject buttons on a due scheduled expense. The prompt goes
    # only to the creator, so this only fires for them. Popping the row first
    # keeps a double tap from recording it twice.
    _, action, scheduled_id = query.data.split(":", 2)
    item = db.pop_scheduled(scheduled_id, query.from_user.id)
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
    (modify, ("modify", "mod")),
    (pay, ("pay",)),
    (list_operations, ("list",)),
    (detailed_list, ("detailedlist", "dlist")),
    (scheduled, ("scheduled",)),
    (income, ("income", "inc")),
    (balance, ("balance",)),
    (ui, ("ui",)),
    (incomes, ("incomes",)),
    (detailed_incomes, ("detailedincomes", "dincomes")),
    (alias, ("alias",)),
    (rmalias, ("rmalias",)),
    (products, ("products",)),
    (group, ("group",)),
    (ungroup, ("ungroup",)),
    (groups, ("groups",)),
    (rename, ("rename",)),
    (tablewidth, ("tablewidth",)),
    (wallet, ("wallet",)),
    (wallets, ("wallets",)),
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
        # One undeliverable prompt (a creator who blocked the bot) must not hold
        # up the items behind it. It stays unnotified so a passing failure is
        # retried; a permanent one keeps complaining in the log until the expense
        # is removed, which beats dropping it silently.
        try:
            await bot.send_message(
                item["created_by"], text, parse_mode="HTML", reply_markup=markup
            )
        except Exception:
            logging.exception("could not prompt for scheduled %s", item["id"])
            continue
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
    # Both Mini App pages read their figures from the API, so it runs as soon as
    # either one is configured.
    if UI_URL or WEBAPP_URL:
        await api.start_web(bot.token, API_PORT, ALLOWED_USER_IDS)
    asyncio.create_task(run_scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
