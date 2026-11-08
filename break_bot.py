#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GER40 — бот алертів CHoCH з індикатора ger40_break.pine.

Приймає webhook-алерти, пересилає в приватний Telegram-чат, АЛЕ лише у вікно
08:00–18:00 за Києвом (DST враховується). Поза вікном — не шле (але логує).
Без залежностей (стандартна бібліотека).

Запуск:
    export TG_TOKEN=...  TG_CHAT_ID=...  WEBHOOK_SECRET=...   # secret = як у Pine
    python3 break_bot.py

Локальний тест: DRY_RUN=1 python3 break_bot.py
Налаштування вікна: TRADE_TZ=Europe/Kyiv TRADE_START=8 TRADE_END=18 TRADE_DAYS=1-5
"""
import os, sys, json, time, csv, hashlib, datetime as dt
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TG_TOKEN   = os.environ.get("TG_TOKEN", "").strip()
# один або кілька отримувачів через кому: "810335685,-1001234567890,..."
TG_CHAT_IDS = [c.strip() for c in os.environ.get("TG_CHAT_ID", "").split(",") if c.strip()]
SECRET     = os.environ.get("WEBHOOK_SECRET", "CHANGE_ME_SECRET").strip()
PORT       = int(os.environ.get("PORT", "8080"))
DRY_RUN    = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False")
LOG_CSV    = os.environ.get("LOG_CSV", os.path.join(os.path.dirname(os.path.abspath(__file__)), "break_log.csv"))

TRADE_TZ    = os.environ.get("TRADE_TZ", "Europe/Kyiv").strip()
TRADE_START = int(os.environ.get("TRADE_START", "8"))     # година (включно)
TRADE_END   = int(os.environ.get("TRADE_END", "18"))      # година (не включно)
TRADE_DAYS  = os.environ.get("TRADE_DAYS", "1-5").strip() # 1=Пн .. 7=Нд
FORCE_HOUR  = os.environ.get("FORCE_HOUR", "").strip()     # для тестів: примусова година
# символи, що працюють усі 7 днів (своє вікно годин), напр. "BTCUSD,ETHUSD"
ALWAYS = {s.strip().upper() for s in os.environ.get("ALWAYS_SYMBOLS", "").split(",") if s.strip()}
ALWAYS_START = int(os.environ.get("ALWAYS_START", "8"))    # година (включно)
ALWAYS_END   = int(os.environ.get("ALWAYS_END", "20"))     # година (не включно)
ALWAYS_DAYS  = os.environ.get("ALWAYS_DAYS", "1-7").strip() # 1=Пн .. 7=Нд (за замовч. усі дні)
API         = f"https://api.telegram.org/bot{TG_TOKEN}"

SIDE = {"LONG": ("🟢", "↑"), "SHORT": ("🔴", "↓")}

# розбір днів "1-5" / "1,2,3"
def _parse_days(s):
    out = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out or {1, 2, 3, 4, 5}
DAYS = _parse_days(TRADE_DAYS)
ADAYS = _parse_days(ALWAYS_DAYS)

_seen = {}  # дедуплікація: signature -> ts


def now_kyiv():
    try:
        from zoneinfo import ZoneInfo
        n = dt.datetime.now(ZoneInfo(TRADE_TZ))
    except Exception:  # noqa: BLE001
        # запасний варіант: UTC+2 (без DST) — попередження
        n = dt.datetime.utcnow() + dt.timedelta(hours=2)
    if FORCE_HOUR != "":
        n = n.replace(hour=int(FORCE_HOUR))
    return n


def _check_window(start, end, days):
    n = now_kyiv()
    iso_weekday = n.isoweekday()  # 1=Пн..7=Нд
    if iso_weekday not in days:
        return False, f"день {iso_weekday} поза {sorted(days)}"
    if not (start <= n.hour < end):
        return False, f"{n.hour:02d}:xx поза {start:02d}-{end:02d} Київ"
    return True, f"{n.hour:02d}:{n.minute:02d} Київ — у вікні"


def in_window():
    return _check_window(TRADE_START, TRADE_END, DAYS)


def tg_send(text):
    if DRY_RUN:
        print("---- DRY_RUN telegram ----")
        print(text)
        print("--------------------------")
        return True
    if not TG_TOKEN or not TG_CHAT_IDS:
        return False
    ok_any = False
    for chat_id in TG_CHAT_IDS:
        data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                           "disable_web_page_preview": True}).encode("utf-8")
        try:
            req = urllib.request.Request(f"{API}/sendMessage", data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ok_any = ok_any or (200 <= r.status < 300)
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] telegram {chat_id}: {type(e).__name__}", file=sys.stderr)
    return ok_any


def _sidetxt(side):
    return "ЛОНГ" if side == "LONG" else "ШОРТ" if side == "SHORT" else side


def build_message(p):
    side = p.get("side", "")
    emoji, arrow = SIDE.get(side, ("•", ""))
    stage = str(p.get("stage", "")).upper()
    sym = p.get("symbol", "GER40")
    tf = p.get("tf", "")

    if stage == "FVG":
        # Стадія 2 — ФВГ сформувався, готовий сетап
        lines = [f"{emoji} *ФВГ — сетап готовий {arrow}* {sym} [{tf}]"]
        lines.append(f"Сторона: *{_sidetxt(side)}*")
        lines.append(f"Вхід (середина FVG): `{p.get('entry', '—')}`")
        lines.append(f"Стоп: `{p.get('stop', '—')}`")
        lines.append(f"Цілі: `{p.get('t1', '—')}` · `{p.get('t2', '—')}` · `{p.get('t3', '—')}`")
        lines.append("_1.5R / 2R / 2.5R — частинами, з переносом у беззбиток_")
        if p.get("comment"):
            lines.append("")
            lines.append(f"_{p.get('comment')}_")
        return "\n".join(lines)

    # Стадія 1 — злам структури (без ФВГ)
    rel = p.get("relevant") in (True, "true", "True", 1)
    poi = p.get("poi", "none")
    disp = p.get("displacement") in (True, "true", "True", 1)
    lines = [f"{emoji} *Злам структури {arrow}* {sym} [{tf}]"]
    lines.append(f"Рівень: `{p.get('level', '—')}`")
    if rel:
        lines.append(f"Контекст: ✅ *{poi}*" + (" · імпульс ✅" if disp else ""))
    else:
        lines.append("Контекст: ⚠️ *без HTF опори*")
    if p.get("comment"):
        lines.append("")
        lines.append(f"_{p.get('comment')}_")
    return "\n".join(lines)


def log_row(p, sent, reason):
    try:
        new = not os.path.exists(LOG_CSV)
        with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["received_at", "kyiv", "sent", "reason", "symbol", "tf",
                            "side", "level", "relevant", "poi", "displacement"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), now_kyiv().strftime("%Y-%m-%d %H:%M"),
                        int(sent), reason, p.get("symbol"), p.get("tf"), p.get("side"),
                        p.get("level"), p.get("relevant"), p.get("poi"), p.get("displacement")])
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] log: {e}", file=sys.stderr)


def is_dup(p):
    sig = f"{p.get('symbol')}:{p.get('side')}:{p.get('stage')}:{p.get('level', p.get('entry'))}:{p.get('tf')}"
    nowt = time.time()
    for k in [k for k, v in _seen.items() if nowt - v > 120]:
        _seen.pop(k, None)
    if sig in _seen and nowt - _seen[sig] < 120:
        return True
    _seen[sig] = nowt
    return False


class Handler(BaseHTTPRequestHandler):
    def _s(self, code, body="ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):
        return

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz"):
            ok, why = in_window()
            self._s(200, f"healthy · {why}")
        else:
            self._s(200, "GER40 break bot up")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        try:
            p = json.loads(raw)
        except json.JSONDecodeError:
            self._s(400, "bad json")
            return
        if SECRET and str(p.get("secret", "")) != SECRET:
            self._s(403, "bad secret")
            return
        if is_dup(p):
            self._s(200, "duplicate ignored")
            return
        sym = str(p.get("symbol", "")).upper()
        if sym in ALWAYS:
            ok, why = _check_window(ALWAYS_START, ALWAYS_END, ADAYS)
        else:
            ok, why = in_window()
        if ok:
            tg_send(build_message(p))
            log_row(p, True, why)
            self._s(200, "sent")
        else:
            log_row(p, False, why)
            self._s(200, f"suppressed ({why})")


def main():
    print(f"GER40 break bot :{PORT}  DRY_RUN={DRY_RUN}  вікно {TRADE_START:02d}-{TRADE_END:02d} "
          f"{TRADE_TZ} дні {sorted(DAYS)}")
    if ALWAYS:
        print(f"  спец-символи {sorted(ALWAYS)}: вікно {ALWAYS_START:02d}-{ALWAYS_END:02d} "
              f"дні {sorted(ADAYS)}")
    print(f"  log={LOG_CSV}")
    ok, why = in_window()
    print(f"Зараз: {why}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
