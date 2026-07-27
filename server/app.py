"""
TFEX / SET50 Signal Bot — Alert -> LINE bridge + Dashboard realtime
-------------------------------------------------------------------
รับ webhook จาก TradingView 3 แบบ (ดูฟิลด์ "type" ใน JSON)

  1) type ไม่ระบุ  = สัญญาณจาก TFEX Signal Bot v2  (BUY/SELL/PREP/DCA + heartbeat)
  2) type=position = สถานะของที่ถือในพอร์ตจาก Port Monitor
                     (กำไร/ขาดทุน, BUY เขียว / SELL แดง / HOLD เหลือง, DCA / cut loss / sell)
  3) type=screen   = ผลสแกน SET50 จาก SET50 Screener (6 เกณฑ์)

หน้าที่ของเซิร์ฟเวอร์
  • เก็บสถานะล่าสุด + ประวัติ
  • push LINE เฉพาะตอน "คำแนะนำเปลี่ยน" หรือมีสัญญาณจริง (ไม่สแปมทุกแท่ง)
  • สรุปคำแนะนำรายวัน: ขายออก / ซื้อเข้า / ซื้อเพิ่ม  (GET /api/daily-summary?push=1)
  • เสิร์ฟ Dashboard realtime แบบแท็บ

ค่าตั้งค่าอ่านจาก environment variables (ดู .env.example)
"""
import os
import json
import datetime as dt

import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

LINE_TOKEN     = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TO        = os.environ.get("LINE_TO", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

BASE = os.path.dirname(__file__)
LATEST_PATH    = os.path.join(BASE, "signals.json")     # สถานะล่าสุดต่อสัญญา (TFEX signal)
HISTORY_PATH   = os.path.join(BASE, "history.json")     # ประวัติสัญญาณจริง
POSITION_PATH  = os.path.join(BASE, "positions.json")   # สถานะของที่ถือในพอร์ต
SCREEN_PATH    = os.path.join(BASE, "screen.json")      # ผลสแกน SET50

ACTIONABLE  = {"BUY", "SELL", "PREP", "DCA"}
HISTORY_MAX = 300

ACTION_EMOJI = {"BUY": "🟢", "SELL": "🔴", "DCA": "🔵", "PREP": "🟡", "HOLD": "⚪"}
ACTION_TH    = {"BUY": "ซื้อจริง (MACD ตัด 0 ขึ้น)", "SELL": "ขาย (MACD ตัด 0 ลง)",
                "DCA": "ย่อ—เข้าเพิ่ม", "PREP": "เตรียมซื้อ (MACD ตัด Signal ขึ้น)",
                "HOLD": "ถือ/รอ"}

# คำแนะนำของพอร์ต — โค้ดที่ "ต้องแจ้งเตือน" เมื่อเปลี่ยนสถานะ
ADVICE_PUSH = {"SELL", "CUT_LOSS", "TRAIL_EXIT", "DCA", "BUY", "TAKE_PARTIAL"}
ADVICE_TH   = {
    "SELL":         "ขายออก (เทรนด์พัง)",
    "CUT_LOSS":     "CUT LOSS (ขาดทุนเกินกฎ)",
    "TRAIL_EXIT":   "ขายล็อกกำไร (หลุด ATR trailing)",
    "TAKE_PARTIAL": "ทยอยล็อกกำไรบางส่วน",
    "DCA":          "ซื้อเพิ่ม (DCA ตอนย่อ)",
    "BUY":          "เข้าซื้อได้",
    "HOLD_LONG":    "ถือต่อ",
    "WATCH":        "เฝ้าดู",
    "WAIT":         "รอสัญญาณ",
    "HOLD":         "ถือ/รอ",
}
SELL_CODES = {"SELL", "CUT_LOSS", "TRAIL_EXIT"}
ADD_CODES  = {"DCA"}

CRITERIA = ["สัญญาณซื้อ (MACD ตัด Signal)", "MACD ตัด 0 + HH/HL", "Volume",
            "เหนือ MA200", "RSI ผ่านกรอง", "ATR คุมความผันผวน"]


# ---------------------------------------------------------------- utilities
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_bkk():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=7)))


def push_line(text):
    if not LINE_TOKEN or not LINE_TO:
        app.logger.warning("LINE not configured — skip push")
        return False, "LINE not configured"
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"to": LINE_TO, "messages": [{"type": "text", "text": text[:4900]}]},
            timeout=10,
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)


def parse_payload(req):
    raw = req.get_data(as_text=True) or ""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    if req.is_json:
        try:
            return req.get_json(force=True, silent=True) or {}
        except Exception:
            pass
    return {"raw": raw}


def fnum(v, nd=2):
    try:
        return f"{float(v):,.{nd}f}"
    except Exception:
        return "-"


