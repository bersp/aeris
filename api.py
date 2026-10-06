import functools
import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import datetime, timedelta

from aiohttp import web

import db

# The API is only ever reached through the web server in front of it, which
# terminates TLS and owns the public hostname. Listening on loopback keeps the
# port itself off the internet, so this is deliberately not configurable.
HOST = "127.0.0.1"

# How long a signed initData stays usable. Telegram signs it once, when the Mini
# App opens, so a short life would break a page left open for a while; a day
# bounds how long a captured one is worth anything.
MAX_AUTH_AGE = timedelta(days=1)

# How many months the chart will overlay. The old limit was six, set by what fit
# in a button URL; now that the page fetches its data, the only thing being
# protected is how many lines stay readable on a phone.
MAX_CHART_MONTHS = 12


def verify_init_data(init_data, bot_token):
    # The Telegram user id behind a Mini App's initData, or None if it was not
    # signed by this bot or is too old. Telegram signs every field except `hash`,
    # sorted by key and joined with newlines, using HMAC("WebAppData", bot_token)
    # as the key. This check is the only thing between the API and the open
    # internet, which is why it fails closed on anything unexpected.
    try:
        pairs = urllib.parse.parse_qsl(init_data, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        return None
    fields = dict(pairs)
    received = fields.get("hash")
    if not received:
        return None
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs) if key != "hash")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        signed_at = datetime.fromtimestamp(int(fields["auth_date"]))
        user_id = json.loads(fields["user"])["id"]
    except (KeyError, ValueError, TypeError):
        return None
    if datetime.now() - signed_at > MAX_AUTH_AGE:
        return None
    return user_id


def authenticated(handler):
    # Runs the handler only for a verified Telegram user, passing the user id read
    # from the signature. Nothing in the path or the query string is trusted to
    # say who is asking, so no one can read another user's operations by editing
    # the request. initData travels in a header rather than the query string so
    # the signature never reaches the web server's access log.
    @functools.wraps(handler)
    async def wrapper(request):
        user_id = verify_init_data(
            request.headers.get("X-Telegram-Init-Data", ""), request.app["bot_token"]
        )
        allowed = request.app["allowed_user_ids"]
        if user_id is None or (allowed and user_id not in allowed):
            return web.json_response({"error": "unauthorized"}, status=401)
        if not db.is_registered(user_id):
            return web.json_response({"error": "not registered"}, status=403)
        return await handler(request, user_id)

    return wrapper


def wallet_of(request, user_id):
    # The wallet a request asks about, or the user's active one when it names
    # none. Every figure the API serves belongs to a single wallet. A page left
    # open on a wallet that has since been removed gets a 404 rather than a
    # crash.
    wallet_id = request.query.get("wallet")
    if not wallet_id:
        return db.active_wallet(user_id)
    try:
        wallet = db.get_wallet(int(wallet_id))
    except ValueError:
        wallet = None
    if wallet is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "no such wallet"}), content_type="application/json"
        )
    return wallet


@authenticated
async def meta(request, user_id):
    # What the page needs before it can ask for anything: who is looking, which
    # wallet it is looking at and which others there are, the range of days worth
    # offering, and what is still coming. Scheduled expenses are due in the
    # future, so they belong to no window in particular and ride here rather than
    # in a dated range.
    me = db.get_name(user_id)
    wallet = wallet_of(request, user_id)
    scheduled = [
        {
            "id": item["id"],
            "date": item["due_date"],
            "product": item["product"],
            "description": item["description"],
            "price": item["price"],
            "share": (
                round(item["price"] / len(item["consumers"]))
                if me in item["consumers"]
                else 0
            ),
            "payer": item["payer"],
            "consumers": item["consumers"],
            "added_by": item["added_by"],
            "plan": (
                f"{item['plan_index']}/{item['plan_total']}" if item["plan_id"] else None
            ),
        }
        for item in db.list_scheduled(user_id, wallet["id"])
    ]
    return web.json_response(
        {
            "me": me,
            "wallet": dict(wallet),
            "wallets": [dict(w) for w in db.all_wallets()],
            "first": db.first_operation_date(user_id, wallet["id"]),
            "today": datetime.now().date().isoformat(),
            "scheduled": scheduled,
        }
    )


