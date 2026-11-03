import base64, ctypes, json, math, os, queue, threading, time, urllib.parse, urllib.request, tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import ttk, messagebox

PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
EXCHANGES = {"NYSE", "NASDAQ", "AMEX"}
EXCLUDED_NAME_TERMS = (" etf", "fund", "warrant", "unit", "right", "preferred", "depositary", "depositary share", "notes", "bond")
POS = {"beats","beat","surge","record","upgrade","growth","profit","strong","buyback","raises","approval","approved","bullish","wins","revenue"}
NEG = {"miss","downgrade","plunge","loss","warning","warns","cuts","fraud","probe","investigation","lawsuit","recall","bankruptcy","bearish","weak","layoff"}
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AstraTradeAI")
os.makedirs(APP_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DIR, "astratrade.log")
STATE_FILE = os.path.join(APP_DIR, "bot_state.json")
MAX_MANAGED_POSITIONS = 5

def read_app_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def write_app_state(**updates):
    data = read_app_state()
    data.update(updates)
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp_file, STATE_FILE)

def load_managed_symbols():
    values = read_app_state().get("managed_symbols", [])
    return {str(x).upper() for x in values if isinstance(x, str) and x.strip()}

def save_managed_symbols(symbols):
    write_app_state(managed_symbols=sorted(set(symbols)))

CREDENTIAL_FILE = os.path.join(APP_DIR, "credentials.dat")

def _dpapi_transform(raw, protect=True):
    if os.name != "nt":
        raise RuntimeError("API anahtarlarını güvenli kaydetmek için Windows DPAPI gerekir.")
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    input_buffer = ctypes.create_string_buffer(raw)
    input_blob = DATA_BLOB(len(raw), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DATA_BLOB()
    if protect:
        crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptProtectData.restype = ctypes.c_bool
        ok = crypt32.CryptProtectData(ctypes.byref(input_blob), "AstraTrade AI Paper API", None, None, None, 1, ctypes.byref(output_blob))
    else:
        description = ctypes.c_wchar_p()
        crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptUnprotectData.restype = ctypes.c_bool
        ok = crypt32.CryptUnprotectData(ctypes.byref(input_blob), ctypes.byref(description), None, None, None, 1, ctypes.byref(output_blob))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))

def save_credentials(key, secret):
    payload = json.dumps({"key": key, "secret": secret}).encode("utf-8")
    encrypted = _dpapi_transform(payload, protect=True)
    temp_file = CREDENTIAL_FILE + ".tmp"
    with open(temp_file, "w", encoding="ascii") as f:
        f.write(base64.b64encode(encrypted).decode("ascii"))
    os.replace(temp_file, CREDENTIAL_FILE)

def load_credentials():
    try:
        with open(CREDENTIAL_FILE, "r", encoding="ascii") as f:
            encrypted = base64.b64decode(f.read().strip())
        payload = json.loads(_dpapi_transform(encrypted, protect=False).decode("utf-8"))
        return str(payload.get("key", "")), str(payload.get("secret", ""))
    except FileNotFoundError:
        return "", ""
    except Exception as exc:
        logline("Kayıtlı API bilgileri açılamadı: " + str(exc))
        return "", ""
def logline(msg):
    line = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception: pass
    return line

