import json, math, os, queue, threading, time, urllib.parse, urllib.request, tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import ttk, messagebox

PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "NYSEARCA", "BATS"}
POS = {"beats","beat","surge","record","upgrade","growth","profit","strong","buyback","raises","approval","approved","bullish","wins","revenue"}
NEG = {"miss","downgrade","plunge","loss","warning","warns","cuts","fraud","probe","investigation","lawsuit","recall","bankruptcy","bearish","weak","layoff"}
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AstraTradeAI")
os.makedirs(APP_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DIR, "astratrade.log")

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
    def news(self, symbols):
        start = (datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
        return self.call(DATA, "/v1beta1/news", {"symbols":",".join(symbols),"start":start,"limit":50})
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
    k=2/(n+1); v=values[0]
    for x in values[1:]: v=k*x+(1-k)*v
    return v

def analyze(rows):
    if len(rows)<55: return None
    c=[float(x["c"]) for x in rows]; h=[float(x["h"]) for x in rows]; l=[float(x["l"]) for x in rows]; v=[float(x["v"]) for x in rows]
    price=c[-1]; e20=ema(c[-100:],20); e50=ema(c[-150:],50); macd=ema(c[-80:],12)-ema(c[-100:],26)
    d=[c[i]-c[i-1] for i in range(len(c)-14,len(c))]
    gain=sum(max(0,x) for x in d)/14; loss=sum(max(0,-x) for x in d)/14
    if gain == 0 and loss == 0: rsi=50
    elif loss == 0: rsi=100
    elif gain == 0: rsi=0
    else: rsi=100-100/(1+gain/loss)
    tr=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(len(c)-14,len(c))]
    atr=sum(tr)/14; dollar_vol=sum(c[-20+i]*v[-20+i] for i in range(20))/20
    ret20=price/c[-21]-1
    score=0.0
    if price>e20>e50: score+=.30
    if macd>0: score+=.20
    if 45<=rsi<=68: score+=.15
    if dollar_vol>=5_000_000: score+=.15
    if ret20>0: score+=.10
    score+=.10*(1 if macd>0 and ret20>0 else .5)
    action="BUY" if score>=.75 else ("SELL" if price<e20 and macd<0 else "HOLD")
    return {"price":price,"ema20":e20,"ema50":e50,"macd":macd,"rsi":rsi,"atr":atr,"score":score,"action":action}

def news_score(items):
    scores={}
    for item in items:
        words={w.strip(".,:;!?()[]{}'\\\"").lower() for w in (str(item.get("headline",""))+" "+str(item.get("summary",""))).split()}
        s=max(-1,min(1,(len(words&POS)-len(words&NEG))/3))
        for sym in item.get("symbols",[]): scores.setdefault(sym,[]).append(s)
    return {sym:sum(vals)/len(vals) for sym,vals in scores.items()}

