"""
SCENTMOTION - Bot de avisos de ventas por Telegram.

Lee los pedidos de la plataforma de la maquina (MAKMIK / weisao168, version movil)
y manda un mensaje de Telegram por cada venta nueva (tarjeta o efectivo), un aviso
si la maquina se desconecta y un resumen diario.

Modos:
    python sm_bot.py once     -> una pasada (para un Cron Job de Render cada 2 min)
    python sm_bot.py loop     -> bucle infinito (para un Background Worker)
    python sm_bot.py chatid   -> muestra tu chat_id de Telegram (configuracion inicial)
    python sm_bot.py test     -> manda un mensaje de prueba a Telegram
    python sm_bot.py resumen  -> manda ahora el resumen del dia

Variables de entorno (en Render, como "Environment Variables"):
    TELEGRAM_TOKEN     token del bot (BotFather)            [obligatoria]
    TELEGRAM_CHAT_ID   tu chat_id (sale con "chatid")        [obligatoria]
    MAKMIK_EMAIL       usuario de la plataforma              [obligatoria]
    MAKMIK_PASSWORD    contrasena de la plataforma           [obligatoria]
    DATABASE_URL       Postgres (Neon) para recordar estado  [recomendada]
    MACHINE_NUM        por defecto WSA2001806
    MACHINE_LABEL      por defecto "GF Fañabé"
    SUMMARY_HOUR       hora del resumen diario, por defecto 22
    POLL_SECONDS       solo modo loop, por defecto 120
"""

import json
import os
import sys
import time
import datetime as dt
from zoneinfo import ZoneInfo

import requests

BASE = "http://ws.m.adminxg.weisao168.com"
TZ_NAME = "Atlantic/Canary"
TZ = ZoneInfo(TZ_NAME)
UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

MACHINE_NUM = os.getenv("MACHINE_NUM", "WSA2001806")
MACHINE_LABEL = os.getenv("MACHINE_LABEL", "GF Fañabé")
SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "22"))
OFFLINE_CONFIRM_RUNS = 2  # pasadas seguidas offline antes de avisar (evita falsas alarmas)

PAYWAY = {
    "CARD": "💳 Tarjeta",
    "CASH": "🪙 Efectivo",
    "COIN": "🪙 Efectivo",
}


# ----------------------------------------------------------------------------
# Almacen de estado (Postgres si hay DATABASE_URL; si no, un JSON local)
# ----------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.url = os.getenv("DATABASE_URL")
        if self.url:
            import psycopg  # noqa: import solo si se usa
            self.conn = psycopg.connect(self.url, autocommit=True)
            with self.conn.cursor() as c:
                c.execute("CREATE TABLE IF NOT EXISTS sm_kv (k TEXT PRIMARY KEY, v TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS sm_seen (order_no TEXT PRIMARY KEY,"
                          " seen_at TIMESTAMPTZ DEFAULT now())")
        else:
            self.path = os.getenv("STATE_FILE", "sm_state.json")
            try:
                with open(self.path) as f:
                    self.data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                self.data = {"kv": {}, "seen": []}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f)

    def get(self, k, default=None):
        if self.url:
            with self.conn.cursor() as c:
                c.execute("SELECT v FROM sm_kv WHERE k=%s", (k,))
                r = c.fetchone()
                return r[0] if r else default
        return self.data["kv"].get(k, default)

    def set(self, k, v):
        if self.url:
            with self.conn.cursor() as c:
                c.execute("INSERT INTO sm_kv (k, v) VALUES (%s, %s) "
                          "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (k, v))
        else:
            self.data["kv"][k] = v
            self._save()

    def is_seen(self, order_no):
        if self.url:
            with self.conn.cursor() as c:
                c.execute("SELECT 1 FROM sm_seen WHERE order_no=%s", (order_no,))
                return c.fetchone() is not None
        return order_no in self.data["seen"]

    def mark_seen(self, order_no):
        if self.url:
            with self.conn.cursor() as c:
                c.execute("INSERT INTO sm_seen (order_no) VALUES (%s) "
                          "ON CONFLICT DO NOTHING", (order_no,))
                # limpieza: no hace falta recordar pedidos de hace mas de 10 dias
                c.execute("DELETE FROM sm_seen WHERE seen_at < now() - interval '10 days'")
        else:
            if order_no not in self.data["seen"]:
                self.data["seen"].append(order_no)
                self.data["seen"] = self.data["seen"][-2000:]
                self._save()


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def tg_send(text):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=20,
    )
    if not r.ok:
        print("Telegram error:", r.status_code, r.text[:300])
    return r.ok


def tg_show_chat_ids():
    token = os.environ["TELEGRAM_TOKEN"]
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    chats = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("No hay mensajes. Abre el bot en Telegram, pulsa Iniciar y escribe 'hola'. "
              "Luego vuelve a ejecutar este comando.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID = {cid}   ({name})")


# ----------------------------------------------------------------------------
# Plataforma MAKMIK (version movil, sin captcha)
# ----------------------------------------------------------------------------
class SessionExpired(Exception):
    pass


