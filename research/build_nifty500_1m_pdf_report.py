#!/usr/bin/env python3
from __future__ import annotations
import io, os, re, json, math, textwrap, urllib.parse, urllib.request
from pathlib import Path
import numpy as np
import pandas as pd
import requests
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak,
    KeepTogether
)
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

ROOT=Path("/tmp/results")
OUT=Path("/tmp/report")
OUT.mkdir(parents=True,exist_ok=True)
AS=OUT/"assets"; AS.mkdir(exist_ok=True)

# ---------- Load corrected outputs ----------
fdir=ROOT/"fno"; ndir=ROOT/"nonfno"
f_metrics=pd.read_csv(fdir/"fno_metrics.csv")
n_metrics=pd.read_csv(ndir/"nonfno_metrics.csv")
f_cl=pd.read_csv(fdir/"fno_cluster_characteristics.csv")
n_cl=pd.read_csv(ndir/"nonfno_cluster_characteristics.csv")
f_imp=pd.read_csv(fdir/"fno_feature_importance.csv")
n_imp=pd.read_csv(ndir/"nonfno_feature_importance.csv")
f_pred=pd.read_parquet(fdir/"fno_test_predictions.parquet")
n_pred=pd.read_parquet(ndir/"nonfno_test_predictions.parquet")
for q in [f_pred,n_pred]:
    q["ts"]=pd.to_datetime(q["ts"])
    q["date"]=pd.to_datetime(q["date"])

# ---------- Utilities ----------
PALETTE={"blue":"#1f4e79","teal":"#2f7e82","orange":"#c87b2a","red":"#a23b3b",
         "green":"#3d7a4d","gray":"#6b7280","light":"#e9eef3","navy":"#17324d"}
plt.rcParams.update({"font.size":9,"axes.titlesize":11,"axes.labelsize":9})

def savefig(fig,name):
    p=AS/name
    fig.savefig(p,dpi=180,bbox_inches="tight")
    plt.close(fig)
    return p

def fmt_pct(x): return f"{100*x:.1f}%"
def fmt_auc(x): return f"{x:.3f}"

def metric_rows(df,target,horizons=(5,15,30,60,120)):
    z=df[(df.target==target)&df.horizon.isin(horizons)].copy().sort_values("horizon")
    return z

# ---------- Charts ----------
# 1) Cluster path endpoints
def cluster_endpoint_plot():
    fig,axs=plt.subplots(1,2,figsize=(10.8,4.2),sharey=False)
    for ax,cl,title in [(axs[0],f_cl,"F&O"),(axs[1],n_cl,"Non-F&O")]:
        for _,r in cl.iterrows():
            xs=[0,30,60,120]
            ys=[0,r.mean_fwd30_bps,r.mean_fwd60_bps,r.mean_fwd120_bps]
            lab=f"C{int(r.cluster)} {r.direction} | mean run {r.mean_direction_run_min:.1f}m"
            col=PALETTE["green"] if r.direction=="UP" else PALETTE["red"]
            ax.plot(xs,ys,marker="o",linewidth=2,label=lab,color=col,alpha=0.85)
        ax.axhline(0,color="black",linewidth=.8)
        ax.set_title(f"{title}: natural future-path families")
        ax.set_xlabel("Minutes after observation")
        ax.set_ylabel("Mean underlying move (bps)")
        ax.grid(alpha=.18)
        ax.legend(fontsize=7,loc="best")
    fig.suptitle("Data-discovered 1-minute future-path families (corrected raw-path clustering)",fontsize=12,fontweight="bold")
    return savefig(fig,"cluster_paths.png")

