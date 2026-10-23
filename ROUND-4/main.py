import json
import math
from datamodel import Order

# ─────────────────────────────────────────────────────────────────────────────
# POSITION LIMITS
# ─────────────────────────────────────────────────────────────────────────────
POSITION_LIMITS = {
    "HYDROGEL_PACK": 200,
    "VELVETFRUIT_EXTRACT": 200,
    "VEV_4000": 300, "VEV_4500": 300, "VEV_5000": 300, "VEV_5100": 300,
    "VEV_5200": 300, "VEV_5300": 300, "VEV_5400": 300, "VEV_5500": 300,
    "VEV_6000": 300, "VEV_6500": 300,
}

SCALP_STRIKES = [5100, 5200, 5300, 5500]  # Removed 5400 (consistent loser: theta decay kills OTM)
MM_STRIKES = [4000, 4500]
ALL_PRODUCTS = list(POSITION_LIMITS.keys())

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def safe_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}

def best_bid_ask(d):
    if not d.buy_orders or not d.sell_orders: return None, None
    return max(d.buy_orders.keys()), min(d.sell_orders.keys())

def sorted_bids(d):
    return sorted(d.buy_orders.items(), key=lambda x: x[0], reverse=True)

def sorted_asks(d):
    return sorted(d.sell_orders.items(), key=lambda x: x[0])

def get_mid(product, od):
    if product not in od: return None
    b, a = best_bid_ask(od[product])
    return (b + a) / 2.0 if b is not None and a is not None else None

def get_micro(d):
    if not d.buy_orders or not d.sell_orders: return None
    bb = max(d.buy_orders.keys()); ba = min(d.sell_orders.keys())
    bv = abs(d.buy_orders[bb]); av = abs(d.sell_orders[ba])
    return (bb * av + ba * bv) / (av + bv) if (av + bv) > 0 else (bb + ba) / 2.0

def obi(d):
    if not d.buy_orders or not d.sell_orders: return 0.0
    bv = sum(abs(v) for v in d.buy_orders.values())
    av = sum(abs(v) for v in d.sell_orders.values())
    return (bv - av) / (bv + av) if (bv + av) > 0 else 0.0

def clamp(q, pos, lim):
    if q > 0: return min(q, lim - pos)
    elif q < 0: return max(q, -lim - pos)
    return 0

class OM:
    def __init__(self, prod, pos, hard, soft=None):
        self.prod = prod; self.pos = pos; self.hard = hard
        self.soft = hard if soft is None else min(abs(soft), hard)
        self.orders = []
    def buy(self, px, qty, force=False):
        if qty <= 0: return 0
        mx = self.hard if force else self.soft
        q = min(int(qty), max(0, mx - self.pos))
        if q > 0:
            self.orders.append(Order(self.prod, int(px), q)); self.pos += q
        return q
    def sell(self, px, qty, force=False):
        if qty <= 0: return 0
        mn = -(self.hard if force else self.soft)
        q = min(int(qty), max(0, self.pos - mn))
        if q > 0:
            self.orders.append(Order(self.prod, int(px), -q)); self.pos -= q
        return q

# ─────────────────────────────────────────────────────────────────────────────
# BLACK-SCHOLES
# ─────────────────────────────────────────────────────────────────────────────
def ncdf(x):
    a1,a2,a3,a4,a5,p = 0.254829592,-0.284496736,1.421413741,-1.453152027,1.061405429,0.3275911
    s = 1 if x >= 0 else -1; x = abs(x)/math.sqrt(2.0)
    t = 1.0/(1.0+p*x)
    y = 1.0-(((((a5*t+a4)*t)+a3)*t+a2)*t+a1)*t*math.exp(-x*x)
    return 0.5*(1.0+s*y)

def npdf(x): return math.exp(-0.5*x*x)/math.sqrt(2.0*math.pi)

def bsc(S,K,T,v):
    if S<=0 or K<=0: return 0.0
    if T<=1e-9 or v<=1e-9: return max(0.0,S-K)
    st=math.sqrt(T); d1=(math.log(S/K)+0.5*v*v*T)/(v*st); d2=d1-v*st
    return S*ncdf(d1)-K*ncdf(d2)

def bsd(S,K,T,v):
    if S<=0 or K<=0: return 0.0
    if T<=1e-9 or v<=1e-9: return 1.0 if S>K else 0.0
    st=math.sqrt(T); d1=(math.log(S/K)+0.5*v*v*T)/(v*st)
    return ncdf(d1)

