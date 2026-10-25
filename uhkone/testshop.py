"""Loopback-only SQLite simulator of the observed public shop protocol.

This is independently implemented test software, not Enkora or an official
Urheiluhallit service. Public course facts remain intact; availability, accounts,
stock, balance and every transaction are explicitly simulated.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCENARIOS = frozenset({"normaali", "ruuhka", "varaus-vastaus-katoaa", "maksu-vastaus-katoaa", "viimeinen-paikka", "taynna",
                       "fastpay-kortti-puuttuu", "fastpay-hylatty", "ensisijainen-taynna",
                       "jono", "jono-vastaus-katoaa", "jono-taynna"})
PREFIX = "/webshop/reservations2/"
SESSION_COOKIE = "uhkone_test_session"


class _APIError(Exception):
    def __init__(self, message: str, status: int = 409, *, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def _int(value) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise ValueError("Odotettiin kokonaislukua")


def _price(row: dict) -> int:
    products = row.get("fare_products")
    if not isinstance(products, list) or not products or not isinstance(products[0], dict):
        raise ValueError("Testikurssilta puuttuu myyntituote")
    value = row.get("bundle_price") if row.get("bundle_price") is not None else row.get("price")
    if value is None:
        value = products[0].get("price")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
            raise ValueError("Kurssihinta ei ole kokonaisia senttejä")
        return int(amount)
    except InvalidOperation as exc:
        raise ValueError("Kurssihinta puuttuu") from exc


class TestShop:
    """Start with ``with TestShop(public_rows) as shop``; no external traffic."""
    __test__ = False

    def __init__(self, courses: list[dict], *, opens_in: float = 3,
                 scenario: str = "normaali", db_path: str = ":memory:"):
        if scenario not in SCENARIOS or isinstance(opens_in, bool) or not math.isfinite(opens_in) or opens_in < 0:
            raise ValueError("Tuntematon testitilanne tai virheellinen avausviive")
        if not isinstance(courses, list) or not courses:
            raise ValueError("Testikauppa tarvitsee vähintään yhden julkisen kurssitietueen")
        self._lock = threading.RLock()
        self._server = self._thread = None
        self.scenario = scenario
        self.opens_at = datetime.now(UTC) + timedelta(seconds=opens_in)
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS courses(id INTEGER PRIMARY KEY, raw TEXT NOT NULL,
                    capacity INTEGER NOT NULL, price INTEGER NOT NULL, product_id INTEGER NOT NULL,
                    queue_capacity INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL REFERENCES courses(id), child_id INTEGER NOT NULL,
                    price INTEGER NOT NULL, status INTEGER NOT NULL DEFAULT 7, is_paid INTEGER NOT NULL DEFAULT 0,
                    receipt_id INTEGER, queue_number INTEGER, UNIQUE(course_id,child_id));
                CREATE TABLE IF NOT EXISTS cart(item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id INTEGER NOT NULL UNIQUE REFERENCES reservations(id),
                    session_id TEXT NOT NULL DEFAULT 'legacy');
                CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY AUTOINCREMENT, total_amount INTEGER NOT NULL,
                    payment_type_id INTEGER NOT NULL DEFAULT 4, session_id TEXT NOT NULL DEFAULT 'legacy');
                CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    course_id INTEGER, child_id INTEGER);
            """)
            if "payment_type_id" not in {column["name"] for column in self._db.execute("PRAGMA table_info(receipts)")}:
                self._db.execute("ALTER TABLE receipts ADD COLUMN payment_type_id INTEGER NOT NULL DEFAULT 4")
            for table in ("cart", "receipts"):
                if "session_id" not in {column["name"] for column in self._db.execute(f"PRAGMA table_info({table})")}:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN session_id TEXT NOT NULL DEFAULT 'legacy'")
            if "queue_capacity" not in {column["name"] for column in self._db.execute("PRAGMA table_info(courses)")}:
                self._db.execute("ALTER TABLE courses ADD COLUMN queue_capacity INTEGER NOT NULL DEFAULT 0")
            if "queue_number" not in {column["name"] for column in self._db.execute("PRAGMA table_info(reservations)")}:
                self._db.execute("ALTER TABLE reservations ADD COLUMN queue_number INTEGER")
            seen = set()
            for row in courses:
                if not isinstance(row, dict):
                    raise ValueError("Kurssitietue ei ole objekti")  # noqa: TRY004 - Preserve the validation error contract.
                course_id = _int(row.get("reservation_event_group_id"))
                if course_id <= 0 or course_id in seen or not row.get("name") or not row.get("location"):
                    raise ValueError("Kurssin tunnus, nimi tai halli on virheellinen")
                seen.add(course_id)
                price, product_id = _price(row), _int(row["fare_products"][0].get("fare_product_id"))
                if product_id <= 0:
                    raise ValueError("Myyntituotteen tunnus on virheellinen")
                try:
                    capacity = max(1, _int(row.get("capacity", row.get("free", 3))))
                except ValueError:
                    capacity = 3
                capacity = 1 if scenario == "viimeinen-paikka" else 0 if scenario in {"taynna", "jono", "jono-vastaus-katoaa", "jono-taynna"} else capacity
                queue_capacity = 1 if scenario in {"jono", "jono-vastaus-katoaa"} else 0
                if scenario == "ensisijainen-taynna" and course_id == _int(courses[0]["reservation_event_group_id"]):
                    capacity = 0
                self._db.execute("INSERT OR IGNORE INTO courses(id,raw,capacity,price,product_id,queue_capacity) VALUES(?,?,?,?,?,?)",
                                 (course_id, json.dumps(row, ensure_ascii=False), capacity, price, product_id, queue_capacity))
            self.course_id = _int(courses[0]["reservation_event_group_id"])
            for key, value in {"balance": "100000", "opens_at": self.opens_at.isoformat(), "scenario": scenario,
                               "course_gets": "0", "lost_add": "0", "lost_payment": "0", "lost_queue": "0"}.items():
                self._db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, value))
            if self._meta("scenario") != scenario:
                raise ValueError("Tallennetun testitietokannan skenaariota ei voi vaihtaa")
            self.opens_at = datetime.fromisoformat(self._meta("opens_at"))
            if str(db_path) != ":memory:":
                os.chmod(db_path, 0o600)
        except Exception:
            self._db.close()
            raise

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("Testikauppaa ei ole käynnistetty")
        return f"http://127.0.0.1:{self._server.server_port}"

    def _meta(self, key):
        return self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    def _set(self, key, value):
        self._db.execute("UPDATE meta SET value=? WHERE key=?", (str(value), key))

    def _session(self, cookie_header):
        """Return one server-issued browser session; never adopt arbitrary cookies."""
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header or "")
            candidate = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""
        except CookieError:
            candidate = ""
        with self._lock:
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", candidate) and self._db.execute(
                    "SELECT 1 FROM sessions WHERE id=?", (candidate,)).fetchone():
                return candidate, False
            session_id = secrets.token_urlsafe(32)
            self._db.execute("INSERT INTO sessions(id) VALUES(?)", (session_id,))
            return session_id, True

    def __enter__(self):
        if self._server is not None:
            raise RuntimeError("Testikauppa on jo käynnissä")
        shop = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def reply(self, status, value, *, html=False, retry_after=None):
                body = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                if retry_after is not None:
                    self.send_header("Retry-After", str(retry_after))
                if self._new_session_id is not None:
                    self.send_header("Set-Cookie", f"{SESSION_COOKIE}={self._new_session_id}; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers()
                self.wfile.write(body)

            def handle_request(self):
                self._new_session_id = None
                try:
                    if self.client_address[0] != "127.0.0.1" or self.headers.get("Host") != urlsplit(shop.base_url).netloc:
                        raise _APIError("Vain paikallinen testikäyttö sallittu", 403)
                    if self.headers.get("Origin") not in (None, shop.base_url):
                        raise _APIError("Vieraan sivun testipyyntö estettiin", 403)
                    session_id, is_new = shop._session(self.headers.get("Cookie"))
                    self._new_session_id = session_id if is_new else None
                    path = urlsplit(self.path).path
                    if (self.command == "GET" and path in {"/", "/webshop", "/webshop/", "/webshop/reservations2", "/webshop/reservations2/"}) or (
                            self.command == "GET" and re.fullmatch(r"/webshop/reservations2/reservations/\d+/[\d-]+/[\d-]+/-/\d+/?", path)):
                        self.reply(200, Path(__file__).with_name("testshop.html").read_text(encoding="utf-8"), html=True)
                        return
                    body = None
                    if self.command == "POST":
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 1024 * 1024:
                            raise _APIError("Virheellinen testipyynnön koko", 400)
                        text = self.rfile.read(length).decode("utf-8")
                        body = json.loads(parse_qs(text)["content"][0]) if path.startswith("/webshop/call/") else json.loads(text)
                    result = shop._dispatch(self.command, path, body, session_id=session_id)
                    self.reply(200, result)
                except _APIError as exc:
                    self.reply(exc.status, {"result": False, "errors": [str(exc)], "testshop": True}, retry_after=exc.retry_after)
                except (ValueError, KeyError, TypeError, UnicodeError):
                    self.reply(400, {"result": False, "errors": ["Virheellinen testipyyntö"], "testshop": True})

            do_GET = do_POST = handle_request

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            assert self._thread is not None
            self._thread.join(timeout=2)
            self._server = None
        self._db.close()

    def serve_forever(self):
        if self._thread is None:
            raise RuntimeError("Käynnistä testikauppa with-lauseella")
        self._thread.join()

    def _course(self, stored):
        row = json.loads(stored["raw"])
        occupied = self._db.execute("SELECT count(*) FROM reservations WHERE course_id=? AND status IN(2,4,5,7)", (stored["id"],)).fetchone()[0]
        queued = self._db.execute("SELECT count(*) FROM reservations WHERE course_id=? AND status IN(1,3)", (stored["id"],)).fetchone()[0]
        free = max(0, stored["capacity"] - occupied)
        row["_test"] = {"simulated": True, "scenario": self.scenario, "original_availability": {
            key: deepcopy(row.get(key)) for key in ("free", "capacity", "is_reservable", "public_reservation_start", "queue_capacity", "queue", "free_queue")}}
        row.update(free=free, capacity=stored["capacity"], is_reservable=datetime.now(UTC) >= self.opens_at,
                   queue_capacity=stored["queue_capacity"], queue=queued, free_queue=max(0, stored["queue_capacity"] - queued),
                   public_reservation_start=self.opens_at.isoformat())
        return row

    def _cart(self, session_id):
        rows = self._db.execute("SELECT c.item_id,r.*,p.product_id FROM cart c JOIN reservations r ON r.id=c.reservation_id JOIN courses p ON p.id=r.course_id WHERE c.session_id=?", (session_id,)).fetchall()
        return [{"item_id": row["item_id"], "fare_product_id": row["product_id"], "quantity": 1, "price": row["price"],
                 "reservation_params": {"reservation_event_group_id": row["course_id"], "reservation_account_id": row["child_id"]}} for row in rows]

    def _own(self):
        return [{"reservation_id": row["id"], "reservation_event_group_id": row["course_id"], "reservation_account_id": row["child_id"],
                 "reserving_account_id": 10, "reservation_status_id": row["status"], "is_paid": row["is_paid"], "receipt_event_id": row["receipt_id"], "queue_number": row["queue_number"]}
                for row in self._db.execute("SELECT * FROM reservations ORDER BY id")]

    def _receipt(self, session_id):
        row = self._db.execute("SELECT id,total_amount,payment_type_id FROM receipts WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        return dict(row, receipt_id=row["id"], is_closed=1, is_pay_later=0) if row else None

    def _payment_types(self):
        return [{"payment_type_id": 4, "name": "Simuloitu testisaldo", "is_balance": True, "is_pay_later": False, "is_disabled": False},
                {"payment_type_id": 5, "name": "FastPay (simuloitu tallennettu testikortti)", "is_balance": False,
                 "is_pay_later": False, "is_disabled": self.scenario == "fastpay-kortti-puuttuu"}]

    def _dispatch(self, method, path, body, *, session_id):
        with self._lock:
            if method == "GET":
                if path == "/_test/status":
                    return self.stats()
                if path == PREFIX + "apicart":
                    return self._cart(session_id)
                if path == PREFIX + "apiusereventgroups":
                    return self._own()
                match = re.fullmatch(re.escape(PREFIX) + r"apieventgroups(?:/(\d+))?/?", path)
                if match:
                    count = int(self._meta("course_gets")) + 1
                    self._set("course_gets", count)
                    if self.scenario == "ruuhka" and count <= 2:
                        raise _APIError("Simuloitu ruuhka", 503, retry_after=1)
                    if match[1]:
                        stored = self._db.execute("SELECT * FROM courses WHERE id=?", (int(match[1]),)).fetchone()
                        if stored is None:
                            raise _APIError("Testikurssia ei löydy", 404)
                        return self._course(stored)
                    return [self._course(row) for row in self._db.execute("SELECT * FROM courses ORDER BY id")]
            elif method == "POST":
                if path == PREFIX + "apicart":
                    return self._add(body, session_id=session_id)
                if path == PREFIX + "apiusereventgroups":
                    return self._queue(body)
                if path.startswith("/webshop/call/"):
                    operation = path.removeprefix("/webshop/call/")
                    reads = {"user/getcurrentuser": {"account_id": 10, "name": "Testihuoltaja", "linked_accounts": [{"account_id": 20, "name": "Testilapsi"}]},
                        "cart/getreceiptinfo": self._receipt(session_id), "cart/islocked": {"isLocked": False},
                        "cart/getonlinepaymenttypes": self._payment_types(),
                        "cart/checkcurrentuserbalance": {"balance": int(self._meta("balance"))}}
                    if operation in reads:
                        return {"result": reads[operation]}
                    if operation == "cart/externalpaymentstart":
                        return self._checkout(body, session_id=session_id)
            raise _APIError("Tuntematon testirajapinta", 404)

    def _add(self, data, *, session_id):
        self._db.execute("INSERT INTO requests(kind) VALUES('cart')")
        try:
            if (not isinstance(data, dict) or set(data) - {"account", "fare_product_id", "reservation_event_group_id", "quantity", "price", "title", "campaign_code", "tags"}
                    or not isinstance(data.get("account"), dict) or _int(data["account"].get("account_id")) != 20
                    or _int(data.get("quantity")) != 1 or data.get("campaign_code")):
                raise _APIError("Vain yksi Testilapsen kurssi on sallittu")
            course_id = _int(data.get("reservation_event_group_id"))
            course = self._db.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
            if course is None or _int(data.get("fare_product_id")) != course["product_id"] or _int(data.get("price")) != course["price"]:
                raise _APIError("Kurssi, myyntituote tai hinta ei täsmää")
            if datetime.now(UTC) < self.opens_at:
                raise _APIError("Testi-ilmoittautuminen ei ole vielä alkanut")
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("SELECT count(*) FROM reservations WHERE course_id=? AND status IN(2,4,5,7)", (course_id,)).fetchone()[0] >= course["capacity"]:
                raise _APIError("Simuloitu kurssi on täynnä")
            existing = self._db.execute("SELECT id,status FROM reservations WHERE course_id=? AND child_id=20", (course_id,)).fetchone()
            if existing:
                if existing["status"] not in (1, 3):
                    raise _APIError("Testilapsella on jo varaus tähän kurssiin")
                reservation_id = existing["id"]
                self._db.execute("UPDATE reservations SET status=7,is_paid=0,receipt_id=NULL,queue_number=NULL,price=? WHERE id=?", (course["price"], reservation_id))
            else:
                reservation = self._db.execute("INSERT INTO reservations(course_id,child_id,price) VALUES(?,20,?)", (course_id, course["price"]))
                reservation_id = reservation.lastrowid
            self._db.execute("INSERT INTO cart(reservation_id,session_id) VALUES(?,?)", (reservation_id, session_id))
            self._db.execute("UPDATE requests SET course_id=?,child_id=20 WHERE id=(SELECT max(id) FROM requests)", (course_id,))
            lost = self.scenario == "varaus-vastaus-katoaa" and self._meta("lost_add") == "0"
            if lost:
                self._set("lost_add", 1)
            self._db.execute("COMMIT")
        except (ValueError, sqlite3.IntegrityError) as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise _APIError("Virheellinen tai jo olemassa oleva testivaraus") from exc
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        if lost:
            raise _APIError("Simuloitu vastauskatko: varaus tallennettiin", 503, retry_after=1)
        return self._cart(session_id)

    def _queue(self, data):
        self._db.execute("INSERT INTO requests(kind) VALUES('queue')")
        try:
            if (not isinstance(data, dict) or set(data) - {"reservation_event_group_id", "reservation_status_id", "account", "tags"}
                    or not isinstance(data.get("account"), dict) or _int(data["account"].get("account_id")) != 20
                    or data["account"].get("name") != "Testilapsi" or _int(data.get("reservation_status_id")) != 1):
                raise _APIError("Vain yksi Testilapsen jonopaikka on sallittu")
            course_id = _int(data.get("reservation_event_group_id"))
            course = self._db.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
            if course is None:
                raise _APIError("Testikurssia ei löydy", 404)
            if datetime.now(UTC) < self.opens_at:
                raise _APIError("Testi-ilmoittautuminen ei ole vielä alkanut")
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("SELECT count(*) FROM reservations WHERE course_id=? AND status IN(2,4,5,7)", (course_id,)).fetchone()[0] < course["capacity"]:
                raise _APIError("Testikurssilla on varsinainen paikka vapaana")
            queued = self._db.execute("SELECT count(*) FROM reservations WHERE course_id=? AND status IN(1,3)", (course_id,)).fetchone()[0]
            if queued >= course["queue_capacity"]:
                raise _APIError("Simuloitu jono on täynnä")
            number = self._db.execute("SELECT coalesce(max(queue_number),0)+1 FROM reservations WHERE course_id=? AND status IN(1,3)", (course_id,)).fetchone()[0]
            self._db.execute("INSERT INTO reservations(course_id,child_id,price,status,queue_number) VALUES(?,20,?,1,?)", (course_id, course["price"], number))
            self._db.execute("UPDATE requests SET course_id=?,child_id=20 WHERE id=(SELECT max(id) FROM requests)", (course_id,))
            lost = self.scenario == "jono-vastaus-katoaa" and self._meta("lost_queue") == "0"
            if lost:
                self._set("lost_queue", 1)
            self._db.execute("COMMIT")
        except (ValueError, sqlite3.IntegrityError) as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise _APIError("Virheellinen tai jo olemassa oleva testijonopaikka") from exc
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        if lost:
            raise _APIError("Simuloitu vastauskatko: jonopaikka tallennettiin", 503, retry_after=1)
        return self._own()

    def _checkout(self, data, *, session_id):
        self._db.execute("INSERT INTO requests(kind) VALUES('checkout')")
        try:
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise _APIError("Testimaksun tietomuoto on virheellinen")
            options, cart = data[0], self._cart(session_id)
            if len(cart) != 1 or cart[0]["reservation_params"]["reservation_account_id"] != 20:
                raise _APIError("Testimaksu edellyttää täsmälleen yhtä lapsen kurssia")
            price = cart[0]["price"]
            method = _int(options.get("payment_type_id"))
            balance = 0 if options.get("balance_amount") is None else _int(options["balance_amount"])
            if (set(options) - {"return_url", "cancel_url", "balance_amount", "payment_type_id", "service_group_id"}
                    or method not in {4, 5} or _int(options.get("service_group_id")) != 9):
                raise _APIError("Testimaksun maksutapa ei ole sallittu")
            if method == 4 and (balance != price or int(self._meta("balance")) < price):
                raise _APIError("Simuloitu testisaldo ei riitä tai summa ei täsmää")
            if method == 5 and (balance != 0 or self.scenario == "fastpay-kortti-puuttuu"):
                raise _APIError("FastPay-testikortti puuttuu tai testimaksuun yritettiin yhdistää saldoa")
            for key, state in (("return_url", "success"), ("cancel_url", "cancel")):
                target = urlsplit(options.get(key, ""))
                if f"{target.scheme}://{target.netloc}" != self.base_url or not target.path.startswith(PREFIX) or parse_qs(target.query).get("doPayment") != [state]:
                    raise _APIError("Testimaksun paluuosoite on virheellinen")
            if method == 5 and self.scenario == "fastpay-hylatty":
                raise _APIError("Simuloitu FastPay-korttiveloitus hylättiin. Maksua ei tallennettu.", 402)
            self._db.execute("BEGIN IMMEDIATE")
            receipt = self._db.execute("INSERT INTO receipts(total_amount,payment_type_id,session_id) VALUES(?,?,?)", (price, method, session_id))
            self._db.execute("UPDATE reservations SET status=4,is_paid=1,receipt_id=? WHERE id IN(SELECT reservation_id FROM cart WHERE session_id=?)", (receipt.lastrowid, session_id))
            self._db.execute("DELETE FROM cart WHERE session_id=?", (session_id,))
            if method == 4:
                self._set("balance", int(self._meta("balance")) - price)
            lost = self.scenario == "maksu-vastaus-katoaa" and self._meta("lost_payment") == "0"
            if lost:
                self._set("lost_payment", 1)
            self._db.execute("COMMIT")
        except ValueError as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise _APIError("Virheellinen testimaksupyyntö") from exc
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        if lost:
            raise _APIError("Simuloitu vastauskatko: maksu tallennettiin", 503, retry_after=1)
        return {"result": {"payment_succeeded": True, "receipt_id": receipt.lastrowid, "payment_type_id": method, "testshop": True}}

    def stats(self) -> dict:
        with self._lock:
            counts = dict(self._db.execute("SELECT kind,count(*) FROM requests GROUP BY kind"))
            own = self._own()
            receipts = [dict(row) for row in self._db.execute("SELECT id,total_amount,payment_type_id FROM receipts ORDER BY id")]
            card_charges = [row for row in receipts if row["payment_type_id"] == 5]
            return {"testshop": True, "scenario": self.scenario, "course_id": self.course_id,
                "base_url": self.base_url if self._server else None, "opens_at": self.opens_at.isoformat(),
                "cart_posts": counts.get("cart", 0), "add_posts": counts.get("cart", 0),
                "queue_posts": counts.get("queue", 0), "queued": sum(row["reservation_status_id"] in (1, 3) for row in own),
                "queue_capacity": self._db.execute("SELECT sum(queue_capacity) FROM courses").fetchone()[0],
                "checkout_posts": counts.get("checkout", 0), "writes": sum(counts.values()),
                "reservations": len(own), "confirmed": sum(row["is_paid"] == 1 for row in own),
                "receipts": len(receipts), "cart_items": self._db.execute("SELECT count(*) FROM cart").fetchone()[0], "balance_cents": int(self._meta("balance")),
                "fake_card_charges": len(card_charges), "fastpay_charges": len(card_charges),
                "fastpay_total_cents": sum(row["total_amount"] for row in card_charges),
                "payment_type_ids": sorted({row["payment_type_id"] for row in receipts}),
                "participant_ids": sorted({row["reservation_account_id"] for row in own}),
                "reservation_rows": own, "receipt_rows": receipts}
