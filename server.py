"""
Telegram Stars backend for the case simulator.
Python 3.9+, standard library only (no pip install needed).

What it does
  POST /api/invoice  -> creates a Stars invoice link (createInvoiceLink)
  POST /api/status   -> {vip, bonus_pending} for the calling Telegram user
  POST /api/claim    -> grants the one-time +100 000 coins bonus (once)
  GET  /health
  + background long-polling of the bot: answers pre_checkout_query and
    records successful_payment in SQLite.

Every request is authenticated by validating Telegram's signed
initData (X-Init-Data header) - the user id is never taken from the client.

Environment variables
  BOT_TOKEN       required  token from @BotFather
  ALLOWED_ORIGIN  origin of the page that hosts the mini app
                  (e.g. https://yourname.github.io). Default "*"
  WEBAPP_URL      optional  mini app URL, used for the /start button
  SUPPORT_TEXT    optional  text shown by /paysupport
  PORT            default 8080
  DB_PATH         default payments.db
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
SUPPORT_TEXT = os.environ.get(
    "SUPPORT_TEXT",
    "По вопросам оплаты (двойное списание, VIP не активировался) опишите проблему "
    "одним сообщением и приложите время платежа — разработчик ответит и поможет.",
)
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "payments.db")

API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

VIP_PRICE = 10                    # Stars, must match the price shown in the app
DONATE_AMOUNTS = {1, 5, 10, 50}   # allowed donation sizes
INIT_DATA_MAX_AGE = 24 * 3600     # seconds

db_lock = threading.Lock()


# ----------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock, db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                   charge_id TEXT PRIMARY KEY,
                   user_id   INTEGER NOT NULL,
                   product   TEXT NOT NULL,
                   stars     INTEGER NOT NULL,
                   ts        INTEGER NOT NULL
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS vip (
                   user_id       INTEGER PRIMARY KEY,
                   paid_at       INTEGER NOT NULL,
                   bonus_claimed INTEGER NOT NULL DEFAULT 0
               )"""
        )


def record_payment(charge_id, user_id, product, stars):
    with db_lock, db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO payments VALUES (?,?,?,?,?)",
            (charge_id, user_id, product, stars, int(time.time())),
        )
        if cur.rowcount and product == "vip":
            c.execute(
                "INSERT OR IGNORE INTO vip (user_id, paid_at) VALUES (?,?)",
                (user_id, int(time.time())),
            )


def vip_row(user_id):
    with db_lock, db() as c:
        return c.execute("SELECT * FROM vip WHERE user_id=?", (user_id,)).fetchone()


def claim_bonus(user_id):
    """Atomic: returns True only for the single call that flips the flag."""
    with db_lock, db() as c:
        cur = c.execute(
            "UPDATE vip SET bonus_claimed=1 WHERE user_id=? AND bonus_claimed=0",
            (user_id,),
        )
        return cur.rowcount == 1


# ------------------------------------------------------------- Telegram API
def tg_call(method, params=None, timeout=40):
    req = urllib.request.Request(
        API + method,
        data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        data = json.loads(e.read() or b"{}")
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data}")
    return data["result"]


def validate_init_data(init_data):
    """Returns the Telegram user dict if the signature is valid, else None."""
    if not init_data:
        return None
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None

    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None


# ------------------------------------------------------------ bot (polling)
def parse_payload(payload):
    """payload format: '<product>:<user_id>:<stars>'"""
    try:
        product, uid, stars = payload.split(":")
        return product, int(uid), int(stars)
    except (ValueError, AttributeError):
        return None


def price_ok(product, stars):
    if product == "vip":
        return stars == VIP_PRICE
    if product == "donate":
        return stars in DONATE_AMOUNTS
    return False