class Makmik:
    def __init__(self, store):
        self.store = store
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                               "Accept-Language": "es-ES,es;q=0.9",
                               "X-Requested-With": "XMLHttpRequest",
                               "Origin": BASE,
                               "Referer": f"{BASE}/client/mobile/index.html"})
        saved = store.get("cookies")
        if saved:
            try:
                self.s.cookies.update(json.loads(saved))
            except Exception:
                pass

    def login(self):
        r = self.s.post(
            f"{BASE}/api/AccountApi/Login",
            data={"email": os.environ["MAKMIK_EMAIL"], "pwd": os.environ["MAKMIK_PASSWORD"],
                  "timezone": "0", "timezonename": TZ_NAME},
            timeout=30,
        )
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError:
            body = {}
        ok = body.get("isSuccess") is not False and body.get("status") not in ("Fail", "Error")
        if not ok or not self.s.cookies:
            raise RuntimeError(f"Login MAKMIK fallido: {str(body)[:200]}")
        # diagnostico sin secretos: solo nombres de campos y de cookies
        print("Login MAKMIK campos:", list(body.keys()) if isinstance(body, dict) else type(body).__name__,
              "| data:", (list(body.get("data").keys()) if isinstance(body, dict) and isinstance(body.get("data"), dict)
                          else type(body.get("data")).__name__ if isinstance(body, dict) else "-"),
              "| cookies:", list(self.s.cookies.get_dict().keys()))
        self.store.set("cookies", json.dumps(self.s.cookies.get_dict()))
        print("Login MAKMIK OK")

    def _json(self, r):
        def why(reason):
            req = getattr(r, "request", None)
            hdrs = getattr(r, "headers", {}) or {}
            print(f"[MAKMIK] {reason} | HTTP {r.status_code} | {getattr(req, 'method', '?')} "
                  f"{str(r.url).split('?')[0]} | tipo {hdrs.get('Content-Type')} | respuesta: {r.text[:300]!r}")
            raise SessionExpired()
        if r.status_code in (401, 403) or r.url.rstrip("/").endswith("login"):
            why("sin permiso")
        try:
            body = r.json()
        except ValueError:
            why("no es JSON")  # devuelve HTML (pagina de login) -> sesion caducada
        if isinstance(body, dict) and body.get("isSuccess") is False:
            why("isSuccess=false")
        return body

    def _call(self, fn):
        """Llama a la API; si la sesion ha caducado, inicia sesion y repite una vez."""
        try:
            return fn()
        except SessionExpired:
            self.login()
            return fn()

    def orders(self, begin, end):
        def fetch():
            out, page = [], 1
            while True:
                r = self.s.get(f"{BASE}/api/OrderApi/OrderBuys", params={
                    "machineNum": MACHINE_NUM, "page": page, "pageSize": 50,
                    "beginTime": begin.strftime("%Y/%m/%d %H:%M"),
                    "endTime": end.strftime("%Y/%m/%d %H:%M"),
                    "orderStatus": "PAY_SUC",
                }, timeout=30)
                body = self._json(r)
                data = body.get("data") or []
                out.extend(data)
                total = body.get("totalCount") or 0
                if len(out) >= total or not data or page >= 20:
                    return out
                page += 1
        return self._call(fetch)

    def machines(self):
        def fetch():
            r = self.s.post(f"{BASE}/api/MachineApi/ListNew",
                            json={"status": -1, "machineTypeId": 0, "addressId": 0,
                                  "keyword": None}, timeout=30)
            return self._json(r)
        return self._call(fetch)


# ----------------------------------------------------------------------------
# Formato de mensajes
# ----------------------------------------------------------------------------
def eur(x):
    return f"{float(x or 0):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def pay_time(o):
    t = o.get("payTime") or o.get("createTime") or ""
    try:
        return dt.datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        return None


def product_lines(o):
    lines = []
    for p in o.get("products") or []:
        name = p.get("title") or p.get("name") or "Perfume"
        via = p.get("boxNum") or p.get("channel") or p.get("aisle")
        qty = p.get("count") or p.get("num") or 1
        txt = f"🧴 {name}"
        if via not in (None, ""):
            txt += f" (vía {via})"
        if qty and int(qty) > 1:
            txt += f" ×{qty}"
        lines.append(txt)
    return lines or ["🧴 (perfume no indicado)"]


def sale_message(o, today_orders):
    t = pay_time(o)
    hora = t.strftime("%H:%M") if t else "--:--"
    pw = PAYWAY.get(str(o.get("payway", "")).upper(), f"❔ {o.get('payway')}")
    n = len(today_orders)
    tot = sum(float(x.get("amount") or 0) for x in today_orders)
    lines = [f"💰 <b>Venta {eur(o.get('amount'))}</b>"]
    lines += product_lines(o)
    lines.append(f"{pw} · {hora}")
    lines.append(f"📍 {MACHINE_LABEL}")
    lines.append(f"📊 Hoy: {n} {'venta' if n == 1 else 'ventas'} · {eur(tot)}")
    if str(o.get("payway", "")).upper() == "CARD":
        lines.append("<i>(si fue una prueba con la tarjeta prepago, no cuenta)</i>")
    return "\n".join(lines)


