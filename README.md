# TFEX Signal Bot 📊

บอทช่วยดูจังหวะ **Buy / Sell / DCA (ย่อแล้วซื้อ)** ของสัญญา TFEX เช่น **S50U26, GOU26, USDZ26**
ทำงานร่วมกับ **TradingView** ที่คุณมีอยู่แล้ว โดยใช้ **RSI + เส้นค่าเฉลี่ย (EMA) + MACD + การจับจังหวะย่อ**
แจ้งเตือนเข้า **LINE** อัตโนมัติ และมี **หน้า Dashboard เว็บ** รวมสัญญาณล่าสุดของทุกสัญญา

> ⚠️ เครื่องมือนี้ช่วย "บอกจังหวะทางเทคนิค" เท่านั้น **ไม่ใช่คำแนะนำการลงทุน**
> Futures มีวันหมดอายุและใช้ margin (เลเวอเรจ) โปรดบริหารความเสี่ยงและตัดสินใจเอง

---

## ภาพรวมการทำงาน

```
   TradingView (กราฟ S50/Gold/USD futures)
        │  ← ติดตั้ง Pine Script "TFEX Signal Bot"
        │     คำนวณ RSI/EMA/MACD + จับจังหวะย่อ → ขึ้นป้าย BUY/SELL/DCA + ตาราง Dashboard บนกราฟ
        │
        │  เมื่อเกิดสัญญาณ → ยิง Alert (webhook, ข้อความเป็น JSON)
        ▼
   เซิร์ฟเวอร์ตัวเชื่อม (server/app.py)
        ├─► push แจ้งเตือนเข้า LINE (Messaging API)
        └─► เก็บสัญญาณล่าสุด + เสิร์ฟหน้า Dashboard เว็บ ( / )
```

3 ส่วนในโปรเจกต์นี้:

| ส่วน | ไฟล์ | ใช้ทำอะไร | ใช้ได้บนแพลนไหน |
|---|---|---|---|
| 1. Pine Script | `tradingview/TFEX_Signal_Bot.pine` | ขึ้นป้าย/ตารางสัญญาณบนกราฟ + ตั้ง Alert | ทุกแพลน (รวม Free) |
| 2. Alert → LINE + Dashboard | `server/app.py` | รับ alert → ส่ง LINE + หน้าเว็บ | ต้องมี **TradingView Essential ขึ้นไป** (ใช้ webhook) |
| 3. เดโม Dashboard | `dashboard-demo.html` | ดูหน้าตา dashboard ได้เลย (ข้อมูลตัวอย่าง) | เปิดในเบราว์เซอร์ได้ทันที |

---

## ส่วนที่ 1 — ติดตั้ง Pine Script บน TradingView

1. เปิด TradingView → เปิดกราฟสัญญาที่ต้องการ (เช่น ค้นหา **S50U2026 / Gold Futures / USD Futures** บน TFEX)
2. เมนูล่าง **Pine Editor** → ลบโค้ดตัวอย่าง → วางเนื้อหาจากไฟล์ `tradingview/TFEX_Signal_Bot.pine` ทั้งหมด
3. กด **Add to chart** → จะเห็นเส้น EMA, ป้าย BUY/SELL/DCA และตาราง Dashboard มุมขวาบน
4. ปรับค่าได้ที่ไอคอน ⚙️ ของ indicator เช่น ความไวของ "ย่อกี่ %", ช่วง RSI, เส้น EMA
5. ทำซ้ำกับทุกสัญญา และตั้งได้ทั้ง **ราย TF** เช่น กราฟ Day (swing) และกราฟ 30/60 นาที (intraday)

### ตั้ง Alert (เพื่อส่งเข้า LINE)
1. คลิกขวาบนกราฟ → **Add alert** (หรือไอคอนนาฬิกา ⏰)
2. **Condition**: เลือก `TFEX Signal Bot` → เลือก **Any alert() function call**
   (ข้อความจะเป็น JSON อัตโนมัติ เช่น `{"symbol":"S50U26","action":"BUY",...}`)
3. **Notifications → Webhook URL**: ใส่ `https://<โดเมนเซิร์ฟเวอร์ของคุณ>/webhook`
   (ถ้าตั้ง `WEBHOOK_SECRET` ให้ใส่เป็น `.../webhook?secret=ค่าที่ตั้ง`)
4. Expiration ตั้งเป็น **Open-ended** เพื่อไม่ให้หมดอายุ
5. กด Create — ทำ 1 alert ต่อ 1 กราฟ/สัญญา

> หมายเหตุ: การส่ง **Webhook** ต้องใช้แพลน **Essential ขึ้นไป**
> ถ้าใช้ **Free**: ยังได้ป้าย/ตารางบนกราฟ + Alert แบบเด้งในแอป/อีเมลครบ แต่จะยังส่งเข้า LINE อัตโนมัติไม่ได้

---

## ส่วนที่ 2 — เซิร์ฟเวอร์ Alert → LINE + Dashboard

### 2.1 เตรียม LINE Messaging API
> LINE Notify ปิดบริการถาวรแล้ว (31 มี.ค. 2025) จึงใช้ **Messaging API** แทน