# ---------------------------------------------------------------- webhook
@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET and request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    d = parse_payload(request)
    kind = str(d.get("type", "signal")).lower()

    if kind == "position":
        return handle_position(d)
    if kind == "screen":
        return handle_screen(d)
    return handle_signal(d)


def handle_signal(d):
    """สัญญาณ TFEX Signal Bot v2 (เดิม)"""
    symbol = str(d.get("symbol", "UNKNOWN"))
    action = str(d.get("action", "HOLD")).upper()
    now = now_bkk()

    rec = {
        "symbol": symbol, "action": action,
        "price": d.get("price"), "tf": d.get("tf", ""),
        "rsi": d.get("rsi"), "dip_pct": d.get("dip_pct"),
        "macd": d.get("macd"), "hist": d.get("hist"),
        "prox": d.get("prox", "-"), "prox_score": d.get("prox_score", 0),
        "fib": d.get("fib", "-"),
        "time": now.strftime("%Y-%m-%d %H:%M:%S"), "ts": now.isoformat(),
    }

    latest = _load(LATEST_PATH, {})
    latest[symbol] = rec
    _save(LATEST_PATH, latest)

    pushed, detail = False, "heartbeat (no push)"
    if action in ACTIONABLE:
        hist = _load(HISTORY_PATH, [])
        hist.insert(0, rec)
        _save(HISTORY_PATH, hist[:HISTORY_MAX])

        msg = (f"{ACTION_EMOJI.get(action,'⚪')} TFEX Signal — {symbol}\n"
               f"สัญญาณ: {action} ({ACTION_TH.get(action, action)})\n"
               f"ราคา: {rec['price']}   TF: {rec['tf']}\n"
               f"RSI: {rec['rsi']}   ย่อจากไฮ: {rec['dip_pct']}%\n"
               f"เวลา: {now.strftime('%H:%M น. %d/%m/%Y')}\n"
               f"— เครื่องมือช่วยดูจังหวะ ไม่ใช่คำแนะนำลงทุน")
        pushed, detail = push_line(msg)

    return jsonify({"ok": True, "kind": "signal", "stored": rec,
                    "line_pushed": pushed, "line_detail": detail})


def handle_position(d):
    """สถานะของที่ถือในพอร์ต จาก Port Monitor"""
    symbol = str(d.get("symbol", "UNKNOWN"))
    now = now_bkk()
    code = str(d.get("advice_code", "HOLD")).upper()

    rec = {
        "symbol": symbol, "tf": d.get("tf", ""),
        "status": str(d.get("status", "HOLD")).upper(),
        "advice_code": code, "advice": d.get("advice", ""),
        "price": d.get("price"), "cost": d.get("cost"), "qty": d.get("qty"),
        "pnl": d.get("pnl"), "pnl_pct": d.get("pnl_pct"),
        "rsi": d.get("rsi"), "macd": d.get("macd"),
        "above_ma": d.get("above_ma"), "stop": d.get("stop"),
        "dip_pct": d.get("dip_pct"), "has_pos": d.get("has_pos", True),
        "time": now.strftime("%Y-%m-%d %H:%M:%S"), "ts": now.isoformat(),
    }

    pos = _load(POSITION_PATH, {})
    prev = pos.get(symbol, {})
    changed = prev.get("advice_code") != code
    pos[symbol] = rec
    _save(POSITION_PATH, pos)

    pushed, detail = False, "no change (no push)"
    if changed and code in ADVICE_PUSH:
        emoji = "🔴" if code in SELL_CODES else ("🔵" if code in ADD_CODES else "🟢")
        pnl = rec.get("pnl_pct")
        pnl_txt = "-" if pnl in (None, "", "null") else f"{float(pnl):+.2f}%"
        msg = (f"{emoji} พอร์ต — {symbol} ({rec['tf']})\n"
               f"คำแนะนำ: {ADVICE_TH.get(code, code)}\n"
               f"{rec['advice']}\n"
               f"ราคา {rec['price']}  ต้นทุน {rec['cost']}  กำไร/ขาดทุน {pnl_txt}\n"
               f"ATR stop: {rec['stop']}   RSI {rec['rsi']}\n"
               f"เวลา: {now.strftime('%H:%M น. %d/%m/%Y')}\n"
               f"— เครื่องมือช่วยดูจังหวะ ไม่ใช่คำแนะนำลงทุน")
        pushed, detail = push_line(msg)

        hist = _load(HISTORY_PATH, [])
        hist.insert(0, {"symbol": symbol, "action": code, "price": rec["price"],
                        "tf": rec["tf"], "rsi": rec["rsi"], "kind": "position",
                        "time": rec["time"], "ts": rec["ts"]})
        _save(HISTORY_PATH, hist[:HISTORY_MAX])

    return jsonify({"ok": True, "kind": "position", "stored": rec,
                    "changed": changed, "line_pushed": pushed, "line_detail": detail})