def bsv(S,K,T,v):
    if S<=0 or K<=0 or T<=1e-9 or v<=1e-9: return 0.0
    st=math.sqrt(T); d1=(math.log(S/K)+0.5*v*v*T)/(v*st)
    return S*npdf(d1)*st

def ivol(mp,S,K,T,init=0.20):
    intr=max(0.0,S-K)
    if mp<=intr+0.01 or T<=1e-9: return init
    sig=max(0.05,min(1.5,init))
    for _ in range(20):
        p=bsc(S,K,T,sig); vg=bsv(S,K,T,sig)
        if vg<1e-8: break
        sig-=(p-mp)/vg; sig=max(0.01,min(2.0,sig))
        if abs(p-mp)<0.01: break
    return sig

# ─────────────────────────────────────────────────────────────────────────────
# VOL SMILE CALIBRATOR
# ─────────────────────────────────────────────────────────────────────────────
class Smile:
    HA,HB,HC = 0.1987,0.0024,0.0184

    def fit(self, S, T, od, prev):
        if T<=1e-9 or S<=0: return prev,(self.HA,self.HB,self.HC)
        st=math.sqrt(T); pts=[]
        for K in SCALP_STRIKES:
            mid=get_mid(f"VEV_{K}",od)
            if mid is None or mid<=0: continue
            intr=max(0.0,S-K)
            if mid<=intr+0.5: continue
            iv=ivol(mid,S,K,T,float(prev.get(str(K),0.20)))
            if 0.05<=iv<=1.0:
                pts.append((math.log(K/S)/st,iv,K))
        if len(pts)<3: return dict(prev),(self.HA,self.HB,self.HC)
        n=len(pts); s0,s1,s2,s3,s4=n,0.,0.,0.,0.; sy,smy,sm2y=0.,0.,0.
        for m,iv,_ in pts:
            s1+=m;s2+=m*m;s3+=m**3;s4+=m**4;sy+=iv;smy+=m*iv;sm2y+=m*m*iv
        det=s0*(s2*s4-s3*s3)-s1*(s1*s4-s3*s2)+s2*(s1*s3-s2*s2)
        if abs(det)<1e-12: return dict(prev),(self.HA,self.HB,self.HC)
        a=(sy*(s2*s4-s3*s3)-s1*(smy*s4-sm2y*s3)+s2*(smy*s3-sm2y*s2))/det
        b=(s0*(smy*s4-sm2y*s3)-sy*(s1*s4-s3*s2)+s2*(s1*sm2y-smy*s2))/det
        c=(s0*(s2*sm2y-s3*smy)-s1*(s1*sm2y-smy*s2)+sy*(s1*s3-s2*s2))/det
        if a<0.05 or a>0.80 or c<-0.1: a,b,c=self.HA,self.HB,self.HC
        a=0.8*a+0.2*self.HA; b=0.8*b+0.2*self.HB; c=0.8*c+0.2*self.HC
        ivs={}
        for K in SCALP_STRIKES+MM_STRIKES+[5000]:
            m=math.log(K/S)/st; siv=max(0.05,min(1.0,a+b*m+c*m*m))
            old=float(prev.get(str(K),siv)); ivs[str(K)]=0.5*old+0.5*siv
        return ivs,(a,b,c)

    def fair_dev(self, S, K, T, sp, od):
        a,b,c=sp
        if T<=1e-9 or S<=0: return max(0.0,S-K),0.0,0.0
        st=math.sqrt(T); m=math.log(K/S)/st
        siv=max(0.05,min(1.0,a+b*m+c*m*m))
        f=bsc(S,K,T,siv); d=bsd(S,K,T,siv)
        mid=get_mid(f"VEV_{K}",od)
        return (f,mid-f,d) if mid is not None else (f,0.0,d)

# ─────────────────────────────────────────────────────────────────────────────
# COUNTERPARTY TRACKER
# ─────────────────────────────────────────────────────────────────────────────
class CPTrack:
    def update(self, state):
        flow={}
        for prod, trades in state.market_trades.items():
            bq={}; sq={}
            for tr in trades:
                if tr.timestamp!=state.timestamp: continue
                if tr.buyer: bq[tr.buyer]=bq.get(tr.buyer,0)+tr.quantity
                if tr.seller: sq[tr.seller]=sq.get(tr.seller,0)+tr.quantity
            if bq or sq:
                tb=max(bq.items(),key=lambda x:x[1])[0] if bq else None
                ts=max(sq.items(),key=lambda x:x[1])[0] if sq else None
                flow[prod]={"tb":tb,"ts":ts}
        return flow