# 2) AUC by horizon
def auc_plot():
    fig,ax=plt.subplots(figsize=(8.8,4.8))
    specs=[(f_metrics,"F&O UP",PALETTE["blue"],"o"),(f_metrics,"F&O DOWN",PALETTE["orange"],"o"),
           (n_metrics,"Non-F&O UP",PALETTE["green"],"s"),(n_metrics,"Non-F&O DOWN",PALETTE["red"],"s")]
    for df,label,col,mark in specs:
        target="up" if "UP" in label else "down"
        z=metric_rows(df,target)
        ax.plot(z.horizon,z.auc,marker=mark,label=label,color=col,linewidth=2)
    ax.axhline(.5,color="black",linewidth=.8,linestyle="--")
    ax.set_ylim(.50,.62)
    ax.set_xlabel("Persistence horizon (minutes)")
    ax.set_ylabel("Held-out 2025 AUC")
    ax.set_title("Ability to rank persistent moves in untouched 2025")
    ax.grid(alpha=.2)
    ax.legend(ncol=2,fontsize=8)
    return savefig(fig,"auc_horizons.png")

# 3) Top-decile lift
def lift_plot():
    fig,ax=plt.subplots(figsize=(8.8,4.8))
    specs=[(f_metrics,"F&O UP",PALETTE["blue"]), (f_metrics,"F&O DOWN",PALETTE["orange"]),
           (n_metrics,"Non-F&O UP",PALETTE["green"]), (n_metrics,"Non-F&O DOWN",PALETTE["red"])]
    for df,label,col in specs:
        target="up" if "UP" in label else "down"
        z=metric_rows(df,target)
        ax.plot(z.horizon,z.top10_lift,marker="o",label=label,color=col,linewidth=2)
    ax.axhline(1,color="black",linewidth=.8,linestyle="--")
    ax.set_xlabel("Persistence horizon (minutes)")
    ax.set_ylabel("Top-10% probability lift vs base rate")
    ax.set_title("Probability enrichment in the highest-ranked states")
    ax.grid(alpha=.2); ax.legend(ncol=2,fontsize=8)
    return savefig(fig,"lift_horizons.png")

# 4) Feature importance
def importance_plot(df,title,name):
    z=df.sort_values("logloss_increase",ascending=False).head(12).iloc[::-1]
    fig,ax=plt.subplots(figsize=(8.6,4.8))
    ax.barh(z.feature,z.logloss_increase,color=PALETTE["blue"] if "F&O" in title and "Non" not in title else PALETTE["teal"])
    ax.set_title(title)
    ax.set_xlabel("Held-out log-loss increase after feature permutation")
    ax.grid(axis="x",alpha=.18)
    return savefig(fig,name)

# 5) Comparison table chart - base vs top5 for 30m
def enrichment_plot():
    rows=[]
    for df,grp in [(f_metrics,"F&O"),(n_metrics,"Non-F&O")]:
        for t in ["up","down"]:
            r=df[(df.target==t)&(df.horizon==30)].iloc[0]
            rows.append((grp,t.upper(),r.base_rate,r.top5_rate))
    fig,ax=plt.subplots(figsize=(8.6,4.6))
    x=np.arange(len(rows)); width=.34
    base=np.array([r[2] for r in rows]); top=np.array([r[3] for r in rows])
    ax.bar(x-width/2,base,width,label="Base rate",color="#b7c3d0")
    ax.bar(x+width/2,top,width,label="Top 5% probability states",color=PALETTE["navy"])
    ax.set_xticks(x,[f"{g}\n{d}" for g,d,_,_ in rows])
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.set_ylabel("Observed 30-minute persistence rate")
    ax.set_title("30-minute persistence: base rate vs highest-probability states")
    ax.legend(); ax.grid(axis="y",alpha=.18)
    return savefig(fig,"enrichment_30m.png")

cluster_endpoint_plot(); auc_plot(); lift_plot()
importance_plot(f_imp,"F&O: variables with the most out-of-sample information","fno_importance.png")
importance_plot(n_imp,"Non-F&O: variables with the most out-of-sample information","nonfno_importance.png")
enrichment_plot()