def handle_screen(d):
    """ผลสแกน SET50 — Pine ส่งมาเป็นชุด (group A / group B)"""
    now = now_bkk()
    group = str(d.get("group", "A"))
    items = d.get("items") or []
    clean = []
    for it in items:
        clean.append({
            "symbol": str(it.get("s", "")), "score": it.get("sc"),
            "price": it.get("p"), "rsi": it.get("rsi"),
            "stop": it.get("stop"), "target": it.get("tgt"),
            "chg": it.get("chg"), "mask": it.get("m", 0),
            "group": group,
        })

    store = _load(SCREEN_PATH, {})
    prev_syms = {i["symbol"] for i in store.get(group, {}).get("items", [])}
    store[group] = {"tf": d.get("tf", ""), "min_score": d.get("min_score"),
                    "items": clean, "time": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "ts": now.isoformat()}
    _save(SCREEN_PATH, store)

    new_syms = [i for i in clean if i["symbol"] not in prev_syms]
    pushed, detail = False, "no new symbol (no push)"
    if new_syms:
        lines = [f"🟢 SET50 Screener — หุ้นเข้าเกณฑ์ใหม่ {len(new_syms)} ตัว"]
        for i in new_syms[:10]:
            lines.append(f"• {i['symbol']}  {i['score']}/6  ราคา {i['price']}  "
                         f"stop {i['stop']}  เป้า {i['target']}")
        lines.append(f"เวลา: {now.strftime('%H:%M น. %d/%m/%Y')}")
        lines.append("— เครื่องมือช่วยคัดกรอง ไม่ใช่คำแนะนำลงทุน")
        pushed, detail = push_line("\n".join(lines))

    return jsonify({"ok": True, "kind": "screen", "group": group,
                    "count": len(clean), "new": [i["symbol"] for i in new_syms],
                    "line_pushed": pushed, "line_detail": detail})


# ---------------------------------------------------------------- read APIs
@app.route("/api/state")
def api_state():
    return jsonify(_load(LATEST_PATH, {}))


@app.route("/api/history")
def api_history():
    return jsonify(_load(HISTORY_PATH, []))


@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(_load(POSITION_PATH, {}))


@app.route("/api/screener")
def api_screener():
    store = _load(SCREEN_PATH, {})
    items, times = [], []
    for g in sorted(store.keys()):
        items.extend(store[g].get("items", []))
        if store[g].get("time"):
            times.append(store[g]["time"])
    items.sort(key=lambda x: (-(x.get("score") or 0), x.get("symbol") or ""))
    return jsonify({"items": items, "updated": max(times) if times else "",
                    "criteria": CRITERIA})


def build_summary():
    pos = _load(POSITION_PATH, {})
    screen = _load(SCREEN_PATH, {})
    held = {s for s, r in pos.items() if r.get("has_pos")}

    sell, add, take, hold = [], [], [], []
    for s, r in pos.items():
        code = r.get("advice_code", "HOLD")
        if code in SELL_CODES:
            sell.append(r)
        elif code in ADD_CODES:
            add.append(r)
        elif code == "TAKE_PARTIAL":
            take.append(r)
        else:
            hold.append(r)

    buy = []
    for g in screen:
        for it in screen[g].get("items", []):
            if it["symbol"] not in held:
                buy.append(it)
    buy.sort(key=lambda x: -(x.get("score") or 0))

    total_pnl = 0.0
    for r in pos.values():
        try:
            total_pnl += float(r.get("pnl") or 0)
        except Exception:
            pass

    return {"date": now_bkk().strftime("%d/%m/%Y"), "sell": sell, "add": add,
            "take": take, "hold": hold, "buy": buy, "total_pnl": total_pnl}


@app.route("/api/summary")
def api_summary():
    return jsonify(build_summary())