class Alpaca:
    def __init__(self, key, secret):
        if not key or not secret: raise ValueError("Paper API Key ve Secret Key gerekli.")
        self.headers = {"APCA-API-KEY-ID":key, "APCA-API-SECRET-KEY":secret, "Content-Type":"application/json"}
    def call(self, base, path, params=None, method="GET", body=None):
        url = base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    def account(self): return self.call(PAPER, "/v2/account")
    def assets(self): return self.call(PAPER, "/v2/assets", {"status":"active","asset_class":"us_equity"})
    def positions(self): return self.call(PAPER, "/v2/positions")
    def clock(self): return self.call(PAPER, "/v2/clock")
    def open_orders(self): return self.call(PAPER, "/v2/orders", {"status":"open","limit":500})
    def bars(self, symbols):
        # Alpaca's limit is shared across all symbols in a request and results are
        # ordered by symbol. Keep each request small enough to fit one year of daily
        # bars for every symbol, then still follow any unexpected page token.
        collected = {}
        for offset in range(0, len(symbols), 20):
            chunk = symbols[offset:offset + 20]
            params = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "limit": 10000,
                "feed": "iex",
                "start": (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(),
                "end": datetime.now(timezone.utc).isoformat(),
                "sort": "asc"
            }
            for _ in range(5):
                page = self.call(DATA, "/v2/stocks/bars", params)
                for symbol, rows in page.get("bars", {}).items():
                    collected.setdefault(symbol, []).extend(rows)
                token = page.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        return {"bars": collected}
    def history(self, symbol, timeframe="1Day", days=365):
        params = {
            "timeframe": timeframe,
            "start": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
            "end": datetime.now(timezone.utc).isoformat(),
            "limit": 10000,
            "feed": "iex",
            "sort": "asc"
        }
        rows = []
        for _ in range(5):
            page = self.call(DATA, "/v2/stocks/" + urllib.parse.quote(symbol.upper()) + "/bars", params)
            rows.extend(page.get("bars", []))
            token = page.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return rows

    def portfolio_history(self):
        return self.call(PAPER, "/v2/account/portfolio/history", {
            "period": "1M", "timeframe": "1D", "intraday_reporting": "market"
        })
    def order(self, symbol, qty, side, take_profit=None, stop_loss=None):
        body = {"symbol":symbol,"qty":str(qty),"side":side,"type":"market","time_in_force":"day"}
        if side == "buy" and take_profit is not None and stop_loss is not None:
            body.update({
                "time_in_force":"gtc",
                "order_class":"bracket",
                "take_profit":{"limit_price":f"{take_profit:.2f}"},
                "stop_loss":{"stop_price":f"{stop_loss:.2f}"}
            })
        req = urllib.request.Request(PAPER+"/v2/orders", data=json.dumps(body).encode(), headers=self.headers, method="POST")
        with urllib.request.urlopen(req, timeout=25) as r: return json.loads(r.read().decode())

def ema(values, n):
    if not values:
        return 0.0
    k = 2 / (n + 1)
    value = float(values[0])
    for x in values[1:]:
        value = k * float(x) + (1 - k) * value
    return value

def ema_series(values, n):
    if not values:
        return []
    k = 2 / (n + 1)
    value = float(values[0])
    result = [value]
    for x in values[1:]:
        value = k * float(x) + (1 - k) * value
        result.append(value)
    return result