1. เข้า [LINE Developers Console](https://developers.line.biz/console/) → สร้าง **Provider** → สร้าง **Messaging API channel**
2. ในแท็บ **Messaging API** → กด **Issue** ที่ *Channel access token (long-lived)* → คัดลอกไว้ = `LINE_CHANNEL_ACCESS_TOKEN`
3. หา **ปลายทางที่จะส่งหา** (`LINE_TO`):
   - ส่งเข้าหาตัวเอง: แอดบอทเป็นเพื่อน แล้วเอา **Your user ID** จากแท็บ *Basic settings* (หรือใช้เว็บฮุคดึง userId)
   - ส่งเข้ากลุ่ม: เชิญบอทเข้ากลุ่ม แล้วใช้ **groupId**
4. ปิด **Auto-reply / Greeting** ในแท็บ Messaging API เพื่อไม่ให้บอทตอบรก

### 2.2 รันเซิร์ฟเวอร์
```bash
cd server
cp .env.example .env         # แล้วแก้ค่าใน .env ให้ครบ
pip install -r requirements.txt

# โหลดค่า env แล้วรัน (ตัวอย่างบน Linux/Mac)
export $(grep -v '^#' .env | xargs)
python app.py                # เปิดที่ http://localhost:8000
```
ทดสอบการเชื่อม LINE: เปิด `http://localhost:8000/test-line` — ควรมีข้อความเด้งเข้า LINE

### 2.3 ทำให้ TradingView ยิงเข้ามาได้ (ต้องมี URL สาธารณะ)
เลือกวิธีใดวิธีหนึ่ง:
- **ngrok** (ทดลองเร็ว): `ngrok http 8000` → เอา URL `https://xxxx.ngrok.app/webhook` ไปใส่ใน alert
- **Deploy ฟรี/ถูก**: Render, Railway, Fly.io — ใช้คำสั่งรัน `gunicorn app:app` และตั้ง Environment variables ตาม `.env`

### 2.4 เปิด Dashboard
เปิด `https://<โดเมนของคุณ>/` — จะเห็นการ์ดสัญญาณล่าสุดของแต่ละสัญญา อัปเดตอัตโนมัติทุก 15 วินาที

---

## ส่วนที่ 3 — ดูหน้าตา Dashboard ทันที
เปิดไฟล์ `dashboard-demo.html` ในเบราว์เซอร์ (ดับเบิลคลิกได้เลย) เพื่อดูหน้าตาจริงด้วยข้อมูลตัวอย่าง

---

## ตรรกะสัญญาณ (ปรับได้ทั้งหมดในหน้า ⚙️ ของ Pine Script)

ระบบ **2 จังหวะ** สำหรับฝั่งซื้อ:
- 🟡 **เตรียมซื้อ (เหลือง)**: **MACD ตัดเส้น Signal ขึ้น** — สัญญาณเตือนล่วงหน้า ยังไม่เข้า
- 🟢 **BUY (เขียว)**: **MACD ตัดเส้น 0 ขึ้น** — ยืนยันเข้าซื้อจริง
- 🔴 **SELL (แดง)**: **MACD ตัดเส้น 0 ลง** — สัญญาณขาย
- 🔵 **DCA (ย่อแล้วซื้อ)**: ราคาย่อจากจุดสูงล่าสุด ≥ % ที่ตั้ง (ค่าเริ่ม 3%) — ตั้งให้เข้าเฉพาะตอนเทรนด์ยังไม่พังได้
- **ลำดับความสำคัญของ alert**: SELL → BUY → เตรียมซื้อ → DCA → HOLD
- **ตัวช่วยวิเคราะห์บนกราฟ**:
  - **Fibonacci Retracement** อัตโนมัติจาก swing ล่าสุด (0.236 / 0.382 / 0.5 / 0.618 / 0.786) เส้น 0.618 เน้นสีส้ม
  - **Trend-based Fib Extension** อัตโนมัติจาก 3 จุด pivot ล่าสุด (0.618 / 1.0 / 1.618 / 2.618) ใช้หาเป้าราคา
  - ตารางบนกราฟบอกด้วยว่าราคาปัจจุบันอยู่ใกล้ระดับ Fib ไหน
  - RSI / EMA แสดงเป็น "บริบท" ประกอบการตัดสินใจ (ไม่ใช่ตัวยิงสัญญาณหลัก)

### เรื่อง DCA กับ futures (สำคัญ)
Futures ใช้ margin และมีวันหมดอายุ การ "ถัวเฉลี่ย" จึงต่างจากหุ้น — สัญญาณ DCA ในบอทนี้หมายถึง
**"จังหวะราคาย่อสำหรับพิจารณาเข้าเพิ่ม/เข้าไม้ต่อ"** ไม่ใช่การถัวไม่จำกัด ควรกำหนดขนาดไม้และจุดตัดขาดทุนเสมอ
และอย่าลืมว่าต้อง **roll สัญญา** ก่อนหมดอายุ (เช่น S50U26 → ซีรีส์ถัดไป)

---

## โครงไฟล์
```
tfex-signal-bot/
├─ tradingview/
│  └─ TFEX_Signal_Bot.pine     # ส่วนที่ 1: indicator + alert (วางใน Pine Editor)
├─ server/
│  ├─ app.py                   # ส่วนที่ 2: webhook → LINE + dashboard
│  ├─ requirements.txt
│  └─ .env.example             # คัดลอกเป็น .env แล้วใส่ค่า
├─ dashboard-demo.html         # ส่วนที่ 3: ดูหน้าตา dashboard ทันที
└─ README.md
```