@authenticated
async def operations(request, user_id):
    # Every operation of a wallet the user takes part in between two days, both
    # inclusive, newest first.
    try:
        since = datetime.strptime(request.query["from"], "%Y-%m-%d")
        until = datetime.strptime(request.query["to"], "%Y-%m-%d") + timedelta(days=1)
    except (KeyError, ValueError):
        return web.json_response(
            {"error": "from and to must be YYYY-MM-DD"}, status=400
        )

    # Products the user has put in a group report under the group name, the way
    # /balance collapses them.
    groups = {}
    for row in db.list_groups(user_id):
        for product in row["products"].split(", "):
            groups[product] = row["name"]

    me = db.get_name(user_id)
    wallet = wallet_of(request, user_id)
    dated = []
    for op in db.recent_operations(
        user_id, wallet["id"], limit=None, since=since, until=until
    ):
        if op["kind"] == "payment":
            item = {
                "kind": "payment",
                "id": op["id"],
                "payer": op["buyer"],
                "payee": op["consumers"][0],
                "amount": op["price"],
            }
        else:
            # The user's share is what /balance sums: an equal split among the
            # consumers, and nothing at all when they only paid for other people.
            consumers = op["consumers"]
            item = {
                "kind": "expense",
                "id": op["id"],
                "product": op["product"],
                "group": groups.get(op["product"]),
                "description": op["description"],
                "price": op["price"],
                "share": round(op["price"] / len(consumers)) if me in consumers else 0,
                "payer": op["buyer"],
                "consumers": consumers,
                "added_by": op["added_by"],
            }
        dated.append((op["created_at"], item))

    for inc in db.recent_incomes(
        user_id, wallet["id"], limit=None, since=since, until=until
    ):
        dated.append(
            (
                inc["created_at"],
                {
                    "kind": "income",
                    "id": inc["id"],
                    "name": inc["description"],
                    "description": inc["note"],
                    "amount": inc["amount"],
                },
            )
        )

    dated.sort(key=lambda pair: pair[0], reverse=True)
    for created_at, item in dated:
        item["date"] = created_at[:10]

    return web.json_response(
        {
            "from": since.date().isoformat(),
            "to": (until - timedelta(days=1)).date().isoformat(),
            "operations": [item for _, item in dated],
        }
    )


@authenticated
async def chart(request, user_id):
    # The cumulative daily spending in a wallet the chart page draws: one series
    # per month, most recent first, plus the current month's income for its
    # reference line.
    try:
        months = min(max(int(request.query.get("months", 2)), 2), MAX_CHART_MONTHS)
    except ValueError:
        return web.json_response({"error": "months must be a number"}, status=400)

    wallet = wallet_of(request, user_id)
    today = datetime.now().date()
    series = []
    for index in range(months):
        start, end = db.months_ago(index + 1), db.months_ago(index)
        totals = {
            row["day"]: row["total"]
            for row in db.daily_expense_totals(user_id, wallet["id"], start, end)
        }
        # The current month stops at today; past months run to their last day.
        last = today if index == 0 else (end.date() - timedelta(days=1))
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

    income = sum(
        row["total"]
        for row in db.income_summary(
            user_id, wallet["id"], db.months_ago(1), db.months_ago(0)
        )
    )
    # The wallet goes in the title once there are several, since the lines
    # alone do not say which money they count.
    title = db.months_ago(1).strftime("%B %Y")
    if len(db.all_wallets()) > 1:
        title += f" · {wallet['name']}"
    return web.json_response(
        {
            "title": title,
            "symbol": wallet["symbol"],
            "series": series,
            "income": round(income / 100, 2),
        }
    )


async def start_web(bot_token, port, allowed_user_ids=()):
    # Serves the read-only API inside the bot's own event loop, so there is one
    # process and one systemd unit. Read-only on purpose: nothing here writes to
    # the database.
    app = web.Application()
    app["bot_token"] = bot_token
    app["allowed_user_ids"] = set(allowed_user_ids)
    app.add_routes([
        web.get("/meta", meta),
        web.get("/operations", operations),
        web.get("/chart", chart),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, port).start()
    logging.info("API listening on %s:%s", HOST, port)
