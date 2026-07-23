"""
TFEX Signal Bot — Alert → LINE bridge + Dashboard server
--------------------------------------------------------
รับ webhook จาก TradingView (Pine Script alert) แล้ว:
  1) เก็บสัญญาณล่าสุดของแต่ละสัญญา (S50, Gold, USD futures ฯลฯ)
  2) push แจ้งเตือนเข้า LINE ผ่าน Messaging API
  3) เสิร์ฟหน้า Dashboard เว็บ ( / ) แสดงสัญญาณล่าสุดทั้งหมด

รันบนเซิร์ฟเวอร์/คอมที่มีเน็ต (เช่น VPS, Render, Railway, หรือรันเองแล้วเปิด ngrok)
TradingView จะยิง alert มาที่  https://<your-domain>/webhook

ค่าตั้งค่าอ่านจาก environment variables (ดู .env.example):
  LINE_CHANNEL_ACCESS_TOKEN : token ของ Messaging API channel
  LINE_TO                   : userId / groupId ปลายทางที่จะส่งหา
  WEBHOOK_SECRET            : (option) คีย์ลับกันคนอื่นยิง webhook มั่ว
"""
import os
import json
import datetime as dt
from flask import Flask, request, jsonify, Response

import requests

app = Flask(__name__)

LINE_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TO     = os.environ.get("LINE_TO", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

STORE_PATH = os.path.join(os.path.dirname(__file__), "signals.json")

ACTION_EMOJI = {"BUY": "🟢", "SELL": "🔴", "DCA": "🔵", "PREP": "🟡", "HOLD": "⚪"}
ACTION_TH    = {"BUY": "ซื้อจริง (MACD ตัด 0 ขึ้น)", "SELL": "ขาย (MACD ตัด 0 ลง)",
                "DCA": "ย่อ—เข้าเพิ่ม", "PREP": "เตรียมซื้อ (MACD ตัด Signal ขึ้น)", "HOLD": "ถือ/รอ"}


def _load():
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def push_line(text):
    """ส่งข้อความเข้า LINE ผ่าน Messaging API (push message)."""
    if not LINE_TOKEN or not LINE_TO:
        app.logger.warning("LINE not configured — skip push")
        return False, "LINE not configured"
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {LINE_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"to": LINE_TO, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        return r.status_code == 200, r.text
    except Exception as e:  # network error
        return False, str(e)


def parse_payload(req):
    """รองรับทั้ง JSON และข้อความธรรมดาจาก TradingView."""
    raw = req.get_data(as_text=True) or ""
    # ลองแบบ JSON ก่อน
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # เผื่อ TradingView ส่ง JSON ซ้อนใน field message
    if req.is_json:
        try:
            return req.get_json(force=True, silent=True) or {}
        except Exception:
            pass
    return {"raw": raw}


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET and request.args.get("secret") != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = parse_payload(request)
    symbol = str(data.get("symbol", "UNKNOWN"))
    action = str(data.get("action", "HOLD")).upper()
    price  = data.get("price")
    tf     = data.get("tf", "")
    rsi    = data.get("rsi")
    dip    = data.get("dip_pct")
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=7)))  # Asia/Bangkok

    store = _load()
    store[symbol] = {
        "symbol": symbol, "action": action, "price": price, "tf": tf,
        "rsi": rsi, "dip_pct": dip, "time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save(store)

    emoji = ACTION_EMOJI.get(action, "⚪")
    th    = ACTION_TH.get(action, action)
    msg = (
        f"{emoji} TFEX Signal — {symbol}\n"
        f"สัญญาณ: {action} ({th})\n"
        f"ราคา: {price}   TF: {tf}\n"
        f"RSI: {rsi}   ย่อจากไฮ: {dip}%\n"
        f"เวลา: {now.strftime('%H:%M น. %d/%m/%Y')}\n"
        f"— เครื่องมือช่วยดูจังหวะทางเทคนิค ไม่ใช่คำแนะนำลงทุน"
    )
    ok, detail = push_line(msg)
    return jsonify({"ok": True, "line_pushed": ok, "line_detail": detail, "stored": store[symbol]})


@app.route("/api/signals")
def api_signals():
    return jsonify(_load())


@app.route("/test-line")
def test_line():
    ok, detail = push_line("✅ ทดสอบการเชื่อมต่อ TFEX Signal Bot → LINE สำเร็จ")
    return jsonify({"ok": ok, "detail": detail})


@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ---- หน้า Dashboard (self-contained, poll /api/signals ทุก 15 วิ) ----
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TFEX Signal Dashboard</title>
<style>
 :root{--bg:#0e1116;--card:#171b22;--muted:#8b95a5;--line:#232a35;
   --buy:#22c55e;--sell:#ef4444;--dca:#3b82f6;--prep:#eab308;--hold:#64748b;--text:#e6edf3}
 *{box-sizing:border-box} body{margin:0;font-family:'Segoe UI',Tahoma,sans-serif;background:var(--bg);color:var(--text)}
 header{padding:20px 24px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
 h1{font-size:18px;margin:0;font-weight:600} .sub{color:var(--muted);font-size:13px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;padding:24px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;position:relative;overflow:hidden}
 .card .bar{position:absolute;left:0;top:0;bottom:0;width:5px}
 .sym{font-size:20px;font-weight:700;letter-spacing:.5px} .tf{color:var(--muted);font-size:12px}
 .badge{display:inline-block;margin-top:10px;padding:6px 14px;border-radius:999px;font-weight:700;font-size:15px;color:#fff}
 .row{display:flex;justify-content:space-between;margin-top:12px;font-size:14px;border-top:1px solid var(--line);padding-top:8px}
 .row span:first-child{color:var(--muted)}
 .time{color:var(--muted);font-size:12px;margin-top:12px}
 .empty{color:var(--muted);padding:40px;text-align:center}
 footer{color:var(--muted);font-size:12px;padding:16px 24px;border-top:1px solid var(--line)}
</style></head><body>
<header>
  <div><h1>📊 TFEX Signal Dashboard</h1><div class="sub">สัญญาณล่าสุดจาก TradingView · อัปเดตอัตโนมัติทุก 15 วินาที</div></div>
  <div class="sub" id="updated">—</div>
</header>
<div id="grid" class="grid"><div class="empty">ยังไม่มีสัญญาณเข้ามา… เมื่อ TradingView ยิง alert มาที่ /webhook การ์ดจะขึ้นที่นี่</div></div>
<footer>เครื่องมือช่วยดูจังหวะทางเทคนิค ไม่ใช่คำแนะนำการลงทุน · futures มีวันหมดอายุและใช้ margin โปรดบริหารความเสี่ยง</footer>
<script>
const COL={BUY:'var(--buy)',SELL:'var(--sell)',DCA:'var(--dca)',PREP:'var(--prep)',HOLD:'var(--hold)'};
const TH={BUY:'ซื้อจริง (ตัด 0 ขึ้น)',SELL:'ขาย (ตัด 0 ลง)',DCA:'ย่อ — เข้าเพิ่ม',PREP:'เตรียมซื้อ (ตัด Signal)',HOLD:'ถือ / รอ'};
function card(s){const c=COL[s.action]||COL.HOLD;
 return `<div class="card"><div class="bar" style="background:${c}"></div>
  <div class="sym">${s.symbol}</div><span class="tf">TF ${s.tf||'-'}</span>
  <div><span class="badge" style="background:${c}">${s.action} · ${TH[s.action]||''}</span></div>
  <div class="row"><span>ราคา</span><span>${s.price??'-'}</span></div>
  <div class="row"><span>RSI</span><span>${s.rsi??'-'}</span></div>
  <div class="row"><span>ย่อจากไฮ</span><span>${s.dip_pct??'-'}%</span></div>
  <div class="time">🕒 ${s.time||''}</div></div>`;}
async function load(){try{const r=await fetch('/api/signals');const d=await r.json();
 const g=document.getElementById('grid');const keys=Object.keys(d);
 if(!keys.length){return;}
 g.innerHTML=keys.map(k=>card(d[k])).join('');
 document.getElementById('updated').textContent='อัปเดตหน้าเมื่อ '+new Date().toLocaleTimeString('th-TH');
 }catch(e){}}
load();setInterval(load,15000);
</script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
