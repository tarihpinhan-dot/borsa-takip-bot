import json, math, os, queue, threading, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import ttk, messagebox

PAPER="https://paper-api.alpaca.markets"
DATA="https://data.alpaca.markets"
APPDIR=os.path.join(os.environ.get("APPDATA",os.path.expanduser("~")),"AstraTradeAI")
os.makedirs(APPDIR,exist_ok=True)
LOG=os.path.join(APPDIR,"astratrade.log")
EXCH={"NYSE","NASDAQ","AMEX","ARCA","NYSEARCA","BATS"}
POS={"beats","beat","surge","record","upgrade","growth","profit","strong","buyback","raises","approval","approved","bullish","wins","revenue"}
NEG={"miss","downgrade","plunge","loss","warning","warns","cuts","fraud","probe","investigation","lawsuit","recall","bankruptcy","bearish","weak","layoff"}

def logline(s):
    line=datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")+str(s)
    try:
        with open(LOG,"a",encoding="utf-8") as f: f.write(line+"\n")
    except Exception: pass
    return line

class Alpaca:
    def __init__(self,key,secret):
        if not key or not secret: raise ValueError("Paper API Key ve Secret Key gerekli.")
        self.h={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret,"Content-Type":"application/json","Accept":"application/json"}
    def call(self,base,path,params=None,method="GET",body=None):
        url=base+path+(("?"+urllib.parse.urlencode(params)) if params else "")
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(url,data=data,headers=self.h,method=method)
        try:
            with urllib.request.urlopen(req,timeout=30) as r:
                raw=r.read().decode("utf-8"); return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError("Alpaca HTTP "+str(e.code)+": "+e.read().decode("utf-8",errors="replace")[:600])
        except urllib.error.URLError as e: raise RuntimeError("İnternet/API bağlantısı: "+str(e))
    def account(self): return self.call(PAPER,"/v2/account")
    def clock(self): return self.call(PAPER,"/v2/clock")
    def assets(self): return self.call(PAPER,"/v2/assets",{"status":"active","asset_class":"us_equity"})
    def positions(self): return self.call(PAPER,"/v2/positions")
    def open_orders(self): return self.call(PAPER,"/v2/orders",{"status":"open","limit":500})
    def bars(self,symbols): return self.call(DATA,"/v2/stocks/bars",{"symbols":",".join(symbols),"timeframe":"1Day","limit":220,"feed":"iex"})
    def news(self,symbols):
        start=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
        return self.call(DATA,"/v1beta1/news",{"symbols":",".join(symbols),"start":start,"limit":50,"sort":"desc"})
    def order(self,symbol,qty,side):
        return self.call(PAPER,"/v2/orders",method="POST",body={"symbol":symbol,"qty":str(qty),"side":side,"type":"market","time_in_force":"day"})

def enum(v): return str(v or "").split(".")[-1].upper()
def get_universe(assets):
    out=[]
    for a in assets:
        s=str(a.get("symbol","")).upper()
        if enum(a.get("status"))=="ACTIVE" and a.get("tradable") is True and enum(a.get("exchange")) in EXCH and s and len(s)<=8 and s.replace(".","").replace("-","").isalnum(): out.append(s)
    return sorted(set(out))

def ema(xs,n):
    if not xs:return 0.
    k=2/(n+1); v=float(xs[0])
    for z in xs[1:]:v=k*float(z)+(1-k)*v
    return v

def features(rows,i):
    if i<55:return None
    p=rows[:i+1]; c=[float(x["c"]) for x in p]; h=[float(x["h"]) for x in p]; l=[float(x["l"]) for x in p]; v=[float(x["v"]) for x in p]
    price=c[-1]
    if price<=0:return None
    e20=ema(c[-100:],20); e50=ema(c[-150:],50); e12=ema(c[-80:],12); e26=ema(c[-100:],26)
    d=[c[j]-c[j-1] for j in range(max(1,len(c)-14),len(c))]
    gain=sum(max(0,x) for x in d)/max(1,len(d)); loss=sum(max(0,-x) for x in d)/max(1,len(d))
    rsi=100 if loss==0 else 100-100/(1+gain/loss)
    tr=[max(h[j]-l[j],abs(h[j]-c[j-1]),abs(l[j]-c[j-1])) for j in range(max(1,len(c)-14),len(c))]
    atr=sum(tr)/max(1,len(tr)); avgd=sum(c[-20+j]*v[-20+j] for j in range(20))/20
    ret5=price/c[-6]-1 if c[-6] else 0; ret20=price/c[-21]-1 if c[-21] else 0
    avgv=sum(v[-20:])/20; vr=v[-1]/avgv if avgv else 1
    x=[max(-1,min(1,ret5*8)),max(-1,min(1,ret20*4)),max(-1,min(1,(price/e20-1)*18)),max(-1,min(1,(e20/e50-1)*18)),max(-1,min(1,(rsi-50)/25)),max(-1,min(1,(vr-1)/2))]
    return {"price":price,"ema20":e20,"ema50":e50,"macd":e12-e26,"rsi":rsi,"atr":atr,"avgd":avgd,"ret20":ret20,"x":x}