def handle_pre_checkout(q):
    parsed = parse_payload(q.get("invoice_payload"))
    ok = bool(
        parsed
        and q.get("currency") == "XTR"
        and parsed[1] == q["from"]["id"]
        and price_ok(parsed[0], parsed[2])
        and q.get("total_amount") == parsed[2]
    )
    tg_call(
        "answerPreCheckoutQuery",
        {"pre_checkout_query_id": q["id"], "ok": ok,
         **({} if ok else {"error_message": "Не удалось проверить платёж"})},
    )


def handle_message(m):
    chat_id = m["chat"]["id"]

    sp = m.get("successful_payment")
    if sp:
        parsed = parse_payload(sp.get("invoice_payload"))
        if parsed and price_ok(parsed[0], parsed[2]) and parsed[1] == m["from"]["id"]:
            record_payment(
                sp["telegram_payment_charge_id"], parsed[1], parsed[0], parsed[2]
            )
            text = (
                "👑 VIP оплачен! Вернитесь в приложение — он активируется автоматически."
                if parsed[0] == "vip"
                else "🙏 Спасибо за поддержку!"
            )
            tg_call("sendMessage", {"chat_id": chat_id, "text": text})
        return

    text = (m.get("text") or "").strip()
    if text.startswith("/paysupport"):
        tg_call("sendMessage", {"chat_id": chat_id, "text": SUPPORT_TEXT})
    elif text.startswith("/start"):
        msg = {"chat_id": chat_id, "text": "Добро пожаловать в Case Simulator!"}
        if WEBAPP_URL:
            msg["reply_markup"] = {
                "inline_keyboard": [[{"text": "🎁 Играть", "web_app": {"url": WEBAPP_URL}}]]
            }
        tg_call("sendMessage", msg)


def poll_updates():
    try:
        # getUpdates and webhooks are mutually exclusive
        tg_call("deleteWebhook", {"drop_pending_updates": False})
    except Exception as e:
        print("deleteWebhook:", e)

    offset = 0
    while True:
        try:
            updates = tg_call(
                "getUpdates",
                {"offset": offset, "timeout": 30,
                 "allowed_updates": ["message", "pre_checkout_query"]},
                timeout=45,
            )
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    if "pre_checkout_query" in u:
                        handle_pre_checkout(u["pre_checkout_query"])
                    elif "message" in u:
                        handle_message(u["message"])
                except Exception as e:  # one bad update must not stop the loop
                    print("update error:", e)
        except Exception as e:
            print("poll error:", e)
            time.sleep(3)


# --------------------------------------------------------------- HTTP API
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        self._send(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

    def do_POST(self):
        user = validate_init_data(self.headers.get("X-Init-Data", ""))
        if not user:
            return self._send(401, {"error": "bad initData"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})

        uid = user["id"]

        try:
            if self.path == "/api/invoice":
                product = body.get("product")
                stars = VIP_PRICE if product == "vip" else int(body.get("stars") or 0)
                if not price_ok(product, stars):
                    return self._send(400, {"error": "bad product"})
                if product == "vip" and vip_row(uid):
                    return self._send(409, {"error": "already vip"})

                title, desc = (
                    ("VIP статус", "+100 000 монет и безлимитные бесплатные кейсы навсегда")
                    if product == "vip"
                    else ("Поддержка разработчика", "Добровольный донат. Спасибо!")
                )
                link = tg_call(
                    "createInvoiceLink",
                    {
                        "title": title,
                        "description": desc,
                        "payload": f"{product}:{uid}:{stars}",
                        "currency": "XTR",
                        "prices": [{"label": title, "amount": stars}],
                    },
                )
                return self._send(200, {"link": link})

            if self.path == "/api/status":
                row = vip_row(uid)
                return self._send(
                    200, {"vip": bool(row), "bonus_pending": bool(row and not row["bonus_claimed"])}
                )

            if self.path == "/api/claim":
                return self._send(200, {"granted": claim_bonus(uid)})

        except Exception as e:
            print("api error:", e)
            return self._send(500, {"error": "server error"})

        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=poll_updates, daemon=True).start()
    print(f"Stars backend listening on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