class Bot:
    def __init__(self, broker, events, stop): self.broker=broker; self.events=events; self.stop=stop
    def run(self):
        self.broker.account()
        assets=self.broker.assets()
        symbols=sorted({a["symbol"] for a in assets if a.get("tradable") and str(a.get("status","")).lower().endswith("active") and str(a.get("exchange","")).upper() in EXCHANGES})
        if not symbols: raise RuntimeError("Alpaca uygun ABD hissesi listesi döndürmedi.")
        batch_size=200
        scan_interval=300
        self.events.put(("log",f"Bağlantı başarılı. Uygun hisse evreni: {len(symbols)}. Her turda {batch_size} hisse taranır; tur aralığı 5 dakika."))
        offset=0
        while not self.stop.is_set():
            batch=symbols[offset:offset+batch_size]
            offset=(offset+batch_size)%len(symbols)
            try:
                bars=self.broker.bars(batch).get("bars",{})
                bar_counts={sym:len(bars.get(sym,[])) for sym in batch}
                data_symbols=sum(1 for count in bar_counts.values() if count>0)
                sufficient_history=sum(1 for count in bar_counts.values() if count>=55)
                total_daily_bars=sum(bar_counts.values())
                samples=", ".join(f"{sym}:{bar_counts[sym]}" for sym in batch[:8])
                try: sentiments=news_score(self.broker.news(batch).get("news",[]))
                except Exception: sentiments={}
                positions=self.broker.positions(); held={p["symbol"]:p for p in positions}
                open_orders=self.broker.open_orders()
                pending={o["symbol"] for o in open_orders}
                pending_buys={o["symbol"] for o in open_orders if str(o.get("side","")).lower()=="buy"}
                reserved=set(held) | pending_buys
                account=self.broker.account(); clock=self.broker.call(PAPER,"/v2/clock")
                equity=float(account["equity"]); cash=float(account.get("cash",0))
                last=float(account.get("last_equity",equity))
                daily=(equity-last)/last if last else 0
                scanned=0
                for sym in batch:
                    if self.stop.is_set(): break
                    rows=bars.get(sym,[])
                    f=analyze(rows)
                    if not f or f["price"]<5: continue
                    sentiment=sentiments.get(sym,0.0)
                    score=max(0,min(1,f["score"]+.05*sentiment))
                    action="BUY" if score>=.75 else ("SELL" if f["action"]=="SELL" else "HOLD")
                    scanned+=1
                    self.events.put(("decision",(sym,action,score,f["price"],f["rsi"])))
                    if not clock.get("is_open") or sym in pending: continue
                    if action=="BUY" and sym not in reserved and len(reserved)<5 and daily>-.02:
                        distance=max(1.5*f["atr"],f["price"]*.01)
                        if distance >= f["price"]*.80:
                            continue
                        qty=max(0,min(int(equity*.005/distance),int(equity*.10/f["price"]),int(max(cash,0)*.98/f["price"])))
                        if qty:
                            stop_price=round(f["price"]-distance,2)
                            take_profit_price=round(f["price"]+2*distance,2)
                            result=self.broker.order(sym,qty,"buy",take_profit=take_profit_price,stop_loss=stop_price)
                            pending.add(sym)
                            pending_buys.add(sym)
                            reserved.add(sym)
                            held[sym]={"symbol":sym,"qty":str(qty)}
                            cash=max(0.0,cash-qty*f["price"])
                            self.events.put(("log",f"SANAL ALIM {sym} x{qty} | skor {score:.2f} | SL {stop_price:.2f} | TP {take_profit_price:.2f} | {result.get('status')}"))
                    elif action=="SELL" and sym in held:
                        qty=abs(float(held[sym]["qty"]))
                        if qty:
                            result=self.broker.order(sym,qty,"sell"); pending.add(sym)
                            self.events.put(("log",f"SANAL SATIŞ {sym} x{qty:g} | {result.get('status')}"))
                self.events.put(("status",(len(symbols),scanned,equity,float(account["cash"]),len(held),bool(clock.get("is_open")))))
                self.events.put(("log",f"Veri yanıtı: {data_symbols}/{len(batch)} sembol; toplam günlük bar: {total_daily_bars}; en az 55 günlük geçmişi olan: {sufficient_history}."))
                self.events.put(("log",f"Veri örneği (sembol:bar): {samples}"))
                if data_symbols == 0:
                    self.events.put(("log","VERİ UYARISI: IEX akışından bu gruptaki semboller için bar gelmedi. Paper API anahtarı çalışsa bile piyasa verisi erişimini ayrıca kontrol etmek gerekiyor."))
                elif sufficient_history == 0:
                    self.events.put(("log","VERİ UYARISI: Veri geldi ancak 55 günlük analiz için yeterli geçmiş yok."))
                self.events.put(("log",f"Tarama tamamlandı: {scanned} hisse analiz edildi. Piyasa: {'açık' if clock.get('is_open') else 'kapalı'}; {'emir gönderimi mümkün' if clock.get('is_open') else 'yeni emir gönderilmedi'}."))
            except Exception as exc: self.events.put(("log","Tarama hatası: "+str(exc)))
            self.stop.wait(scan_interval)
        self.events.put(("log","Bot durduruldu."))

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
        tk.Label(settings,text="Alışlar için Alpaca bracket emriyle yaklaşık 1,5 ATR zarar-kes ve 2R kâr-al seviyesi gönderilir. Nakit bakiyesi dışına çıkılmaz. Açık pozisyonlar ve bekleyen alış emirleri birlikte en fazla 5 ile sınırlandırılır; işlem başına yaklaşık %0,5 risk hedeflenir. Bu korumalar kaybı garanti olarak sınırlamaz.",bg=bg,fg="#ffc36d",wraplength=850,justify="left").pack(anchor="w",padx=20,pady=5)
        tk.Label(settings,text="Ücretsiz veri IEX akışıdır. Son 1 yıllık günlük veriler 20'şer sembollük isteklerle alınır; böylece Alpaca'nın 10.000 toplam bar sınırı aşılmaz. Her 5 dakikada 200 sembol taranır. Veri sayıları faaliyet kayıtlarına yazılır. Gerçek para hesabı bu sürümde desteklenmez.",bg=bg,fg="#9caec1",wraplength=850,justify="left").pack(anchor="w",padx=20,pady=5)
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