# ---------- Fixed-rule examples ----------
def choose_example(pred, direction, success=True, horizon=30):
    pcol=f"p_{direction}_{horizon}"
    acol=f"actual_{direction}_duration"
    thr=pred[pcol].quantile(.95)
    z=pred[pred[pcol]>=thr].sort_values(["ts","symbol"])
    if success:
        z=z[z[acol]>=horizon]
    else:
        z=z[z[acol]<5]
    if z.empty: return None
    return z.iloc[0].to_dict()

examples=[
    ("F&O persistent UP",f_pred,"up",True),
    ("F&O persistent DOWN",f_pred,"down",True),
    ("Non-F&O persistent UP",n_pred,"up",True),
    ("Non-F&O persistent DOWN",n_pred,"down",True),
    ("Caution: high-ranked Non-F&O UP that failed quickly",n_pred,"up",False),
]
selected=[]
for label,pred,direction,success in examples:
    e=choose_example(pred,direction,success)
    e["_label"]=label; e["_direction"]=direction; e["_success"]=success
    selected.append(e)

# ---------- Download source 1-minute data for examples ----------
TREE_URL="https://api.github.com/repos/rahulkanadia/IndianMarkets_DataBackfill/git/trees/main?recursive=1"
tree=requests.get(TREE_URL,timeout=60).json()["tree"]
pathmap={}
for it in tree:
    p=it.get("path","")
    if it.get("type")=="blob" and p.endswith(".parquet") and "INDEX" not in p.upper():
        stem=Path(p).stem.upper()
        pathmap[stem]=p

def raw_parquet(symbol):
    p=pathmap.get(symbol.upper())
    if not p:
        raise FileNotFoundError(symbol)
    url="https://raw.githubusercontent.com/rahulkanadia/IndianMarkets_DataBackfill/main/"+urllib.parse.quote(p,safe="/")
    r=requests.get(url,timeout=90)
    r.raise_for_status()
    return pd.read_parquet(io.BytesIO(r.content))

def pickcol(cols,names):
    m={str(c).lower().replace("_","").replace(" ",""):c for c in cols}
    for n in names:
        k=n.lower().replace("_","").replace(" ","")
        if k in m:return m[k]
    return None

def get_path(example,minutes=120):
    sym=example["symbol"]; t0=pd.Timestamp(example["ts"])
    raw=raw_parquet(sym)
    tc=pickcol(raw.columns,["datetime","timestamp","date","time"])
    cc=pickcol(raw.columns,["close","closingprice","c"])
    if tc is None:
        if isinstance(raw.index,pd.DatetimeIndex):
            raw=raw.reset_index(); tc=raw.columns[0]
        else: raise RuntimeError("no ts")
    x=pd.DataFrame({"ts":pd.to_datetime(raw[tc],errors="coerce"),
                    "close":pd.to_numeric(raw[cc],errors="coerce")}).dropna().sort_values("ts")
    try:
        if x.ts.dt.tz is not None:
            x["ts"]=x.ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception: pass
    # exact timestamp or nearest within 2 min
    q=x[(x.ts>=t0-pd.Timedelta(minutes=2))&(x.ts<=t0+pd.Timedelta(minutes=minutes))]
    if q.empty: raise RuntimeError(f"no data {sym} {t0}")
    idx=(q.ts-t0).abs().idxmin()
    tbase=x.loc[idx,"ts"]; p0=float(x.loc[idx,"close"])
    z=x[(x.ts>=tbase)&(x.ts<=tbase+pd.Timedelta(minutes=minutes))].copy()
    z=z[z.ts.dt.normalize()==tbase.normalize()]
    z["minute"]=(z.ts-tbase).dt.total_seconds()/60
    z["bps"]=np.log(z.close/p0)*10000
    return z,tbase,p0

valid=[]
for i,e in enumerate(selected):
    try:
        z,tbase,p0=get_path(e)
        e["_path"]=z; e["_base_ts"]=tbase; e["_base_price"]=p0
        valid.append(e)
    except Exception as ex:
        e["_error"]=repr(ex)
        print("example path error",e.get("symbol"),ex,flush=True)