def summary_text(s):
    L = [f"📋 สรุปคำแนะนำประจำวัน {s['date']}",
         f"กำไร/ขาดทุนรวมในพอร์ต: {s['total_pnl']:+,.2f}", ""]
    if s["sell"]:
        L.append(f"🔴 ควรขายออก ({len(s['sell'])})")
        for r in s["sell"]:
            L.append(f"• {r['symbol']} — {ADVICE_TH.get(r['advice_code'], r['advice_code'])} "
                     f"| ราคา {r['price']} | P/L {fnum(r.get('pnl_pct'))}%")
        L.append("")
    if s["take"]:
        L.append(f"🟠 ทยอยล็อกกำไร ({len(s['take'])})")
        for r in s["take"]:
            L.append(f"• {r['symbol']} — กำไร {fnum(r.get('pnl_pct'))}% | ราคา {r['price']}")
        L.append("")
    if s["add"]:
        L.append(f"🔵 ควรซื้อเพิ่ม / DCA ({len(s['add'])})")
        for r in s["add"]:
            L.append(f"• {r['symbol']} — ย่อ {fnum(r.get('dip_pct'),1)}% | ราคา {r['price']} "
                     f"| stop {r['stop']}")
        L.append("")
    if s["buy"]:
        L.append(f"🟢 น่าซื้อเข้าใหม่ — ผ่านสกรีน SET50 ({len(s['buy'])})")
        for i in s["buy"][:10]:
            L.append(f"• {i['symbol']} {i['score']}/6 | ราคา {i['price']} "
                     f"| stop {i['stop']} | เป้า {i['target']}")
        L.append("")
    if s["hold"]:
        L.append(f"🟡 ถือต่อ ({len(s['hold'])}): " + ", ".join(r["symbol"] for r in s["hold"]))
        L.append("")
    if not (s["sell"] or s["add"] or s["buy"] or s["take"]):
        L.append("วันนี้ยังไม่มีคำแนะนำให้ทำอะไร — ถือเงินสด/ถือของเดิม รอสัญญาณ")
        L.append("")
    L.append("— เครื่องมือช่วยดูจังหวะทางเทคนิค ไม่ใช่คำแนะนำการลงทุน")
    return "\n".join(L)


@app.route("/api/daily-summary")
def api_daily_summary():
    s = build_summary()
    text = summary_text(s)
    pushed, detail = False, "not requested"
    has_reco = bool(s["sell"] or s["add"] or s["buy"] or s["take"])
    want = request.args.get("push") in ("1", "true", "yes")
    only = request.args.get("only_if_any") in ("1", "true", "yes")
    if want and (has_reco or not only):
        pushed, detail = push_line(text)
    return jsonify({"ok": True, "has_reco": has_reco, "text": text,
                    "line_pushed": pushed, "line_detail": detail})


# ---------------------------------------------------------------- write APIs
@app.route("/api/clear", methods=["POST"])
def api_clear():
    _save(LATEST_PATH, {})
    _save(HISTORY_PATH, [])
    return jsonify({"ok": True})


@app.route("/api/clear/<symbol>", methods=["POST", "DELETE"])
def api_clear_symbol(symbol):
    latest = _load(LATEST_PATH, {})
    latest.pop(symbol, None)
    _save(LATEST_PATH, latest)
    pos = _load(POSITION_PATH, {})
    pos.pop(symbol, None)
    _save(POSITION_PATH, pos)
    return jsonify({"ok": True})


@app.route("/api/clear-portfolio", methods=["POST"])
def api_clear_portfolio():
    _save(POSITION_PATH, {})
    return jsonify({"ok": True})


@app.route("/api/clear-screen", methods=["POST"])
def api_clear_screen():
    _save(SCREEN_PATH, {})
    return jsonify({"ok": True})


@app.route("/test-line")
def test_line():
    ok, detail = push_line("✅ ทดสอบการเชื่อมต่อ TFEX Signal Bot → LINE สำเร็จ")
    return jsonify({"ok": ok, "detail": detail})