def analyze(rows):
    # Long-only daily swing strategy; all indicators use the same price series.
    if len(rows) < 220:
        return None
    c = [float(x["c"]) for x in rows]
    h = [float(x["h"]) for x in rows]
    l = [float(x["l"]) for x in rows]
    o = [float(x["o"]) for x in rows]
    v = [float(x["v"]) for x in rows]
    price = c[-1]
    e20 = ema(c[-100:], 20)
    e50 = ema(c[-150:], 50)
    sma200 = sum(c[-200:]) / 200
    macd_line = [a - b for a, b in zip(ema_series(c, 12), ema_series(c, 26))]
    macd_signal = ema(macd_line[-40:], 9)
    macd_hist = macd_line[-1] - macd_signal
    gains, losses = [], []
    for i in range(len(c)-14, len(c)):
        change = c[i] - c[i-1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    if avg_loss == 0:
        rsi = 100.0 if avg_gain else 50.0
    else:
        rsi = 100 - 100 / (1 + avg_gain / avg_loss)
    true_ranges = []
    for i in range(len(c)-14, len(c)):
        true_ranges.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    atr = sum(true_ranges) / len(true_ranges)
    avg_volume = sum(v[-21:-1]) / 20
    relative_volume = (v[-1] / avg_volume) if avg_volume > 0 else 0.0
    dollar_vol = sum(c[-20+i] * v[-20+i] for i in range(20)) / 20
    prior_20_high = max(h[-21:-1])
    conditions = {
        "uzun_trend": price > sma200,
        "trend_sirasi": e20 > e50,
        "macd_pozitif": macd_hist > 0,
        "rsi_uygun": 50 <= rsi <= 68,
        "20_gun_tepe": price > prior_20_high,
        "hacim_onayi": relative_volume >= 1.10,
        "likidite": dollar_vol >= 10_000_000
    }
    score = sum(1 for value in conditions.values() if value) / len(conditions)
    trend_ok = conditions["uzun_trend"] and conditions["trend_sirasi"] and conditions["macd_pozitif"]
    action = "BUY" if trend_ok and score >= 6/7 else ("SELL" if price < e20 and macd_hist < 0 and rsi < 45 else "HOLD")
    return {
        "price": price, "open": o[-1], "ema20": e20, "ema50": e50,
        "sma200": sma200, "macd_hist": macd_hist, "rsi": rsi,
        "atr": atr, "dollar_vol": dollar_vol, "relative_volume": relative_volume,
        "score": score, "action": action, "conditions": conditions
    }
def news_score(items):
    scores={}
    for item in items:
        words={w.strip(".,:;!?()[]{}'\\\"").lower() for w in (str(item.get("headline",""))+" "+str(item.get("summary",""))).split()}
        s=max(-1,min(1,(len(words&POS)-len(words&NEG))/3))
        for sym in item.get("symbols",[]): scores.setdefault(sym,[]).append(s)
    return {sym:sum(vals)/len(vals) for sym,vals in scores.items()}

class Bot:
    def __init__(self, broker, events, stop):
        self.broker = broker
        self.events = events
        self.stop = stop
        self.managed_symbols = load_managed_symbols()

    def run(self):
        self.broker.account()
        assets = self.broker.assets()
        eligible = set()
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            exchange = str(asset.get("exchange", "")).upper()
            symbol = str(asset.get("symbol", "")).strip()
            is_common = "common stock" in name or "common shares" in name
            excluded = any(term in name for term in EXCLUDED_NAME_TERMS)
            if symbol and asset.get("tradable") and str(asset.get("status", "")).lower() == "active" and exchange in EXCHANGES and is_common and not excluded:
                eligible.add(symbol)
        symbols = sorted(eligible)
        if not symbols:
            raise RuntimeError("Alpaca'dan uygun ABD adi hissesi alınamadı.")
        self.events.put(("log", f"Tarama evreni: {len(symbols)} aktif NYSE/NASDAQ/AMEX adi hissesi."))
        batch_size = 200
        scan_interval = 300
        offset = 0
        self.events.put(("log", "Strateji: uzun trend + EMA20/50 + MACD + RSI + 20 günlük kırılım + hacim teyidi."))
        self.events.put(("log", "Risk motoru: işlem başına %0,25 risk, pozisyon başına azami %7, toplam yönetilen maruziyet %30, günlük zarar sınırı %1,5, zirveden düşüş sınırı %5."))
        while not self.stop.is_set():
            batch = symbols[offset:offset+batch_size]
            offset = (offset + batch_size) % len(symbols)
            try:
                bars = self.broker.bars(batch).get("bars", {})
                bar_counts = {sym: len(bars.get(sym, [])) for sym in batch}
                sufficient_history = sum(1 for count in bar_counts.values() if count >= 220)
                total_daily_bars = sum(bar_counts.values())
                samples = ", ".join(f"{sym}:{bar_counts[sym]}" for sym in batch[:8])
                spy_rows = self.broker.bars(["SPY"]).get("bars", {}).get("SPY", [])
                spy = analyze(spy_rows)
                market_ok = bool(spy and spy["price"] > spy["sma200"] and spy["ema50"] > spy["sma200"])
                positions = self.broker.positions()
                held = {p["symbol"]: p for p in positions}
                open_orders = self.broker.open_orders()
                pending = {o["symbol"] for o in open_orders}
                active_symbols = set(held) | pending
                self.managed_symbols.intersection_update(active_symbols)
                save_managed_symbols(self.managed_symbols)
                managed_active = set(self.managed_symbols)
                reserved = set(held) | pending
                external_positions = set(held) - managed_active
                account = self.broker.account()
                clock = self.broker.clock()
                equity = float(account.get("equity", 0) or 0)
                cash = float(account.get("cash", 0) or 0)
                last_equity = float(account.get("last_equity", equity) or equity)
                daily_return = (equity-last_equity)/last_equity if last_equity else 0.0
                state = read_app_state()
                peak_equity = float(state.get("peak_equity", equity) or equity)
                if equity > peak_equity:
                    peak_equity = equity
                    write_app_state(peak_equity=peak_equity)
                peak_drawdown = (equity-peak_equity)/peak_equity if peak_equity else 0.0
                risk_allowed = daily_return > -0.015 and peak_drawdown > -0.05
                managed_exposure = sum(abs(float(p.get("market_value", 0) or 0)) for s, p in held.items() if s in managed_active)
                self.events.put(("log", f"Hesap: {len(held)} pozisyon | AstraTrade: {len(managed_active)}/5 | eski/harici: {len(external_positions)} | günlük değişim: {daily_return*100:.2f}% | zirveden düşüş: {peak_drawdown*100:.2f}%."))
                if not market_ok:
                    self.events.put(("log", "PİYASA FİLTRESİ: SPY uzun vadeli trendi olumlu değil veya veri yetersiz; yeni alışlar bekletiliyor. Mevcut AstraTrade pozisyonları yine izleniyor."))
                if not risk_allowed:
                    self.events.put(("log", "RİSK KİLİDİ: Günlük %1,5 zarar veya portföy zirvesinden %5 düşüş sınırı aşıldı; yeni alışlar durduruldu. Satış/çıkış sinyalleri izlenmeye devam ediyor."))
                scanned = 0
                for sym in batch:
                    if self.stop.is_set():
                        break
                    f = analyze(bars.get(sym, []))
                    if not f or f["price"] < 5:
                        continue
                    action = f["action"]
                    if action == "BUY" and f["dollar_vol"] < 10_000_000:
                        action = "HOLD"
                    scanned += 1
                    self.events.put(("decision", (sym, action, f["score"], f["price"], f["rsi"])))
                    if not clock.get("is_open") or sym in pending:
                        continue
                    if action == "BUY" and market_ok and risk_allowed and sym not in reserved and len(managed_active) < MAX_MANAGED_POSITIONS:
                        distance = max(2.0*f["atr"], f["price"]*0.015)
                        if distance <= 0 or distance >= f["price"]*0.25:
                            continue
                        max_risk_dollars = equity * 0.0025
                        managed_room = max(0.0, equity*0.30-managed_exposure)
                        qty_by_risk = int(max_risk_dollars/distance)
                        qty_by_position = int(equity*0.07/f["price"])
                        qty_by_exposure = int(managed_room/f["price"])
                        qty_by_cash = int(max(cash, 0)*0.98/f["price"])
                        qty = max(0, min(qty_by_risk, qty_by_position, qty_by_exposure, qty_by_cash))
                        if qty:
                            stop_price = round(f["price"]-distance, 2)
                            take_profit_price = round(f["price"]+3.0*distance, 2)
                            result = self.broker.order(sym, qty, "buy", take_profit=take_profit_price, stop_loss=stop_price)
                            pending.add(sym)
                            reserved.add(sym)
                            managed_active.add(sym)
                            self.managed_symbols.add(sym)
                            save_managed_symbols(self.managed_symbols)
                            managed_exposure += qty*f["price"]
                            cash = max(0.0, cash-qty*f["price"])
                            self.events.put(("log", f"ASTRATRADE PAPER ALIŞ {sym} x{qty} | skor {f['score']*100:.0f}/100 | risk USD {qty*distance:.2f} | SL {stop_price:.2f} | TP {take_profit_price:.2f} | {result.get('status')}"))
                    elif action == "SELL" and sym in held and sym in self.managed_symbols:
                        qty = abs(float(held[sym].get("qty", 0) or 0))
                        if qty:
                            result = self.broker.order(sym, qty, "sell")
                            pending.add(sym)
                            self.events.put(("log", f"ASTRATRADE PAPER ÇIKIŞ {sym} x{qty:g} | {result.get('status')}"))
                self.events.put(("status", (len(symbols), scanned, equity, cash, len(held), bool(clock.get("is_open")))))
                data_symbols = sum(1 for count in bar_counts.values() if count > 0)
                self.events.put(("log", f"Veri: {data_symbols}/{len(batch)} sembol; {total_daily_bars} günlük bar; 220+ bar geçmişi olan: {sufficient_history}. SPY piyasa filtresi: {'OLUMLU' if market_ok else 'OLUMSUZ'}."))
                self.events.put(("log", f"Veri örneği: {samples}"))
                if data_symbols == 0:
                    self.events.put(("log", "VERİ UYARISI: IEX piyasa verisi bu grupta boş döndü."))
                self.events.put(("log", f"Tarama tamamlandı: {scanned} hisse analiz edildi. Piyasa: {'açık' if clock.get('is_open') else 'kapalı'}."))
            except Exception as exc:
                self.events.put(("log", "Tarama hatası: " + str(exc)))
            self.stop.wait(scan_interval)
        self.events.put(("log", "Bot durduruldu."))
class App:
    def __init__(self, root):
        self.root=root; root.title("AstraTrade AI"); root.geometry("950x650"); root.configure(bg="#111827")
        self.events=queue.Queue(); self.stop_event=threading.Event(); self.thread=None; self.broker=None
        self.build(); root.after(250,self.poll); root.protocol("WM_DELETE_WINDOW",self.close)
    def build(self):
        bg="#111827"; panel="#1f2d42"; green="#36c99a"
        head=tk.Frame(self.root,bg=bg); head.pack(fill="x",padx=20,pady=18)
        tk.Label(head,text="ASTRATRADE AI",bg=bg,fg="white",font=("Segoe UI",22,"bold")).pack(side="left")
        tk.Label(head,text="US EQUITIES • PAPER ONLY • RULE-BASED",bg=bg,fg=green).pack(side="left",padx=15)
        self.connection=tk.Label(head,text="● BAĞLI DEĞİL",bg=bg,fg="#ffc36d"); self.connection.pack(side="right")
        tabs=ttk.Notebook(self.root); tabs.pack(fill="both",expand=True,padx=20,pady=(0,15)); self.tabs=tabs
        home=tk.Frame(tabs,bg=bg); settings=tk.Frame(tabs,bg=bg); history=tk.Frame(tabs,bg=bg)
        tabs.add(home,text="Kontrol Paneli"); tabs.add(settings,text="Alpaca Bağlantısı"); tabs.add(history,text="İşlemler"); self.settings=settings
        self.metrics={}
        row=tk.Frame(home,bg=bg); row.pack(fill="x",pady=15)
        for key,title in [("equity","SANAL HESAP"),("cash","NAKİT"),("positions","AÇIK POZİSYON")]:
            card=tk.Frame(row,bg=panel,padx=15,pady=12); card.pack(side="left",fill="x",expand=True,padx=5)
            tk.Label(card,text=title,bg=panel,fg="#9caec1").pack(anchor="w")
            value=tk.Label(card,text="—",bg=panel,fg="white",font=("Segoe UI",18,"bold")); value.pack(anchor="w"); self.metrics[key]=value
        bar=tk.Frame(home,bg=panel,padx=12,pady=12); bar.pack(fill="x",pady=10)
        tk.Label(bar,text="Otomatik sanal işlem",bg=panel,fg="white").pack(side="left")
        self.start_button=tk.Button(bar,text="BOTU BAŞLAT",command=self.start,bg=green,fg="#07131a",relief="flat",padx=18,pady=9); self.start_button.pack(side="right")
        self.stop_button=tk.Button(bar,text="DURDUR",command=self.stop_bot,bg="#a13e4a",fg="white",relief="flat",padx=18,pady=9,state="disabled"); self.stop_button.pack(side="right",padx=8)
        self.market=tk.Label(home,text="Piyasa: — | Tarama evreni: —",bg=bg,fg="#9caec1"); self.market.pack(anchor="w")
        tk.Label(home,text="BOT FAALİYETLERİ",bg=bg,fg="white").pack(anchor="w",pady=(10,3))
        self.log=tk.Text(home,bg="#090f1a",fg="#dbe5ef",font=("Consolas",9),relief="flat"); self.log.pack(fill="both",expand=True)
        box=tk.Frame(settings,bg=panel,padx=22,pady=22); box.pack(fill="x",padx=18,pady=18)
        tk.Label(box,text="Alpaca Paper Trading",bg=panel,fg="white",font=("Segoe UI",15,"bold")).pack(anchor="w")
        tk.Label(box,text="YALNIZCA Paper API anahtarlarını gir. Gerçek hesap anahtarlarını kullanma.",bg=panel,fg="#ffc36d").pack(anchor="w",pady=12)
        tk.Label(box,text="Paper API Key",bg=panel,fg="white").pack(anchor="w"); self.key=tk.Entry(box,font=("Consolas",10)); self.key.pack(fill="x",pady=5)
        tk.Label(box,text="Paper Secret Key",bg=panel,fg="white").pack(anchor="w"); self.secret=tk.Entry(box,show="*",font=("Consolas",10)); self.secret.pack(fill="x",pady=5)
        self.test_button=tk.Button(box,text="BAĞLANTIYI TEST ET",command=self.test_connection,bg=green,fg="#07131a",relief="flat",padx=15,pady=9); self.test_button.pack(anchor="w",pady=12)
        self.result=tk.Label(settings,text="Henüz bağlantı test edilmedi.",bg=bg,fg="#9caec1"); self.result.pack(anchor="w",padx=20)
        tk.Label(settings,text="Strateji: EMA / RSI / MACD ve basit haber kelime puanlaması; bu sürüm makine öğrenmesi modeli değildir.",bg=bg,fg="#9caec1",wraplength=850,justify="left").pack(anchor="w",padx=20,pady=(15,5))
        tk.Label(settings,text="Alışlar için Alpaca bracket emriyle yaklaşık 1,5 ATR zarar-kes ve 2R kâr-al seviyesi gönderilir. Nakit bakiyesi dışına çıkılmaz. AstraTrade bu kurulumda yalnızca kendi açtığı pozisyonları yönetir; eski botlardan/manual hesaptan gelen pozisyonları otomatik satmaz ve kendi 5 pozisyon sınırına saymaz. İşlem başına yaklaşık %0,5 risk hedeflenir. Bu korumalar kaybı garanti olarak sınırlamaz.",bg=bg,fg="#ffc36d",wraplength=850,justify="left").pack(anchor="w",padx=20,pady=5)
        tk.Label(settings,text="Ücretsiz veri IEX akışıdır. Tarama evreni yalnızca adı Common Stock / Common Shares olan aktif NYSE, NASDAQ ve NYSE American adi hisseleridir. ETF, kaldıraçlı/ters fon, varant, imtiyazlı pay, birim ve tahvil benzeri varlıklar dışarıda bırakılır. Alış için ayrıca 20 günlük ortalama günlük dolar hacmi en az 5 milyon USD olmalıdır. Her 5 dakikada 200 sembol taranır. Gerçek para hesabı bu sürümde desteklenmez.",bg=bg,fg="#9caec1",wraplength=850,justify="left").pack(anchor="w",padx=20,pady=5)
        self.tree=ttk.Treeview(history,columns=("time","symbol","side","qty","status"),show="headings")
        for col,title,width in [("time","ZAMAN",180),("symbol","HİSSE",100),("side","YÖN",100),("qty","ADET",100),("status","DURUM",180)]:
            self.tree.heading(col,text=title); self.tree.column(col,width=width,anchor="center")
        self.tree.pack(fill="both",expand=True,padx=15,pady=15)
        tk.Button(history,text="İŞLEMLERİ YENİLE",command=self.refresh_history).pack(anchor="e",padx=15,pady=10)
    def add_log(self,msg):
        line=logline(msg); self.log.insert("end",line+"\n"); self.log.see("end")
    def test_connection(self):
        key=self.key.get().strip(); secret=self.secret.get().strip()
        if not key or not secret: messagebox.showwarning("Eksik bilgi","Paper API Key ve Secret Key gir."); return
        self.test_button.configure(state="disabled")
        def work():
            try:
                broker=Alpaca(key,secret); account=broker.account(); self.events.put(("connected",(broker,account)))
            except Exception as exc: self.events.put(("error",str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def start(self):
        if not self.key.get().strip() or not self.secret.get().strip():
            self.tabs.select(self.settings); messagebox.showwarning("Bağlantı","Önce Paper API bilgilerini gir."); return
        if self.thread and self.thread.is_alive(): return
        self.broker=Alpaca(self.key.get().strip(),self.secret.get().strip()); self.stop_event.clear()
        bot=Bot(self.broker,self.events,self.stop_event); self.thread=threading.Thread(target=self.run,args=(bot,),daemon=True); self.thread.start()
        self.start_button.configure(state="disabled"); self.stop_button.configure(state="normal"); self.connection.configure(text="● BOT ÇALIŞIYOR",fg=green)
    def run(self,bot):
        try: bot.run()
        except Exception as exc: self.events.put(("fatal",str(exc)))
    def stop_bot(self):
        self.stop_event.set(); self.stop_button.configure(state="disabled"); self.add_log("Durdurma isteği gönderildi.")
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=="log": self.add_log(value)
                elif kind=="connected":
                    self.broker,account=value; self.connection.configure(text="● ALPACA PAPER BAĞLI",fg="#36c99a")
                    self.result.configure(text=f"Bağlantı başarılı | Hesap: {account.get('status')} | Sanal bakiye: USD {float(account.get('equity',0)):,.2f}",fg="#36c99a")
                    self.metrics["equity"].configure(text=f"USD {float(account.get('equity',0)):,.2f}"); self.metrics["cash"].configure(text=f"USD {float(account.get('cash',0)):,.2f}")
                    self.test_button.configure(state="normal"); self.add_log("Alpaca Paper bağlantısı doğrulandı.")
                elif kind=="error":
                    self.result.configure(text="Bağlantı hatası: "+value,fg="#ff7373"); self.test_button.configure(state="normal"); messagebox.showerror("Alpaca",value)
                elif kind=="decision":
                    sym,action,score,price,rsi=value; self.add_log(f"{sym} | {action} | skor {score:.2f} | USD {price:.2f} | RSI {rsi:.1f}")
                elif kind=="status":
                    n,count,equity,cash,positions,market=value
                    self.metrics["equity"].configure(text=f"USD {equity:,.2f}"); self.metrics["cash"].configure(text=f"USD {cash:,.2f}"); self.metrics["positions"].configure(text=str(positions))
                    self.market.configure(text=f"Piyasa: {'Açık' if market else 'Kapalı'} | Hisse evreni: {n} | Bu tur: {count}")
                elif kind=="history":
                    for item in self.tree.get_children(): self.tree.delete(item)
                    for order in value:self.tree.insert("","end",values=(str(order.get("submitted_at",""))[:16],order.get("symbol",""),order.get("side","").upper(),order.get("qty",""),order.get("status","")))
                elif kind=="fatal":
                    self.add_log("HATA: "+value); self.connection.configure(text="● HATA",fg="#ff7373"); self.start_button.configure(state="normal"); self.stop_button.configure(state="disabled"); messagebox.showerror("Bot hatası",value)
        except queue.Empty: pass
        if self.thread and not self.thread.is_alive():
            self.start_button.configure(state="normal"); self.stop_button.configure(state="disabled")
        self.root.after(250,self.poll)
    def refresh_history(self):
        if not self.broker: messagebox.showinfo("Bilgi","Önce hesaba bağlan."); return
        def work():
            try:self.events.put(("history",self.broker.call(PAPER,"/v2/orders",{"status":"all","limit":100,"direction":"desc"})))
            except Exception as exc:self.events.put(("log","İşlem geçmişi hatası: "+str(exc)))
        threading.Thread(target=work,daemon=True).start()
    def close(self):
        if self.thread and self.thread.is_alive() and not messagebox.askyesno("Bot açık","Kapatınca bot duracak. Kapatılsın mı?"): return
        self.stop_event.set(); self.root.destroy()

if __name__=="__main__":
    root=tk.Tk(); App(root); root.mainloop()