def summary_message(day, orders):
    n = len(orders)
    tot = sum(float(o.get("amount") or 0) for o in orders)
    card = [o for o in orders if str(o.get("payway", "")).upper() == "CARD"]
    cash = [o for o in orders if str(o.get("payway", "")).upper() != "CARD"]
    by_product = {}
    for o in orders:
        for p in o.get("products") or [{"title": "?"}]:
            name = p.get("title") or "?"
            by_product[name] = by_product.get(name, 0) + 1
    rank = sorted(by_product.items(), key=lambda x: -x[1])
    lines = [f"📅 <b>Resumen {day.strftime('%d/%m/%Y')}</b> · {MACHINE_LABEL}",
             f"Ventas: <b>{n}</b> · {eur(tot)}",
             f"💳 Tarjeta: {len(card)} ({eur(sum(float(o.get('amount') or 0) for o in card))})",
             f"🪙 Efectivo: {len(cash)} ({eur(sum(float(o.get('amount') or 0) for o in cash))})"]
    if rank:
        lines.append("")
        lines.append("🏆 Perfumes:")
        for name, c in rank[:10]:
            lines.append(f"  {c} × {name}")
    if card:
        lines.append("")
        lines.append("<i>La tarjeta incluye las pruebas con prepago; réstalas si las hubo.</i>")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Logica principal
# ----------------------------------------------------------------------------
def check_sales(mk, store, now):
    begin = now - dt.timedelta(hours=26)
    orders = mk.orders(begin, now + dt.timedelta(minutes=5))
    orders.sort(key=lambda o: (pay_time(o) or now))
    today = [o for o in orders if (pay_time(o) or now).date() == now.date()]

    first_run = store.get("initialized") is None
    new = [o for o in orders if o.get("orderNo") and not store.is_seen(o["orderNo"])]
    for o in new:
        if not first_run:
            upto = [x for x in today if (pay_time(x) or now) <= (pay_time(o) or now)]
            tg_send(sale_message(o, upto))
        store.mark_seen(o["orderNo"])
    if first_run:
        store.set("initialized", now.isoformat())
        tg_send("✅ <b>Bot de ventas SCENTMOTION activado</b>\n"
                f"Vigilando la máquina {MACHINE_NUM} ({MACHINE_LABEL}).\n"
                f"Hoy lleva {len(today)} {'venta' if len(today) == 1 else 'ventas'} · "
                f"{eur(sum(float(o.get('amount') or 0) for o in today))}.")
    return len(new)


def check_online(mk, store):
    body = mk.machines()
    other = body.get("other") or {}
    offline = other.get("offline")
    if offline is None:
        return
    streak = int(store.get("offline_streak", "0"))
    alerted = store.get("offline_alerted", "0") == "1"
    if int(offline) > 0:
        streak += 1
        store.set("offline_streak", str(streak))
        if streak >= OFFLINE_CONFIRM_RUNS and not alerted:
            tg_send(f"⚠️ <b>Máquina OFFLINE</b> ({MACHINE_LABEL})\n"
                    "La plataforma no la ve conectada desde hace unos minutos. "
                    "Puede seguir cobrando con tarjeta, pero revisa la pantalla.")
            store.set("offline_alerted", "1")
    else:
        if alerted:
            tg_send(f"✅ Máquina de nuevo <b>ONLINE</b> ({MACHINE_LABEL}).")
        store.set("offline_streak", "0")
        store.set("offline_alerted", "0")


def maybe_summary(mk, store, now, force=False):
    key = now.date().isoformat()
    if not force and (now.hour < SUMMARY_HOUR or store.get("summary_date") == key):
        return
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    orders = mk.orders(start, now)
    tg_send(summary_message(now, orders))
    store.set("summary_date", key)


def run_once():
    store = Store()
    mk = Makmik(store)
    now = dt.datetime.now(TZ)
    for name, fn in (("ventas", lambda: check_sales(mk, store, now)),
                     ("online", lambda: check_online(mk, store)),
                     ("resumen", lambda: maybe_summary(mk, store, now))):
        try:
            fn()
        except Exception as e:  # una parte que falle no debe parar las demas
            print(f"[{name}] error: {e!r}")
            fails = int(store.get("fail_count", "0")) + 1
            store.set("fail_count", str(fails))
            if fails == 15:  # ~30 min fallando seguido
                tg_send(f"⚠️ El bot no consigue leer la plataforma de la máquina "
                        f"(último error: {str(e)[:150]}). Revisa la contraseña o la web.")
            return
    store.set("fail_count", "0")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "chatid":
        tg_show_chat_ids()
    elif mode == "test":
        print("Enviado" if tg_send("🔔 Prueba del bot SCENTMOTION: ¡funciona!") else "Fallo")
    elif mode == "resumen":
        store = Store()
        maybe_summary(Makmik(store), store, dt.datetime.now(TZ), force=True)
    elif mode == "loop":
        every = int(os.getenv("POLL_SECONDS", "120"))
        while True:
            run_once()
            time.sleep(every)
    else:
        run_once()


if __name__ == "__main__":
    main()