@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TFEX / SET50 Dashboard</title>
<style>
 :root{--bg:#0e1116;--card:#171b22;--muted:#8b95a5;--line:#232a35;
   --buy:#22c55e;--sell:#ef4444;--dca:#3b82f6;--prep:#eab308;--hold:#64748b;--text:#e6edf3}
 *{box-sizing:border-box} body{margin:0;font-family:'Segoe UI',Tahoma,sans-serif;background:var(--bg);color:var(--text)}
 header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
 h1{font-size:18px;margin:0;font-weight:600} .sub{color:var(--muted);font-size:12px}
 .controls{display:flex;gap:8px;flex-wrap:wrap}
 button{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font-size:13px;cursor:pointer}
 button:hover{border-color:#3a4657} button.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
 button.danger:hover{border-color:var(--sell);color:var(--sell)}
 nav{display:flex;gap:6px;padding:10px 24px;border-bottom:1px solid var(--line);flex-wrap:wrap}
 nav button{padding:8px 16px;font-weight:600}
 h2{font-size:13px;color:var(--muted);margin:20px 24px 8px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
 .wrap{padding:0 24px} .hide{display:none}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
 .c{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:8px}
 .ctop{display:flex;justify-content:space-between;align-items:center}
 .sym{font-size:17px;font-weight:700} .tf{color:var(--muted);font-size:11px}
 .badge{padding:4px 10px;border-radius:999px;font-weight:700;font-size:13px;color:#fff;white-space:nowrap}
 .pnl{font-size:22px;font-weight:700} .adv{font-size:13px;color:#fbbf24;line-height:1.45}
 .bar{height:8px;border-radius:6px;background:#0b0e13;overflow:hidden;border:1px solid var(--line)}
 .barfill{height:100%;border-radius:6px}
 .meta{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}
 table{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
 th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}
 th{color:var(--muted);font-weight:600;cursor:pointer;user-select:none;white-space:nowrap}
 th:hover{color:var(--text)} tr:last-child td{border-bottom:none}
 .pill{padding:2px 9px;border-radius:999px;font-weight:700;font-size:12px;color:#fff}
 .filters{display:flex;gap:8px;flex-wrap:wrap;margin:0 24px 10px;align-items:center}
 select,input{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px}
 .empty{color:var(--muted);padding:26px;text-align:center;background:var(--card);border:1px solid var(--line);border-radius:12px}
 .sum{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;white-space:pre-wrap;font-size:13.5px;line-height:1.7}
 .kpi{display:flex;gap:12px;flex-wrap:wrap;margin:0 24px 6px}
 .k{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:150px}
 .k .l{color:var(--muted);font-size:11px} .k .v{font-size:20px;font-weight:700;margin-top:2px}
 .tick{font-family:monospace;letter-spacing:2px;color:#38bdf8}
 footer{color:var(--muted);font-size:12px;padding:18px 24px;border-top:1px solid var(--line);margin-top:26px}
</style></head><body>
<header>
  <div><h1>📊 TFEX / SET50 Dashboard</h1><div class="sub" id="updated">—</div></div>
  <div class="controls">
    <button onclick="loadAll()">🔄 รีเฟรช</button>
    <button id="autoBtn" class="active" onclick="toggleAuto()">⏸ หยุดออโต้</button>
    <button class="danger" onclick="clearAll()">🗑 ล้างสัญญาณ</button>
  </div>
</header>
<nav>
  <button id="tb-port"   class="active" onclick="tab('port')">💼 พอร์ต</button>
  <button id="tb-screen" onclick="tab('screen')">🔎 สกรีน SET50</button>
  <button id="tb-watch"  onclick="tab('watch')">🎯 จับตา TFEX</button>
  <button id="tb-sum"    onclick="tab('sum')">📋 สรุปวันนี้</button>
  <button id="tb-hist"   onclick="tab('hist')">🕘 ประวัติ</button>
</nav>

<!-- พอร์ต -->
<section id="s-port">
  <div class="kpi">
    <div class="k"><div class="l">กำไร/ขาดทุนรวม</div><div class="v" id="kPnl">-</div></div>
    <div class="k"><div class="l">ถืออยู่</div><div class="v" id="kCount">-</div></div>
    <div class="k"><div class="l">ควรขาย / ควรเพิ่ม</div><div class="v" id="kAct">-</div></div>
  </div>
  <h2>💼 ของที่ถืออยู่ — TFEX ใช้ TF 4 ชม. · หุ้นใช้ TF รายวัน</h2>
  <div class="wrap"><div id="port" class="grid"></div></div>
</section>

<!-- สกรีน SET50 -->
<section id="s-screen" class="hide">
  <div class="filters">
    <select id="fScore" onchange="renderScreen()">
      <option value="0">ทุกคะแนน</option><option value="6">6/6 เท่านั้น</option>
      <option value="5" selected>5/6 ขึ้นไป</option><option value="4">4/6 ขึ้นไป</option>
    </select>
    <input id="qScreen" placeholder="ค้นหาหุ้น..." oninput="renderScreen()">
    <span class="sub" id="critLegend"></span>
  </div>
  <div class="wrap"><div id="screen"></div></div>
</section>

<!-- จับตา TFEX -->
<section id="s-watch" class="hide">
  <h2>🎯 จับตา — ใกล้สัญญาณแค่ไหน</h2>
  <div class="wrap"><div id="watch" class="grid"></div></div>
</section>

<!-- สรุปวันนี้ -->
<section id="s-sum" class="hide">
  <div class="filters"><button onclick="pushSummary()">📨 ส่งสรุปเข้า LINE ตอนนี้</button>
    <span class="sub">ตั้งให้ส่งอัตโนมัติทุกวันได้ด้วย cron-job.org — ดู README</span></div>
  <div class="wrap"><div id="sumBox" class="sum">กำลังโหลด…</div></div>
</section>

<!-- ประวัติ -->
<section id="s-hist" class="hide">
  <div class="filters">
    <select id="fAction" onchange="renderHistory()">
      <option value="">ทุก action</option>
      <option value="BUY">BUY</option><option value="SELL">SELL</option>
      <option value="PREP">เตรียมซื้อ</option><option value="DCA">DCA</option>
      <option value="CUT_LOSS">CUT LOSS</option><option value="TRAIL_EXIT">ล็อกกำไร</option>
    </select>
    <select id="fSymbol" onchange="renderHistory()"><option value="">ทุกสัญญา</option></select>
    <input id="fSearch" placeholder="ค้นหา..." oninput="renderHistory()">
  </div>
  <div class="wrap"><div id="historyBox"></div></div>
</section>

<footer>เครื่องมือช่วยดูจังหวะทางเทคนิค ไม่ใช่คำแนะนำการลงทุน · futures มีวันหมดอายุและใช้ margin โปรดบริหารความเสี่ยง</footer>

<script>
const COL={BUY:'var(--buy)',SELL:'var(--sell)',DCA:'var(--dca)',PREP:'var(--prep)',HOLD:'var(--hold)',
  CUT_LOSS:'var(--sell)',TRAIL_EXIT:'var(--sell)',TAKE_PARTIAL:'#f97316',HOLD_LONG:'var(--buy)',WATCH:'var(--prep)',WAIT:'var(--hold)'};
const TH={BUY:'ซื้อจริง',SELL:'ขาย',DCA:'ย่อ—เข้าเพิ่ม',PREP:'เตรียมซื้อ',HOLD:'ถือ/รอ',
  CUT_LOSS:'CUT LOSS',TRAIL_EXIT:'ล็อกกำไร',TAKE_PARTIAL:'ล็อกกำไรบางส่วน',HOLD_LONG:'ถือต่อ',WATCH:'เฝ้าดู',WAIT:'รอสัญญาณ'};
const STCOL={BUY:'var(--buy)',SELL:'var(--sell)',HOLD:'var(--prep)'};
let state={},history=[],port={},screen_={items:[],criteria:[]},summary=null;
let auto=true,timer=null,sortKey='ts',sortDir=-1,cur='port';

function tab(t){cur=t;['port','screen','watch','sum','hist'].forEach(x=>{
  document.getElementById('s-'+x).classList.toggle('hide',x!==t);
  document.getElementById('tb-'+x).classList.toggle('active',x===t);});}
function fmt(v,n){return (v===undefined||v===null||v===''||isNaN(+v))?'-':(+v).toLocaleString(undefined,{maximumFractionDigits:n===undefined?2:n});}
function ticks(m){m=+m||0;let s='';for(let i=0;i<6;i++)s+=((m>>i)&1)?'✓':'·';return s;}

async function loadAll(){
  try{
    const [s,h,p,sc,su]=await Promise.all([
      fetch('/api/state').then(r=>r.json()),fetch('/api/history').then(r=>r.json()),
      fetch('/api/portfolio').then(r=>r.json()),fetch('/api/screener').then(r=>r.json()),
      fetch('/api/summary').then(r=>r.json())]);
    state=s||{};history=h||[];port=p||{};screen_=sc||{items:[]};summary=su;
    renderPort();renderScreen();renderWatch();renderSummary();fillSymbolFilter();renderHistory();
    document.getElementById('updated').textContent='อัปเดตเมื่อ '+new Date().toLocaleTimeString('th-TH');
  }catch(e){console.log(e);}
}

function renderPort(){
  const box=document.getElementById('port');const arr=Object.values(port);
  const sell=arr.filter(r=>['SELL','CUT_LOSS','TRAIL_EXIT'].includes(r.advice_code)).length;
  const add=arr.filter(r=>r.advice_code==='DCA').length;
  let tot=0;arr.forEach(r=>{if(!isNaN(+r.pnl))tot+=+r.pnl;});
  document.getElementById('kPnl').textContent=(tot>=0?'+':'')+fmt(tot);
  document.getElementById('kPnl').style.color=tot>=0?'var(--buy)':'var(--sell)';
  document.getElementById('kCount').textContent=arr.length+' ตัว';
  document.getElementById('kAct').textContent=sell+' / '+add;
  if(!arr.length){box.innerHTML='<div class="empty">ยังไม่มีข้อมูลพอร์ต — ใส่สคริปต์ Port Monitor ลงกราฟแต่ละตัว ตั้ง Alert แบบ Any alert() function call แล้วชี้ Webhook มาที่ /webhook</div>';return;}
  arr.sort((a,b)=>(['SELL','CUT_LOSS','TRAIL_EXIT'].includes(b.advice_code)?2:b.advice_code==='DCA'?1:0)
                 -(['SELL','CUT_LOSS','TRAIL_EXIT'].includes(a.advice_code)?2:a.advice_code==='DCA'?1:0));
  box.innerHTML=arr.map(r=>{
    const st=STCOL[r.status]||'var(--hold)';const pc=(+r.pnl_pct);
    const pcol=isNaN(pc)?'var(--muted)':(pc>=0?'var(--buy)':'var(--sell)');
    return `<div class="c" style="border-left:4px solid ${st}">
      <div class="ctop"><div><span class="sym">${r.symbol}</span> <span class="tf">TF ${r.tf||'-'}</span></div>
        <span class="badge" style="background:${st}">${r.status}</span></div>
      <div class="pnl" style="color:${pcol}">${isNaN(+r.pnl)?'-':((+r.pnl>=0?'+':'')+fmt(r.pnl))} <span style="font-size:14px">(${isNaN(pc)?'-':(pc>=0?'+':'')+fmt(pc)}%)</span></div>
      <div class="adv">👉 ${r.advice||TH[r.advice_code]||'-'}</div>
      <div class="meta"><span>ราคา ${fmt(r.price)}</span><span>ต้นทุน ${fmt(r.cost)} × ${fmt(r.qty,0)}</span></div>
      <div class="meta"><span>RSI ${fmt(r.rsi,1)} · MACD ${fmt(r.macd,3)}</span><span>${r.above_ma?'เหนือ MA200 ✔':'ใต้ MA200 ✘'}</span></div>
      <div class="meta"><span>ATR stop ${fmt(r.stop)}</span><span>🕒 ${(r.time||'').slice(11)}</span></div>
    </div>`;}).join('');
}

function renderScreen(){
  const box=document.getElementById('screen');const min=+document.getElementById('fScore').value;
  const q=(document.getElementById('qScreen').value||'').toUpperCase();
  const cr=screen_.criteria||[];
  document.getElementById('critLegend').textContent=cr.length?('เกณฑ์ 1-6: '+cr.join(' · ')):'';
  let rows=(screen_.items||[]).filter(i=>(+i.score>=min)&&(!q||i.symbol.indexOf(q)>=0));
  if(!rows.length){box.innerHTML='<div class="empty">ยังไม่มีผลสแกน — ใส่ SET50 Screener A/B ลงกราฟ แล้วตั้ง Alert ชี้มาที่ /webhook</div>';return;}
  box.innerHTML=`<table><thead><tr><th>อันดับ</th><th>หุ้น</th><th>คะแนน</th><th>เกณฑ์ 1-6</th>
    <th>ราคา</th><th>%วัน</th><th>RSI</th><th>Stop (ATR)</th><th>เป้า</th></tr></thead><tbody>`+
    rows.map((i,n)=>{const sc=+i.score;const c=sc>=6?'var(--buy)':sc>=5?'#65a30d':sc>=4?'#d97706':'var(--hold)';
      const ch=+i.chg;
      return `<tr><td>${n+1}</td><td style="font-weight:700">${i.symbol}</td>
      <td><span class="pill" style="background:${c}">${sc}/6</span></td>
      <td class="tick">${ticks(i.mask)}</td><td>${fmt(i.price)}</td>
      <td style="color:${ch>=0?'var(--buy)':'var(--sell)'}">${isNaN(ch)?'-':(ch>=0?'+':'')+fmt(ch)}%</td>
      <td>${fmt(i.rsi,1)}</td><td>${fmt(i.stop)}</td><td>${fmt(i.target)}</td></tr>`;}).join('')+
    `</tbody></table><div class="sub" style="margin-top:8px">อัปเดตผลสแกนล่าสุด: ${screen_.updated||'-'}</div>`;
}

function proxColor(p){if(p&&p.indexOf('SELL')>=0)return 'var(--sell)';if(p&&p.indexOf('BUY')>=0)return 'var(--buy)';
  if(p&&p.indexOf('LONG')>=0)return 'var(--buy)';return 'var(--hold)';}
function urgency(s){const p=s.prox||'';const sc=+s.prox_score||0;return p.indexOf('ใกล้')>=0?1000+sc:sc;}
function renderWatch(){
  const box=document.getElementById('watch');const arr=Object.values(state).sort((a,b)=>urgency(b)-urgency(a));
  if(!arr.length){box.innerHTML='<div class="empty">ยังไม่มีข้อมูล… เมื่อ TradingView ยิงเข้ามา การ์ดจะขึ้นที่นี่</div>';return;}
  box.innerHTML=arr.map(s=>{const pc=proxColor(s.prox);const sc=Math.max(0,Math.min(100,+s.prox_score||0));
    return `<div class="c"><div class="ctop"><div><span class="sym">${s.symbol}</span> <span class="tf">TF ${s.tf||'-'}</span></div>
      <span class="badge" style="background:${pc}">${s.prox||'-'}</span></div>
      <div class="bar"><div class="barfill" style="width:${sc}%;background:${pc}"></div></div>
      <div class="meta"><span>ความใกล้ ${sc}%</span><span>MACD ${fmt(s.macd,4)} · hist ${fmt(s.hist,4)}</span></div>
      <div class="meta"><span>ราคา ${s.price??'-'}</span><span>RSI ${s.rsi??'-'} · ย่อ ${s.dip_pct??'-'}%</span></div>
      <div class="meta"><span style="color:${COL[s.action]||COL.HOLD}">● ล่าสุด: ${TH[s.action]||s.action}</span><span>🕒 ${(s.time||'').slice(11)}</span></div></div>`;}).join('');
}

function renderSummary(){
  if(!summary){return;}
  const s=summary;const L=[];
  L.push(`สรุปวันที่ ${s.date}  ·  กำไร/ขาดทุนรวม ${(s.total_pnl>=0?'+':'')+fmt(s.total_pnl)}`);
  L.push('');
  const sec=(icon,title,arr,f)=>{if(!arr||!arr.length)return;L.push(`${icon} ${title} (${arr.length})`);arr.forEach(x=>L.push('   • '+f(x)));L.push('');};
  sec('🔴','ควรขายออก',s.sell,r=>`${r.symbol} — ${r.advice}`);
  sec('🟠','ทยอยล็อกกำไร',s.take,r=>`${r.symbol} — กำไร ${fmt(r.pnl_pct)}%`);
  sec('🔵','ควรซื้อเพิ่ม / DCA',s.add,r=>`${r.symbol} — ย่อ ${fmt(r.dip_pct,1)}% · stop ${fmt(r.stop)}`);
  sec('🟢','น่าซื้อเข้าใหม่ (ผ่านสกรีน SET50)',s.buy,i=>`${i.symbol} ${i.score}/6 · ราคา ${fmt(i.price)} · stop ${fmt(i.stop)} · เป้า ${fmt(i.target)}`);
  if(s.hold&&s.hold.length)L.push(`🟡 ถือต่อ (${s.hold.length}): `+s.hold.map(r=>r.symbol).join(', '));
  if(!s.sell.length&&!s.add.length&&!s.buy.length&&!s.take.length)L.push('วันนี้ยังไม่มีคำแนะนำให้ทำอะไร — รอสัญญาณ');
  document.getElementById('sumBox').textContent=L.join('\n');
}
async function pushSummary(){const r=await fetch('/api/daily-summary?push=1').then(r=>r.json());
  alert(r.line_pushed?'ส่งเข้า LINE แล้ว':'ส่งไม่สำเร็จ: '+r.line_detail);}

function fillSymbolFilter(){const sel=document.getElementById('fSymbol');const cur=sel.value;
  const syms=[...new Set(history.map(h=>h.symbol))];
  sel.innerHTML='<option value="">ทุกสัญญา</option>'+syms.map(s=>`<option>${s}</option>`).join('');sel.value=cur;}
function setSort(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=(k==='ts')?-1:1;}renderHistory();}
function renderHistory(){
  const fa=document.getElementById('fAction').value,fs=document.getElementById('fSymbol').value,
        q=(document.getElementById('fSearch').value||'').toLowerCase();
  let rows=history.filter(h=>(!fa||h.action===fa)&&(!fs||h.symbol===fs)&&
    (!q||(h.symbol+h.action+(h.price||'')).toLowerCase().indexOf(q)>=0));
  rows.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(sortKey==='price'){x=+x;y=+y;}return (x>y?1:x<y?-1:0)*sortDir;});
  const box=document.getElementById('historyBox');
  if(!rows.length){box.innerHTML='<div class="empty">ยังไม่มีสัญญาณในประวัติ</div>';return;}
  const ar=k=>sortKey===k?(sortDir>0?' ▲':' ▼'):'';
  box.innerHTML=`<table><thead><tr><th onclick="setSort('ts')">เวลา${ar('ts')}</th>
    <th onclick="setSort('symbol')">สัญญา${ar('symbol')}</th><th onclick="setSort('action')">Action${ar('action')}</th>
    <th onclick="setSort('price')">ราคา${ar('price')}</th><th>TF</th><th>RSI</th><th>ที่มา</th></tr></thead><tbody>`+
    rows.map(h=>`<tr><td>${h.time||''}</td><td>${h.symbol}</td>
      <td><span class="pill" style="background:${COL[h.action]||COL.HOLD}">${h.action}</span> ${TH[h.action]||''}</td>
      <td>${h.price??'-'}</td><td>${h.tf||'-'}</td><td>${h.rsi??'-'}</td><td>${h.kind==='position'?'พอร์ต':'สัญญาณ'}</td></tr>`).join('')+
    `</tbody></table>`;
}

function toggleAuto(){auto=!auto;const b=document.getElementById('autoBtn');
  b.classList.toggle('active',auto);b.textContent=auto?'⏸ หยุดออโต้':'▶ เล่นออโต้';
  if(auto){timer=setInterval(loadAll,15000);}else{clearInterval(timer);}}
async function clearAll(){if(!confirm('ล้างสถานะสัญญาณและประวัติทั้งหมด?'))return;
  await fetch('/api/clear',{method:'POST'});loadAll();}

loadAll();timer=setInterval(loadAll,15000);
</script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