class TinyML:
    def __init__(self):self.ready=False;self.mu=[0.]*6;self.sd=[1.]*6;self.w=[0.]*6;self.b=0.
    def fit(self,samples):
        if len(samples)<100 or len({y for x,y in samples})<2:self.ready=False;return
        cols=list(zip(*(x for x,y in samples)));self.mu=[sum(c)/len(c) for c in cols]
        self.sd=[max(.08,(sum((z-self.mu[j])**2 for z in cols[j])/len(cols[j]))**.5) for j in range(6)]
        data=[([(x[j]-self.mu[j])/self.sd[j] for j in range(6)],y) for x,y in samples];w=[0.]*6;b=0.
        for ep in range(18):
            lr=.04/(1+ep*.08)
            for x,y in data:
                p=1/(1+math.exp(-max(-15,min(15,b+sum(w[j]*x[j] for j in range(6))))))
                err=p-y
                for j in range(6):w[j]-=lr*(err*x[j]+.002*w[j])
                b-=lr*err
        self.w,self.b,self.ready=w,b,True
    def predict(self,x):
        if not self.ready:return .5
        z=self.b+sum(self.w[j]*(x[j]-self.mu[j])/self.sd[j] for j in range(6))
        return 1/(1+math.exp(-max(-15,min(15,z))))

def trainset(frames):
    out=[]
    for rows in frames.values():
        for i in range(55,len(rows)-5,2):
            f=features(rows,i)
            if f:out.append((f["x"],1 if float(rows[i+5]["c"])>f["price"]*1.005 else 0))
    return out

def news_sentiment(items):
    out={}
    for item in items:
        words={w.strip(".,:;!?()[]{}'\\\"").lower() for w in (str(item.get("headline",""))+" "+str(item.get("summary",""))).split()}
        score=max(-1,min(1,(len(words&POS)-len(words&NEG))/3))
        for s in item.get("symbols",[]):out.setdefault(s,[]).append(score)
    return {s:sum(v)/len(v) for s,v in out.items()}

def signal(f,ml,news):
    score=0.;why=[]
    if f["price"]>f["ema20"]>f["ema50"]:score+=.25;why.append("EMA trend")
    if f["macd"]>0:score+=.18;why.append("MACD pozitif")
    if 45<=f["rsi"]<=68:score+=.12;why.append("RSI momentum")
    if f["avgd"]>=5_000_000:score+=.10;why.append("likidite")
    if f["ret20"]>0:score+=.08;why.append("20g momentum")
    score+=.20*ml+.07*max(-1,min(1,news))
    action="BUY" if score>=.72 else ("SELL" if f["price"]<f["ema20"] and f["macd"]<0 else "HOLD")
    return action,max(0,min(1,score)),", ".join(why) or "Alım şartları oluşmadı"