def example_plot(e,i):
    z=e["_path"]; direction=e["_direction"]; sym=e["symbol"]; t0=e["_base_ts"]
    fig,ax=plt.subplots(figsize=(8.8,3.9))
    ax.plot(z.minute,z.bps,color=PALETTE["green"] if direction=="up" else PALETTE["red"],linewidth=2)
    ax.axhline(0,color="black",linewidth=.8)
    for h in [15,30,60,120]:
        ax.axvline(h,color="#c7cdd4",linewidth=.8,linestyle="--")
    ax.set_xlim(0,120)
    ax.set_xlabel("Minutes after observation")
    ax.set_ylabel("Underlying move from observation (bps)")
    status="SUCCESS" if e["_success"] else "FAILURE CASE"
    ax.set_title(f"{sym} - {t0:%d %b %Y %H:%M} - {e['_label']} ({status})")
    ax.grid(alpha=.16)
    p15=e.get(f"p_{direction}_15",np.nan); p30=e.get(f"p_{direction}_30",np.nan); p60=e.get(f"p_{direction}_60",np.nan)
    dur=e.get(f"actual_{direction}_duration",np.nan)
    txt=f"P(15m) {p15:.1%}   P(30m) {p30:.1%}   P(60m) {p60:.1%}   actual same-side run {dur:.0f}m"
    ax.text(.01,.98,txt,transform=ax.transAxes,va="top",fontsize=8,
            bbox=dict(boxstyle="round,pad=.35",facecolor="white",alpha=.88,edgecolor="#aab2bd"))
    return savefig(fig,f"example_{i}_{sym}.png")

example_assets=[]
for i,e in enumerate(valid):
    example_assets.append((e,example_plot(e,i)))

# ---------- PDF ----------
PAGE_W,PAGE_H=A4
styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name="Title2",parent=styles["Title"],fontName="Helvetica-Bold",fontSize=23,leading=27,
                          textColor=colors.HexColor("#17324d"),spaceAfter=10))
styles.add(ParagraphStyle(name="SubTitle",parent=styles["Normal"],fontSize=11.5,leading=16,
                          textColor=colors.HexColor("#4b5563"),spaceAfter=12))
styles.add(ParagraphStyle(name="H1x",parent=styles["Heading1"],fontName="Helvetica-Bold",fontSize=16,leading=20,
                          textColor=colors.HexColor("#17324d"),spaceBefore=8,spaceAfter=8))
styles.add(ParagraphStyle(name="H2x",parent=styles["Heading2"],fontName="Helvetica-Bold",fontSize=12.5,leading=16,
                          textColor=colors.HexColor("#1f4e79"),spaceBefore=6,spaceAfter=5))
styles.add(ParagraphStyle(name="Bodyx",parent=styles["BodyText"],fontSize=9.4,leading=13.2,spaceAfter=6))
styles.add(ParagraphStyle(name="Small",parent=styles["BodyText"],fontSize=7.6,leading=10,textColor=colors.HexColor("#4b5563")))
styles.add(ParagraphStyle(name="Callout",parent=styles["BodyText"],fontSize=10.2,leading=14,
                          textColor=colors.HexColor("#17324d"),backColor=colors.HexColor("#edf3f7"),
                          borderColor=colors.HexColor("#9fb4c6"),borderWidth=.6,borderPadding=8,spaceBefore=5,spaceAfter=8))
styles.add(ParagraphStyle(name="CenterSmall",parent=styles["Small"],alignment=TA_CENTER))

def p(txt,style="Bodyx"): return Paragraph(txt,styles[style])

def img(path,width=170*mm):
    im=Image(str(path)); ratio=im.imageHeight/im.imageWidth
    im.drawWidth=width; im.drawHeight=width*ratio
    return im