# ─────────────────────────────────────────────────────────────────────────────
# HYDROGEL — ADAPTIVE Mean Reversion
#
# CRITICAL FIX: Dynamic fair using running mean as anchor.
# Fixed anchor (9991): mean dev=+42.5, PnL proxy=+1,904, actual=-3,577
# Dynamic anchor:      mean dev=+10.7, PnL proxy=+12,744
# ─────────────────────────────────────────────────────────────────────────────
class HydrogelTrader:
    P = "HYDROGEL_PACK"
    def __init__(self):
        self.ema_a=0.003; self.anc_w=0.10; self.rev_a=5; self.thr=3
        self.sp=2; self.qty=20; self.skew=2.0; self.slim=150; self.eod=880000

    def run(self, state, prev, flow):
        p=self.P; lim=POSITION_LIMITS[p]
        if p not in state.order_depths: return [],prev
        depth=state.order_depths[p]
        if not depth.buy_orders or not depth.sell_orders: return [],prev
        pos=state.position.get(p,0); orders=[]
        bids=sorted_bids(depth); asks=sorted_asks(depth)
        bb=bids[0][0]; ba=asks[0][0]
        bv=abs(bids[0][1]); av=abs(asks[0][1])
        micro=(bb*av+ba*bv)/(av+bv) if (av+bv)>0 else (bb+ba)/2.0

        # DYNAMIC FAIR: EMA anchored to running mean (adapts to regime shifts)
        pf=float(prev.get("f",micro))
        fair=(1-self.ema_a)*pf+self.ema_a*micro
        rs=float(prev.get("rs",micro)); rn=int(prev.get("rn",1))
        rs+=micro; rn+=1; rmean=rs/rn
        fair=(1-self.anc_w)*fair+self.anc_w*rmean

        dev=micro-fair
        tgt=int(-self.rev_a*dev)
        # Position cap: scale max position with deviation size
        # At dev=10 -> cap=80, dev=20 -> cap=150, dev=30+ -> cap=150
        dev_cap = min(self.slim, max(40, int(8 * abs(dev))))
        tgt=max(-dev_cap, min(dev_cap, tgt))

        # EOD
        if state.timestamp>=self.eod:
            prog=min(1.0,(state.timestamp-self.eod)/(1e6-self.eod))
            tgt=int(tgt*(1-0.85*prog))
            if pos>30:
                q=clamp(-min(35,pos),pos,lim)
                if q<0: orders.append(Order(p,bb,q)); pos+=q
            elif pos<-30:
                q=clamp(min(35,-pos),pos,lim)
                if q>0: orders.append(Order(p,ba,q)); pos+=q

        # AGGRESSIVE TAKE
        if abs(dev)>self.thr:
            for px,aq in asks:
                if px>fair-0.5: break
                q=clamp(abs(aq),pos,lim)
                if tgt>0: q=min(q,max(0,int(tgt*1.2)-pos))
                if q>0: orders.append(Order(p,px,q)); pos+=q
            for px,bq in bids:
                if px<fair+0.5: break
                q=clamp(-abs(bq),pos,lim)
                if tgt<0: q=max(q,min(0,int(tgt*1.2)-pos))
                if q<0: orders.append(Order(p,px,q)); pos+=q

        # PASSIVE MM
        ps=(tgt-pos)/max(abs(tgt)+1,20)
        bo=max(1,self.sp-ps*self.skew); ao=max(1,self.sp+ps*self.skew)
        mb=min(int(round(fair-bo)),bb+1); ma=max(int(round(fair+ao)),ba-1)
        bq,aq=self.qty,self.qty
        if pos>tgt: aq=int(self.qty*1.5); bq=int(self.qty*0.5)
        elif pos<tgt: bq=int(self.qty*1.5); aq=int(self.qty*0.5)

        pf2=flow.get(p,{})
        if pf2.get("tb")=="Mark 38": aq=max(aq,30); bq=min(bq,8)
        elif pf2.get("ts")=="Mark 38": bq=max(bq,30); aq=min(aq,8)

        if state.timestamp>=self.eod: bq=max(3,bq//2); aq=max(3,aq//2)
        bq=clamp(bq,pos,lim); aq=clamp(-aq,pos,lim)
        if bq>0: orders.append(Order(p,mb,bq))
        if aq<0: orders.append(Order(p,ma,aq))
        return orders,{"f":fair,"rs":rs,"rn":rn}

# ─────────────────────────────────────────────────────────────────────────────
# IV SCALPER
# ─────────────────────────────────────────────────────────────────────────────
class IVScalp:
    def __init__(self):
        self.sl={5100:100,5200:120,5300:120,5500:60}
        self.te={5100:0.5,5200:0.5,5300:0.5,5500:0.5}
        self.mh={5100:2,5200:1,5300:1,5500:1}
        self.mq={5100:10,5200:15,5300:15,5500:6}

    def run(self, state, S, T, sp, cal, flow):
        res={}; nd=0.0
        for K in SCALP_STRIKES:
            pr=f"VEV_{K}"; res[pr]=[]
            if pr not in state.order_depths: continue
            d=state.order_depths[pr]
            if not d.buy_orders or not d.sell_orders: continue
            pos=state.position.get(pr,0); soft=self.sl[K]
            om=OM(pr,pos,POSITION_LIMITS[pr],soft)
            bl=sorted_bids(d); al=sorted_asks(d); bb=bl[0][0]; ba=al[0][0]
            fair,dev,delta=cal.fair_dev(S,K,T,sp,state.order_depths)
            if fair<0.3 and bb<=0: nd+=pos*delta; continue
            e=self.te[K]
            if dev>e:
                for px,q in bl:
                    if px<=fair+0.3: break
                    if om.pos<=-soft: break
                    om.sell(px,min(abs(q),soft+om.pos,20))
            elif dev<-e:
                for px,q in al:
                    if px>=fair-0.3: break
                    if om.pos>=soft: break
                    om.buy(px,min(abs(q),soft-om.pos,20))
            h=self.mh[K]; bq=self.mq[K]
            isk=(om.pos/max(1,soft))*h
            bp=max(0,int(math.floor(fair-h-isk))); ap=max(bp+1,int(math.ceil(fair+h-isk)))
            bp=min(bp,bb+1); ap=max(ap,ba-1)
            if ap<=bp: ap=bp+1
            if bp==1 and bb==0 and fair<1.5: bp=0
            bqt,aqt=bq,bq
            if om.pos>0.3*soft: bqt=max(1,bq//3); aqt=bq*2
            elif om.pos<-0.3*soft: bqt=bq*2; aqt=max(1,bq//3)
            pf=flow.get(pr,{})
            if pf.get("tb")=="Mark 38": aqt=max(aqt,bq*2); bqt=max(1,bqt//2)
            elif pf.get("ts")=="Mark 38": bqt=max(bqt,bq*2); aqt=max(1,aqt//2)
            if bqt>0: om.buy(bp,bqt)
            if aqt>0: om.sell(ap,aqt)
            res[pr]=om.orders; nd+=om.pos*delta
        return res,nd

# ─────────────────────────────────────────────────────────────────────────────
# DEEP ITM MM (VEV_4000, VEV_4500)
# ─────────────────────────────────────────────────────────────────────────────
class DeepITM:
    def __init__(self):
        self.sl={4000:80,4500:60}; self.mh={4000:5,4500:4}; self.mq={4000:8,4500:6}
    def run(self, state, S, T, ivs, flow):
        res={}; nd=0.0
        for K in MM_STRIKES:
            pr=f"VEV_{K}"; res[pr]=[]
            if pr not in state.order_depths: continue
            d=state.order_depths[pr]
            if not d.buy_orders or not d.sell_orders: continue
            pos=state.position.get(pr,0); soft=self.sl[K]
            om=OM(pr,pos,POSITION_LIMITS[pr],soft)
            bl=sorted_bids(d); al=sorted_asks(d); bb=bl[0][0]; ba=al[0][0]
            sig=float(ivs.get(str(K),0.20)); fair=bsc(S,K,T,sig); delta=bsd(S,K,T,sig)
            h=self.mh[K]; bq=self.mq[K]
            for px,q in al:
                if px>=fair-1.5: break
                if om.pos>=soft: break
                om.buy(px,min(abs(q),soft-om.pos,10))
            for px,q in bl:
                if px<=fair+1.5: break
                if om.pos<=-soft: break
                om.sell(px,min(abs(q),om.pos+soft,10))
            isk=(om.pos/max(1,soft))*h
            bp=max(0,int(math.floor(fair-h-isk))); ap=int(math.ceil(fair+h-isk))
            bp=min(bp,bb+1); ap=max(ap,ba-1)
            if ap<=bp: ap=bp+1
            bqt,aqt=bq,bq
            if om.pos>0.4*soft: bqt=1; aqt=bq*2
            elif om.pos<-0.4*soft: bqt=bq*2; aqt=1
            pf=flow.get(pr,{})
            if pf.get("tb")=="Mark 38": aqt=max(aqt,bq*3); bqt=max(1,bqt//2)
            elif pf.get("ts")=="Mark 38": bqt=max(bqt,bq*3); aqt=max(1,aqt//2)
            om.buy(bp,bqt); om.sell(ap,aqt)
            res[pr]=om.orders; nd+=om.pos*delta
        return res,nd

# ─────────────────────────────────────────────────────────────────────────────
# VELVETFRUIT — Light MM + OBI
# ─────────────────────────────────────────────────────────────────────────────
class VelvetT:
    P="VELVETFRUIT_EXTRACT"
    def __init__(self):
        self.sl=80; self.hr=0.20; self.ug=60; self.qty=8; self.sp=2
    def run(self, state, nd, flow):
        p=self.P
        if p not in state.order_depths: return []
        d=state.order_depths[p]
        if not d.buy_orders or not d.sell_orders: return []
        pos=state.position.get(p,0); om=OM(p,pos,POSITION_LIMITS[p],self.sl)
        bl=sorted_bids(d); al=sorted_asks(d); bb=bl[0][0]; ba=al[0][0]
        micro=get_micro(d)
        tgt=max(-self.sl,min(self.sl,int(-self.hr*nd))); gap=tgt-om.pos
        if gap>self.ug:
            need=min(gap,15)
            for px,q in al:
                if need<=0: break
                need-=om.buy(px,min(abs(q),need),force=True)
        elif gap<-self.ug:
            need=min(-gap,15)
            for px,q in bl:
                if need<=0: break
                need-=om.sell(px,min(abs(q),need),force=True)
        ob=obi(d); os=-2.5*ob; ps=(tgt-om.pos)/50.0
        bp=int(round(micro-self.sp+ps+os)); ap=int(round(micro+self.sp+ps+os))
        bp=min(bp,bb+1); ap=max(ap,ba-1)
        if ap<=bp: ap=bp+1
        bq,aq=self.qty,self.qty
        if om.pos<tgt-20: bq=12; aq=3
        elif om.pos>tgt+20: bq=3; aq=12
        pf=flow.get(p,{})
        if pf.get("tb") in ("Mark 55","Mark 67"): aq=max(aq,12)
        if pf.get("ts")=="Mark 55": bq=max(bq,12)
        om.buy(bp,bq); om.sell(ap,aq)
        return om.orders

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
class Trader:
    def __init__(self):
        self.hyd=HydrogelTrader(); self.scl=IVScalp(); self.itm=DeepITM()
        self.vel=VelvetT(); self.smi=Smile(); self.cp=CPTrack()

    def run(self, state):
        res={p:[] for p in ALL_PRODUCTS}
        data=safe_json(state.traderData)
        hd=data.get("h",{}); od=data.get("o",{})
        pt=int(od.get("pt",-1)); day=int(od.get("d",0))
        if pt>=0 and state.timestamp<pt: day+=1
        T=max(0.05,4.0-day-state.timestamp/1e6)/252.0
        S=get_mid("VELVETFRUIT_EXTRACT",state.order_depths)
        if S is None: S=float(od.get("s",5247.0))
        flow=self.cp.update(state)
        pivs=od.get("iv",{}); ivs,sp=self.smi.fit(S,T,state.order_depths,pivs)
        ho,nh=self.hyd.run(state,hd,flow); res["HYDROGEL_PACK"]=ho
        so,sd=self.scl.run(state,S,T,sp,self.smi,flow)
        for p,o in so.items(): res[p]=o
        io,id2=self.itm.run(state,S,T,ivs,flow)
        for p,o in io.items(): res[p]=o
        res["VELVETFRUIT_EXTRACT"]=self.vel.run(state,sd+id2,flow)
        nd={"h":nh,"o":{"pt":state.timestamp,"d":day,"s":S,"iv":ivs}}
        return res,0,json.dumps(nd)