class Worker:
    def __init__(self,api,q,stop):self.api,self.q,self.stop=api,q,stop;self.symbols=[];self.offset=0;self.ml=TinyML()
    def emit(self,k,v):self.q.put((k,v))
    def run(self):
        self.api.account();self.symbols=get_universe(self.api.assets())
        if not self.symbols:raise RuntimeError("Alpaca uygun ABD hissesi listesi döndürmedi.")
        self.emit("log",f"Bağlantı başarılı. Uygun hisse evreni: {len(self.symbols)}")
        self.emit("log","Paper Trading güvenlik kilidi etkin. Gerçek para emri gönderilmez.")
        while not self.stop.is_set():
            start=time.time();batch=self.symbols[self.offset:self.offset+30];self.offset=(self.offset+30)%len(self.symbols)
            if len(batch)<30:batch+=self.symbols[:30-len(batch)]
            try:
                raw=self.api.bars(batch).get("bars",{});frames={s:raw[s] for s in batch if raw.get(s)}
                self.ml.fit(trainset(frames))
                try:news=news_sentiment(self.api.news(batch).get("news",[]))
                except Exception as e:news={};self.emit("log","Haber puanı nötr: "+str(e))
                pos=self.api.positions();held={p["symbol"]:p for p in pos}
                pending={o["symbol"] for o in self.api.open_orders()}
                acct=self.api.account();market=bool(self.api.clock().get("is_open"))
                equity=float(acct.get("equity",0));last=float(acct.get("last_equity",0));daily=(equity-last)/last if last else 0
                count=0
                for sym,rows in frames.items():
                    if self.stop.is_set():break
                    f=features(rows,len(rows)-1)
                    if not f or f["price"]<5:continue
                    count+=1;mp=self.ml.predict(f["x"]);ns=news.get(sym,0.)
                    action,score,_=signal(f,mp,ns)
                    self.emit("decision",{"symbol":sym,"action":action,"score":score,"price":f["price"],"ml":mp})
                    if not market or sym in pending:continue
                    if action=="BUY" and sym not in held:
                        if len(held)>=5 or daily<=-.02:continue
                        distance=max(1.5*f["atr"],f["price"]*.01);qty=max(0,min(int(equity*.005/distance),int(equity*.10/f["price"])))
                        if qty:
                            o=self.api.order(sym,qty,"buy");pending.add(sym)
                            self.emit("log",f"SANAL ALIM {sym} x{qty} | skor {score:.2f} | {o.get('status','gönderildi')}")
                    elif action=="SELL" and sym in held:
                        qty=abs(float(held[sym].get("qty",0)))
                        if qty:
                            o=self.api.order(sym,qty,"sell");pending.add(sym)
                            self.emit("log",f"SANAL SATIŞ {sym} x{qty:g} | {o.get('status','gönderildi')}")
                self.emit("status",{"universe":len(self.symbols),"scanned":count,"market":market,"ml":self.ml.ready,"equity":equity,"cash":float(acct.get("cash",0)),"positions":len(held)})
                self.emit("log",f"Tarama tamamlandı: {count} hisse. ML: {'hazır' if self.ml.ready else 'nötr/veri az'}. Piyasa: {'açık' if market else 'kapalı'}.")
            except Exception as e:self.emit("log",f"Tarama hatası: {type(e).__name__}: {e}")
            self.stop.wait(max(10,900-int(time.time()-start)))
        self.emit("log","Bot durduruldu.")