def tbl(data,widths=None,header=True,font=8):
    t=Table(data,colWidths=widths,repeatRows=1 if header else 0,hAlign="LEFT")
    st=[("VALIGN",(0,0),(-1,-1),"MIDDLE"),("FONTNAME",(0,0),(-1,-1),"Helvetica"),
        ("FONTSIZE",(0,0),(-1,-1),font),("LEADING",(0,0),(-1,-1),font+2),
        ("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#c7d0d9")),
        ("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4),
        ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)]
    if header:
        st += [("BACKGROUND",(0,0),(-1,0),colors.HexColor("#17324d")),
               ("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold")]
    for r in range(1,len(data)):
        if r%2==0: st.append(("BACKGROUND",(0,r),(-1,r),colors.HexColor("#f7f9fb")))
    t.setStyle(TableStyle(st)); return t

def footer(canvas,doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#d7dde3")); canvas.line(18*mm,15*mm,PAGE_W-18*mm,15*mm)
    canvas.setFont("Helvetica",7.5); canvas.setFillColor(colors.HexColor("#6b7280"))
    canvas.drawString(18*mm,9*mm,"NIFTY 500 1-minute clean-slate research | Corrected raw-path analysis")
    canvas.drawRightString(PAGE_W-18*mm,9*mm,f"Page {doc.page}")
    canvas.restoreState()

story=[]
story += [Spacer(1,12*mm),p("NIFTY 500 1-Minute Clean-Slate Research","Title2"),
          p("Early directional movement and persistence - corrected historical analysis with held-out 2025 examples","SubTitle"),
          p("<b>Objective:</b> detect a stock's early move (up or down) and estimate how long that move is likely to persist - without forcing CALL V1, EMA/RSI/MACD, Z-score, option-preview logic or any predeclared ignition rule.","Callout"),
          Spacer(1,4*mm),
          tbl([["Research element","Frozen design"],
               ["Universe","Point-in-time NIFTY 500; historical F&O membership reconstructed from NSE derivatives files"],
               ["Input granularity","1-minute underlying price/volume data"],
               ["Sample","1,031,802 ordinary intraday states across 517 historically observed NIFTY-500 symbols"],
               ["Discovery","through 2022"],["Validation","2023-2024"],["Untouched test","2025"],
               ["Future horizons","3, 5, 10, 15, 30, 60 and 120 minutes"],
               ["Anti-overlap sampling","candidate states sampled on a 5-minute research grid; full 120-minute path required"],
               ["Target construction","raw future bps path, standardized only from discovery sample - no current-state feature inside target"],
               ["Controls","label permutation nulls + trading-day cluster bootstrap"]],[47*mm,123*mm],font=8.2),
          Spacer(1,5*mm),
          p("<b>Important correction:</b> an earlier pass normalized future paths with current 60-minute volatility. Because that current-state variable was also available to the predictor, the pass was rejected as potentially circular. This report uses only the corrected raw-path analysis.","Small"),
          PageBreak()]

story += [p("1. Executive finding","H1x"),
          p("The 1-minute data does contain statistically reproducible information about directional persistence, but the strength is different across F&O and non-F&O stocks. The strongest result is not a conventional crossover pattern. The data repeatedly emphasizes the stock's <b>volatility/range regime, displacement from session open, opening gap and market context</b>."),
          p("For F&O stocks, persistent UP states are more rankable than persistent DOWN states with the current feature set. For non-F&O stocks, both directions are materially more separable, with held-out AUCs around 0.59-0.60 for 15-120 minute persistence and substantial enrichment in the highest-probability states.","Callout"),
          img(AS/"auc_horizons.png",170*mm),Spacer(1,3*mm),img(AS/"lift_horizons.png",170*mm),
          PageBreak()]

story += [p("2. What future paths did the data discover?","H1x"),
          p("Future direction was not defined by a threshold. The complete 1-120 minute future paths were clustered first. The corrected F&O study selected <b>four</b> natural families; non-F&O selected <b>three</b>."),
          img(AS/"cluster_paths.png",178*mm),
          Spacer(1,3*mm),
          p("<b>Key distinction:</b> one F&O family ends strongly positive at 120 minutes but usually moves below the observation price almost immediately. That is not the same thing as an early persistent UP move. A scanner can therefore be directionally right about a later mover yet still provide poor entry timing.","Callout"),
          PageBreak()]

# F&O table
def persistence_table(metrics,grp):
    rows=[["Direction","Horizon","Base","Top 10%","Top 5%","Lift (Top 5%)","AUC","95% day-bootstrap CI"]]
    for d in ["up","down"]:
        for h in [5,15,30,60,120]:
            r=metrics[(metrics.target==d)&(metrics.horizon==h)].iloc[0]
            rows.append([d.upper(),f"{h}m",fmt_pct(r.base_rate),fmt_pct(r.top10_rate),fmt_pct(r.top5_rate),
                         f"{r.top5_lift:.2f}x",fmt_auc(r.auc),f"{r.auc_ci_lo:.3f}-{r.auc_ci_hi:.3f}"])
    return rows

story += [p("3. F&O: held-out 2025 persistence","H1x"),
          p("The corrected F&O model ranks UP persistence more effectively than DOWN persistence. At 30 minutes, the highest 5% UP states persisted 30 minutes in <b>12.8%</b> of observations versus an <b>8.6%</b> base rate (1.49x). The corresponding DOWN enrichment was smaller: 12.0% versus 9.6% (1.25x)."),
          tbl(persistence_table(f_metrics,"F&O"),[18*mm,17*mm,18*mm,20*mm,19*mm,22*mm,16*mm,35*mm],font=7.2),
          Spacer(1,4*mm),img(AS/"fno_importance.png",172*mm),
          p("The largest out-of-sample information contribution came from <b>60-minute realized volatility</b>, followed by NIFTY volatility, range structure, displacement from open, gap and candle shape. These are descriptors of market state, not conventional indicator crossovers.","Small"),
          PageBreak()]

story += [p("4. Non-F&O: stronger separation","H1x"),
          p("Non-F&O stocks show stronger probability separation. At 30 minutes, top-5% UP states persisted in <b>11.4%</b> of cases versus a 6.8% base rate (1.68x); top-5% DOWN states persisted in <b>14.2%</b> versus an 8.8% base rate (1.61x). At 120 minutes the enrichment rises to roughly 1.95x for UP and 1.80x for DOWN."),
          tbl(persistence_table(n_metrics,"Non-F&O"),[18*mm,17*mm,18*mm,20*mm,19*mm,22*mm,16*mm,35*mm],font=7.2),
          Spacer(1,4*mm),img(AS/"nonfno_importance.png",172*mm),
          PageBreak()]

story += [p("5. Probability enrichment at 30 minutes","H1x"),
          p("The chart below translates the model into the practical question: if we only look at the highest-ranked states, how often does same-direction movement actually remain continuously on the correct side for 30 minutes?"),
          img(AS/"enrichment_30m.png",172*mm),
          p("These are <b>underlying persistence probabilities</b>, not option P&L probabilities. They deliberately ignore option premium, IV, theta, spreads and strike selection because the research objective is to understand the underlying move first.","Callout"),
          PageBreak()]

# examples pages
story += [p("6. Held-out 2025 examples from a fixed selection rule","H1x"),
          p("To avoid hand-picking attractive charts, examples were selected mechanically: for each group/direction, take the <b>first chronological 2025 observation</b> inside the top 5% predicted probability bucket for 30-minute persistence that actually survived 30 minutes. The caution case is the first chronological top-5% Non-F&O UP observation that failed within 5 minutes."),
          p("The lines below are reconstructed from the source 1-minute underlying data. Probability values are those generated before observing the future path.","Small"),
          Spacer(1,2*mm)]

for idx,(e,path) in enumerate(example_assets):
    d=e["_direction"]
    actual=e.get(f"actual_{d}_duration",np.nan)
    status="confirmed 30-minute persistence" if e["_success"] else "high-ranked state that failed within 5 minutes"
    story += [KeepTogether([
        p(f"{e['_label']}: {e['symbol']} - {pd.Timestamp(e['_base_ts']):%d %b %Y, %H:%M}","H2x"),
        p(f"Predicted P(15m) <b>{e.get(f'p_{d}_15',np.nan):.1%}</b>, P(30m) <b>{e.get(f'p_{d}_30',np.nan):.1%}</b>, P(60m) <b>{e.get(f'p_{d}_60',np.nan):.1%}</b>. Actual same-side run: <b>{actual:.0f} minutes</b> - {status}.","Small"),
        img(path,168*mm),Spacer(1,3*mm)
    ])]
    if idx in [1,3]: story.append(PageBreak())

story += [PageBreak(),p("7. Interpretation for DBIndicator","H1x"),
          p("The test supports a change in research direction, not a production deployment. The useful signal is a <b>state-transition probability</b>: how current range/volatility/displacement/market conditions change the odds that price will stay on one side of the current price for 5, 15, 30, 60 or 120 minutes."),
          p("The next clean test should therefore be <b>sequential onset detection</b>: replay every minute, track the probability surface through time, and record the first minute at which persistence probability crosses a frozen threshold. The key outcomes should be detection delay, false alerts per stock-day, percentage of the move already consumed before detection, remaining MFE/MAE, and remaining same-side duration.","Callout"),
          p("<b>What should not happen next:</b> do not convert these findings directly into another arbitrary score, do not require an option contract to exist before detecting the underlying state, and do not force persistence by holding an alert for a minimum time. The underlying probability should rise and fall naturally."),
          p("<b>What the current study does not prove:</b> that a top-ranked state is automatically tradeable after costs; that the same probabilities hold in 2026 live data; that a particular option strike will profit; or that 5-minute sampling is the optimal real-time alert cadence. Those require the next replay and shadow stages."),
          Spacer(1,6*mm),
          p("Research status","H2x"),
          tbl([["Item","Status"],["Corrected 1-minute clean-slate study","Complete"],
               ["F&O/non-F&O split","Complete"],["Untouched 2025 validation","Complete"],
               ["Permutation + trading-day bootstrap controls","Complete"],
               ["Sequential first-onset replay","Next"],
               ["Production Actionable Desk change","Not approved / not deployed"]],[70*mm,100*mm],font=8.2),
          Spacer(1,5*mm),
          p("This document summarizes historical research, not investment advice. All example charts are underlying-price observations from the held-out 2025 test period and were selected by the fixed rule described above.","Small")]

pdf=OUT/"NIFTY500_1m_clean_slate_findings_with_examples.pdf"
doc=SimpleDocTemplate(str(pdf),pagesize=A4,rightMargin=17*mm,leftMargin=17*mm,topMargin=16*mm,bottomMargin=20*mm,
                      title="NIFTY 500 1-Minute Clean-Slate Findings")
doc.build(story,onFirstPage=footer,onLaterPages=footer)

# Example manifest
manifest=[]
for e in valid:
    d=e["_direction"]
    manifest.append({
        "label":e["_label"],"symbol":e["symbol"],"timestamp":str(e["_base_ts"]),
        "direction":d,"p15":float(e.get(f"p_{d}_15",np.nan)),
        "p30":float(e.get(f"p_{d}_30",np.nan)),"p60":float(e.get(f"p_{d}_60",np.nan)),
        "actual_duration_min":float(e.get(f"actual_{d}_duration",np.nan)),
        "selection_rule":"first chronological 2025 row in top 5% p(direction persists 30m) bucket; success requires actual duration >=30m; caution case actual duration <5m"
    })
pd.DataFrame(manifest).to_csv(OUT/"example_manifest.csv",index=False)
print(pdf)
print(pd.DataFrame(manifest).to_string(index=False))
