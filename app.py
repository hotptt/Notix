import os, re, time, asyncio
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import aiosqlite
import httpx

DB_PATH = os.getenv("DB_PATH", "data.db")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # Bot token
UPBIT_MARKETS_URL = "https://api.upbit.com/v1/market/all?isDetails=false"
UPBIT_TICKER_URL = "https://api.upbit.com/v1/ticker?markets={markets}"
DISCORD_API_BASE = "https://discord.com/api/v10"

app = FastAPI(title="Upbit Alerts (Bot Token, Web only)")
# ⚠️ static 폴더 마운트 없음 (폴더 없이 루트에 index.html만 둠)

@app.get("/api/health")
def health():
    return {"ok": True}

_MARKETS_CACHE: Dict[str, Any] = {"items": [], "ts": 0, "ttl": 300}

CREATE_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS trackers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT NOT NULL,
  avg_price REAL NOT NULL,
  up_threshold REAL NOT NULL,
  down_threshold REAL NOT NULL,
  channel_id TEXT NOT NULL,
  UNIQUE (market, channel_id)
);
CREATE TABLE IF NOT EXISTS last_alert (
  market TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  last_state TEXT NOT NULL,
  last_ts INTEGER NOT NULL,
  PRIMARY KEY (market, channel_id)
);
"""

async def init_db():
  os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
  async with aiosqlite.connect(DB_PATH) as db:
    for stmt in [s.strip() for s in CREATE_SQL.strip().split(";") if s.strip()]:
      await db.execute(stmt)
    await db.commit()

class TrackIn(BaseModel):
  market:str
  avg_price:float
  up_threshold:float
  down_threshold:float
  channel_id:str

def normalize_market(s: str) -> Optional[str]:
  if not s: return None
  s = s.strip().upper()
  if re.fullmatch(r"[A-Z0-9]{2,10}", s):
    return f"KRW-{s}"
  if re.fullmatch(r"[A-Z]{3,5}-[A-Z0-9]{2,15}", s):
    return s
  return None

async def fetch_upbit_markets() -> List[Dict[str, str]]:
  now = int(time.time())
  if _MARKETS_CACHE["items"] and now - _MARKETS_CACHE["ts"] < _MARKETS_CACHE["ttl"]:
    return _MARKETS_CACHE["items"]
  headers = {"User-Agent": "UpbitAlertBot/1.0 (+https://render.com)"}
  async with httpx.AsyncClient(timeout=10, headers=headers) as client:
    last_err = None
    for _ in range(3):
      try:
        r = await client.get(UPBIT_MARKETS_URL)
        r.raise_for_status()
        data = r.json()
        out = []
        for it in data:
          m = it.get("market")
          if m and m.startswith("KRW-"):
            name = it.get("korean_name") or it.get("english_name") or m.split("-",1)[1]
            out.append({"market": m, "name": name})
        out.sort(key=lambda x: x["market"])
        _MARKETS_CACHE["items"] = out
        _MARKETS_CACHE["ts"] = now
        return out
      except Exception as e:
        last_err = e
        await asyncio.sleep(0.8)
    raise last_err

@app.get("/api/markets")
async def markets():
  try:
    return await fetch_upbit_markets()
  except Exception as e:
    return JSONResponse(status_code=502, content={"ok": False, "msg": f"Upbit fetch failed: {type(e).__name__} {e}"})

@app.post("/api/track")
async def track(t: TrackIn):
  if not DISCORD_TOKEN:
    raise HTTPException(500, "DISCORD_TOKEN not set on server")
  m = normalize_market(t.market)
  if not m:
    raise HTTPException(400, "Invalid market")
  if t.up_threshold <= 0 or t.down_threshold >= 0:
    raise HTTPException(400, "up must be > 0 and down must be < 0")
  if not re.fullmatch(r"\d{15,25}", t.channel_id):
    raise HTTPException(400, "Invalid channel_id")
  await init_db()
  async with aiosqlite.connect(DB_PATH) as db:
    await db.execute(
      "INSERT INTO trackers (market, avg_price, up_threshold, down_threshold, channel_id) "
      "VALUES (?,?,?,?,?) "
      "ON CONFLICT(market, channel_id) DO UPDATE SET avg_price=excluded.avg_price, up_threshold=excluded.up_threshold, down_threshold=excluded.down_threshold",
      (m, float(t.avg_price), float(t.up_threshold), float(t.down_threshold), t.channel_id)
    )
    await db.commit()
  return {"ok": True, "market": m}

def fmt_price(x: float) -> str:
  return f"{x:,.0f}₩" if x >= 100 else f"{x:,.2f}₩"

def fmt_pct(x: float) -> str:
  return f"{x:+.2f}%"

async def fetch_tickers(markets: List[str]) -> Dict[str, Any]:
  out: Dict[str, Any] = {}
  if not markets: return out
  headers = {"User-Agent": "UpbitAlertBot/1.0 (+https://render.com)"}
  async with httpx.AsyncClient(timeout=10, headers=headers) as client:
    CHUNK = 30
    for i in range(0, len(markets), CHUNK):
      part = markets[i:i+CHUNK]
      url = UPBIT_TICKER_URL.format(markets=",".join(part))
      r = await client.get(url)
      r.raise_for_status()
      data = r.json()
      for item in data:
        m = item.get("market")
        out[m] = item
  return out

async def send_discord_message(channel_id: str, embed: Dict[str, Any]):
  headers = {"Authorization": f"Bot {os.getenv('DISCORD_TOKEN')}"}
  payload = {"embeds": [embed]}
  async with httpx.AsyncClient(timeout=15) as client:
    while True:
      resp = await client.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages", headers=headers, json=payload)
      if resp.status_code == 429:
        data = resp.json()
        wait = data.get("retry_after", 1.0)
        await asyncio.sleep(float(wait) + 0.1)
        continue
      resp.raise_for_status()
      break

async def poller():
  await init_db()
  await asyncio.sleep(1)
  print("[poller] started; interval", POLL_INTERVAL)
  while True:
    try:
      async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT market, avg_price, up_threshold, down_threshold, channel_id FROM trackers")
        rows = await cur.fetchall()
      if not rows:
        await asyncio.sleep(POLL_INTERVAL); continue

      markets = sorted({r[0] for r in rows})
      tick = await fetch_tickers(markets)

      now = int(time.time())
      async with aiosqlite.connect(DB_PATH) as db:
        for market, avg, up_th, down_th, chan in rows:
          info = tick.get(market)
          if not info: continue
          price = float(info.get("trade_price") or 0.0)
          if price<=0 or float(avg)<=0: continue
          pct = (price-float(avg))/float(avg)*100.0
          st = "above" if pct>=float(up_th) else "below" if pct<=float(down_th) else "neutral"

          cur2 = await db.execute("SELECT last_state FROM last_alert WHERE market=? AND channel_id=?", (market, chan))
          prev = await cur2.fetchone()

          should=False
          if st in ("above","below"):
            if prev is None or prev[0]!=st:
              should=True
          if should:
            emb = {
              "title": f"{'📈' if st=='above' else '📉'} {market} {'상승' if st=='above' else '하락'} 알림",
              "description": f"현재가: **{fmt_price(price)}**\n평단가: **{fmt_price(float(avg))}**\n변화율: **{fmt_pct(pct)}** (↑{fmt_pct(float(up_th))} / ↓{fmt_pct(float(down_th))})",
              "color": 0x22c55e if st=='above' else 0xef4444,
              "footer": {"text":"업비트 알림 · Render Web Service (Bot Token)"},
            }
            try:
              await send_discord_message(chan, emb)
              await db.execute(
                "INSERT INTO last_alert (market, channel_id, last_state, last_ts) VALUES (?,?,?,?) "
                "ON CONFLICT(market, channel_id) DO UPDATE SET last_state=excluded.last_state, last_ts=excluded.last_ts",
                (market, chan, st, now)
              )
              await db.commit()
            except Exception as e:
              print("[poller] discord send error:", e)
      await asyncio.sleep(POLL_INTERVAL)
    except Exception as e:
      print("[poller] loop error:", e)
      await asyncio.sleep(POLL_INTERVAL)

@app.get("/")
def index():
  # 루트에 있는 index.html을 그대로 서빙
  if os.path.exists("index.html"):
    return FileResponse("index.html")
  # index.html이 없어도 최소한 살아있게
  return JSONResponse({"ok": True, "msg": "Upload index.html at repo root."})

@app.on_event("startup")
async def on_start():
  if not DISCORD_TOKEN:
    print("[warn] DISCORD_TOKEN not set; /api/track will 500")
  asyncio.create_task(poller())