class App:
    def __init__(self,root):
        self.root=root;root.title("AstraTrade AI");root.geometry("1000x700");root.minsize(850,600);root.configure(bg="#101827")
        self.q=queue.Queue();self.stop_event=threading.Event();self.thread=None;self.api=None
        self.build();root.after(250,self.poll);root.protocol("WM_DELETE_WINDOW",self.close)
    def build(self):
        bg="#101827";panel="#172337";green="#36c99a"
        h=tk.Frame(self.root,bg=bg);h.pack(fill="x",padx=22,pady=18)
        tk.Label(h,text="ASTRATRADE AI",bg=bg,fg="white",font=("Segoe UI",23,"bold")).pack(side="left")
        tk.Label(h,text="US STOCKS  •  PAPER TRADING",bg=bg,fg=green,font=("Segoe UI",9,"bold")).pack(side="left",padx=16)
        self.conn=tk.Label(h,text="● BAĞLI DEĞİL",bg=bg,fg="#ffc36d",font=("Segoe UI",10,"bold"));self.conn.pack(side="right")
        tabs=ttk.Notebook(self.root);tabs.pack(fill="both",expand=True,padx=22,pady=(0,12));self.tabs=tabs
        self.home=tk.Frame(tabs,bg=bg);self.settings=tk.Frame(tabs,bg=bg);self.history=tk.Frame(tabs,bg=bg)
        tabs.add(self.home,text="Kontrol Paneli");tabs.add(self.settings,text="Hesap Bağlantısı");tabs.add(self.history,text="İşlem Geçmişi")
        self.metrics={};r=tk.Frame(self.home,bg=bg);r.pack(fill="x",pady=14)
        for key,title in [("equity","SANAL HESAP"),("cash","NAKİT"),("positions","AÇIK POZİSYON")]:
            c=tk.Frame(r,bg=panel,padx=15,pady=12);c.pack(side="left",fill="x",expand=True,padx=(0,10))
            tk.Label(c,text=title,bg=panel,fg="#9ba9ba").pack(anchor="w")
            v=tk.Label(c,text="—",bg=panel,fg="white",font=("Segoe UI",19,"bold"));v.pack(anchor="w",pady=5);self.metrics[key]=v
        bar=tk.Frame(self.home,bg=panel,padx=15,pady=12);bar.pack(fill="x",pady=(0,12))
        tk.Label(bar,text="Otomatik sanal işlem",bg=panel,fg="white",font=("Segoe UI",12,"bold")).pack(side="left")
        self.start_btn=tk.Button(bar,text="BOTU BAŞLAT",command=self.start,bg=green,fg="#07131a",relief="flat",padx=18,pady=9);self.start_btn.pack(side="right",padx=5)
        self.stop_btn=tk.Button(bar,text="DURDUR",command=self.stop,bg="#a13e4a",fg="white",relief="flat",padx=18,pady=9,state="disabled");self.stop_btn.pack(side="right")
        self.market=tk.Label(self.home,text="Piyasa: — | Hisse evreni: —",bg=bg,fg="#9ba9ba");self.market.pack(anchor="w",pady=5)
        tk.Label(self.home,text="BOT FAALİYETLERİ",bg=bg,fg="white").pack(anchor="w")
        self.log=tk.Text(self.home,bg="#0b1220",fg="#d7e2ee",relief="flat",font=("Consolas",9),padx=12,pady=10);self.log.pack(fill="both",expand=True,pady=6);self.log.configure(state="disabled")
        card=tk.Frame(self.settings,bg=panel,padx=22,pady=22);card.pack(fill="x",padx=18,pady=18)
        tk.Label(card,text="Alpaca Paper Trading",bg=panel,fg="white",font=("Segoe UI",15,"bold")).pack(anchor="w")
        tk.Label(card,text="Yalnızca PAPER API anahtarlarını gir. Gerçek hesap anahtarlarını kullanma.",bg=panel,fg="#ffc36d").pack(anchor="w",pady=10)
        tk.Label(card,text="Paper API Key",bg=panel,fg="white").pack(anchor="w");self.key=tk.Entry(card,font=("Consolas",10));self.key.pack(fill="x",pady=5)
        tk.Label(card,text="Paper Secret Key",bg=panel,fg="white").pack(anchor="w");self.secret=tk.Entry(card,show="*",font=("Consolas",10));self.secret.pack(fill="x",pady=5)
        self.test=tk.Button(card,text="BAĞLANTIYI TEST ET",command=self.test_connection,bg=green,fg="#07131a",relief="flat",padx=18,pady=9);self.test.pack(anchor="w",pady=12)
        self.result=tk.Label(self.settings,text="Henüz bağlantı test edilmedi.",bg=bg,fg="#9ba9ba",wraplength=850,justify="left");self.result.pack(anchor="w",padx=22)
        tk.Label(self.settings,text="Aktif ve işlem yapılabilir ABD hisseleri taranır. Her turda 30 sembol sırayla incelenir. Ücretsiz veri kaynağı IEX'tir.",bg=bg,fg="#9ba9ba",wraplength=850,justify="left").pack(anchor="w",padx=22,pady=14)
        style=ttk.Style();style.theme_use("clam");style.configure("Treeview",background=panel,foreground="white",fieldbackground=panel,rowheight=27);style.configure("Treeview.Heading",background="#263950",foreground="white")
        self.tree=ttk.Treeview(self.history,columns=("time","symbol","side","qty","status"),show="headings")
        for col,title,width in [("time","ZAMAN",180),("symbol","HİSSE",100),("side","YÖN",90),("qty","MİKTAR",100),("status","DURUM",190)]:
            self.tree.heading(col,text=title);self.tree.column(col,width=width,anchor="center")
        self.tree.pack(fill="both",expand=True,padx=15,pady=15)
        tk.Button(self.history,text="YENİLE",command=self.refresh_history,bg="#34465e",fg="white",relief="flat",padx=15,pady=8).pack(anchor="e",padx=15,pady=10)
        tk.Label(self.root,text="Deneysel yazılım • Yalnızca sanal emir • Kâr garantisi yoktur",bg=bg,fg="#9ba9ba",font=("Segoe UI",8)).pack(pady=(0,8))
    def addlog(self,msg):
        line=logline(msg);self.log.configure(state="normal");self.log.insert("end",line+"\n");self.log.see("end");self.log.configure(state="disabled")
    def test_connection(self):
        key,sec=self.key.get().strip(),self.secret.get().strip()
        if not key or not sec:messagebox.showwarning("Eksik bilgi","Paper API Key ve Secret Key gir.");return
        self.test.configure(state="disabled",text="TEST EDİLİYOR...")
        def go():
            try:api=Alpaca(key,sec);self.q.put(("connected",(api,api.account())))
            except Exception as e:self.q.put(("connect_error",str(e)))
        threading.Thread(target=go,daemon=True).start()
    def start(self):
        if self.thread and self.thread.is_alive():return
        if not self.key.get().strip() or not self.secret.get().strip():
            self.tabs.select(self.settings);messagebox.showwarning("Hesap bağlantısı","Önce Paper API anahtarlarını gir ve bağlantıyı test et.");return
        self.api=Alpaca(self.key.get().strip(),self.secret.get().strip());self.stop_event.clear()
        self.thread=threading.Thread(target=self.worker_run,args=(Worker(self.api,self.q,self.stop_event),),daemon=True);self.thread.start()
        self.start_btn.configure(state="disabled");self.stop_btn.configure(state="normal");self.conn.configure(text="● BOT ÇALIŞIYOR",fg=green);self.addlog("Bot başlatıldı. Paper Trading kilidi etkin.")
    def worker_run(self,w):
        try:w.run()
        except Exception as e:self.q.put(("fatal",type(e).__name__+": "+str(e)))
    def stop(self):
        self.stop_event.set();self.stop_btn.configure(state="disabled");self.addlog("Durdurma isteği gönderildi.")
    def poll(self):
        try:
            while True:
                kind,v=self.q.get_nowait()
                if kind=="log":self.addlog(v)
                elif kind=="connected":
                    self.api,acc=v;self.conn.configure(text="● ALPACA PAPER BAĞLI",fg=green)
                    self.result.configure(text="Bağlantı başarılı. Hesap: "+str(acc.get("status"))+" | Sanal özkaynak: USD "+format(float(acc.get("equity",0)),",.2f"),fg=green)
                    self.metrics["equity"].configure(text="USD "+format(float(acc.get("equity",0)),",.2f"));self.metrics["cash"].configure(text="USD "+format(float(acc.get("cash",0)),",.2f"))
                    self.test.configure(state="normal",text="BAĞLANTIYI TEST ET");self.addlog("Alpaca Paper bağlantısı doğrulandı.")
                elif kind=="connect_error":
                    self.result.configure(text="Bağlantı kurulamadı: "+v,fg="#ff7373");self.test.configure(state="normal",text="BAĞLANTIYI TEST ET");messagebox.showerror("Bağlantı hatası",v)
                elif kind=="decision":self.addlog(f"{v['symbol']} | {v['action']} | skor {v['score']:.2f} | USD {v['price']:.2f} | ML {v['ml']:.2f}")
                elif kind=="status":
                    self.metrics["equity"].configure(text="USD "+format(v["equity"],",.2f"));self.metrics["cash"].configure(text="USD "+format(v["cash"],",.2f"));self.metrics["positions"].configure(text=str(v["positions"]))
                    self.market.configure(text=f"Piyasa: {'Açık' if v['market'] else 'Kapalı'} | Hisse evreni: {v['universe']} | Bu tur: {v['scanned']} | ML: {'eğitildi' if v['ml'] else 'nötr'}")
                elif kind=="history":
                    for it in self.tree.get_children():self.tree.delete(it)
                    for o in v:self.tree.insert("","end",values=(str(o.get("submitted_at",""))[:16],o.get("symbol",""),o.get("side","").upper(),o.get("qty",""),o.get("status","")))
                elif kind=="fatal":
                    self.addlog("HATA: "+v);self.conn.configure(text="● HATA",fg="#ff7373");self.start_btn.configure(state="normal");self.stop_btn.configure(state="disabled");messagebox.showerror("Bot hatası",v)
        except queue.Empty:pass
        if self.thread and not self.thread.is_alive() and str(self.start_btn["state"])=="disabled":
            self.start_btn.configure(state="normal");self.stop_btn.configure(state="disabled")
            if self.stop_event.is_set():self.conn.configure(text="● DURDURULDU",fg="#ffc36d")
        self.root.after(300,self.poll)
    def refresh_history(self):
        if not self.api:messagebox.showinfo("Bilgi","Önce Alpaca'ya bağlan.");return
        def go():
            try:self.q.put(("history",self.api.call(PAPER,"/v2/orders",{"status":"all","limit":100,"direction":"desc"})))
            except Exception as e:self.q.put(("log","İşlem geçmişi alınamadı: "+str(e)))
        threading.Thread(target=go,daemon=True).start()
    def close(self):
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno("Bot çalışıyor","Uygulama kapanınca bot duracak. Kapatılsın mı?"):return
            self.stop_event.set()
        self.root.destroy()

if __name__=="__main__":
    root=tk.Tk();App(root);root.mainloop()
