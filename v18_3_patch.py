from pathlib import Path
import sys, shutil, py_compile

if len(sys.argv)!=2: raise SystemExit("Usage: py v18_3_patch.py <dashboard.py>")
p=Path(sys.argv[1]).resolve()
if not p.exists(): raise SystemExit(f"Not found: {p}")
src=p.read_text(encoding="utf-8")
if 'V18.2-BEARPROOF-1.2' not in src: raise SystemExit("ABORT: V18.2 base not detected.")
bak=p.with_name("dashboard_V18_2_BEFORE_V18_3.py"); shutil.copy2(p,bak)

src=src.replace('V16_ENGINE_VERSION = "V18.2-BEARPROOF-1.2"','V16_ENGINE_VERSION = "V18.3-NEWSINTEL-1.3"',1)
src=src.replace('V18.2 Failure Envelope','V18.3 Failure Envelope',1)
src=src.replace('V18.2 treats stop-loss','V18.3 treats stop-loss',1)
src=src.replace('eligible V18.2 BUY/EXIT','eligible V18.3 BUY/EXIT',1)
src=src.replace('V18.2 BEARPROOF ENGINE','V18.3 NEWS + QUANT ENGINE',1)

# News/event intelligence helpers: keyless GDELT + best-effort Reddit RSS, metadata/headlines only.
anchor='\nSTARTING_BALANCE = 2000.0\n'
helpers=r'''
# =========================
# V18.3 NEWS / EVENT INTELLIGENCE
# =========================
NEWS_CACHE_FILE = Path("paper_data/news_intel_cache.json")
NEWS_CACHE_SECONDS = 900
NEWS_MAX_ARTICLES = 20

_NEWS_POS = {"beat","beats","growth","profit","profits","surge","surges","upgrade","upgrades","record","approval","approved","contract","win","wins","rally","strong","raises","raised"}
_NEWS_NEG = {"miss","misses","loss","losses","cuts","cut","downgrade","downgrades","lawsuit","probe","investigation","fraud","bankruptcy","default","recall","warning","sanction","sanctions","war","attack","attacks","strike","strikes","conflict","crisis"}
_EVENT_WORDS = {
    "geopolitics": {"war","attack","attacks","missile","sanction","sanctions","ceasefire","invasion","conflict","military"},
    "politics_regulation": {"government","minister","president","election","parliament","congress","regulator","regulation","tariff","tax"},
    "earnings_company": {"earnings","revenue","profit","loss","guidance","dividend","contract","acquisition","merger"},
    "macro": {"inflation","interest rate","rates","central bank","recession","gdp","unemployment","oil","gas"},
}
_HIGH_QUALITY_DOMAINS = ("reuters.com","apnews.com","ft.com","wsj.com","bloomberg.com","cnbc.com","bbc.","nrk.no","e24.no","dn.no")

def _news_fetch_json(url, timeout=8):
    req=urllib.request.Request(url,headers={"User-Agent":"NordnetPaperResearch/18.3"})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8","replace"))

def _news_gdelt(query, timespan="24h", maxrecords=NEWS_MAX_ARTICLES):
    try:
        params=urllib.parse.urlencode({"query":query,"mode":"artlist","format":"json","timespan":timespan,
                                      "maxrecords":maxrecords,"sort":"datedesc"})
        data=_news_fetch_json("https://api.gdeltproject.org/api/v2/doc/doc?"+params)
        out=[]
        for a in data.get("articles",[])[:maxrecords]:
            out.append({"title":str(a.get("title","")),"url":str(a.get("url","")),
                        "domain":str(a.get("domain","")),"seen":str(a.get("seendate","")),
                        "source_type":"news"})
        return out
    except Exception:
        return []

def _news_reddit(query, limit=8):
    # Best-effort public forum metadata. Failure never blocks the trading research engine.
    try:
        import xml.etree.ElementTree as ET
        q=urllib.parse.quote_plus(query)
        url=f"https://www.reddit.com/search.rss?q={q}&sort=new&t=day"
        req=urllib.request.Request(url,headers={"User-Agent":"NordnetPaperResearch/18.3"})
        with urllib.request.urlopen(req,timeout=6) as resp:
            root=ET.fromstring(resp.read())
        ns={"a":"http://www.w3.org/2005/Atom"}; out=[]
        for e in root.findall("a:entry",ns)[:limit]:
            title=e.findtext("a:title",default="",namespaces=ns)
            link=e.find("a:link",ns)
            out.append({"title":title,"url":link.get("href","") if link is not None else "",
                        "domain":"reddit.com","seen":e.findtext("a:updated",default="",namespaces=ns),
                        "source_type":"forum"})
        return out
    except Exception:
        return []

def _news_analyze(items):
    seen=set(); unique=[]
    for x in items:
        key=" ".join(str(x.get("title","")).lower().split())
        if not key or key in seen: continue
        seen.add(key); unique.append(x)
    weighted=0.0; denom=0.0; cats={}
    enriched=[]
    for x in unique:
        title=str(x.get("title","")); low=title.lower()
        pos=sum(1 for w in _NEWS_POS if w in low); neg=sum(1 for w in _NEWS_NEG if w in low)
        raw=max(-3,min(3,pos-neg))
        domain=str(x.get("domain","")).lower()
        source_type=x.get("source_type","news")
        quality=1.0 if any(d in domain for d in _HIGH_QUALITY_DOMAINS) else (0.35 if source_type=="forum" else 0.65)
        for cat,words in _EVENT_WORDS.items():
            if any(w in low for w in words): cats[cat]=cats.get(cat,0)+1
        weighted += raw*quality; denom += 3.0*quality
        y=dict(x); y["headline_sentiment"]=raw; y["source_weight"]=quality; enriched.append(y)
    score=round(100.0*weighted/denom,1) if denom else 0.0
    return {"score":score,"article_count":len(enriched),"categories":cats,"items":enriched[:12]}

def _news_bundle(targets):
    now=datetime.now(timezone.utc)
    try:
        if NEWS_CACHE_FILE.exists():
            cached=json.loads(NEWS_CACHE_FILE.read_text(encoding="utf-8"))
            ts=pd.to_datetime(cached.get("timestamp_utc"),utc=True).to_pydatetime()
            if (now-ts).total_seconds()<NEWS_CACHE_SECONDS and cached.get("target_keys")==targets:
                return cached
    except Exception: pass
    result={"timestamp_utc":now.isoformat(),"target_keys":targets,"targets":{}}
    for key,query in targets.items():
        items=_news_gdelt(query)+_news_reddit(query)
        result["targets"][key]=_news_analyze(items)
    try:
        NEWS_CACHE_FILE.parent.mkdir(parents=True,exist_ok=True)
        NEWS_CACHE_FILE.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    except Exception: pass
    return result
'''
if anchor not in src: raise SystemExit("ABORT: config anchor not found")
src=src.replace(anchor,"\n"+helpers+anchor,1)

# Add research-only news collection after validation, before automatic decision.
decision_anchor='\nvalidated_candidates = []\n'
news_block=r'''
# V18.3 news intelligence is deliberately RESEARCH-ONLY until forward testing shows
# incremental value. It cannot turn a validation FAIL into a trade.
news_targets = {
    "MACRO": '(markets OR stocks OR inflation OR "interest rates" OR oil OR gas OR war OR sanctions OR tariff)'
}
if "validation_rows" in locals() and not ranked_df.empty:
    pass_symbols=[str(x.get("Symbol","")) for x in validation_rows if x.get("Candidate Filters")=="PASS"][:3]
    for sym in pass_symbols:
        rr=ranked_df[ranked_df["Symbol"].astype(str)==sym]
        if not rr.empty:
            company=str(rr.iloc[0].get("Company") or "").strip()
            # Company name is preferred to reduce ticker ambiguity.
            query=f'"{company}"' if company and company.lower()!=sym.lower() else f'"{sym}"'
            news_targets[sym]=query

news_intel=_news_bundle(news_targets)
st.write("### V18.3 News & Event Intelligence — RESEARCH ONLY")
st.caption("Headlines/metadata from global news plus best-effort public forum metadata. Source weighting, deduplication and event tags are applied. News cannot override failed quantitative validation.")
news_table=[]
for key,info in news_intel.get("targets",{}).items():
    news_table.append({"Target":key,"News score":info.get("score",0.0),"Unique items":info.get("article_count",0),
                       "Event categories":", ".join(f"{k}:{v}" for k,v in info.get("categories",{}).items()) or "—"})
if news_table: st.dataframe(pd.DataFrame(news_table),width="stretch",hide_index=True)
with st.expander("News/event evidence"):
    for key,info in news_intel.get("targets",{}).items():
        st.write(f"**{key}** — score {info.get('score',0):+.1f}")
        for item in info.get("items",[])[:8]:
            st.write(f"- [{item.get('source_type','news').upper()}] {item.get('title','')} — {item.get('domain','')}")
'''
if decision_anchor not in src: raise SystemExit("ABORT: decision anchor not found")
src=src.replace(decision_anchor,"\n"+news_block+decision_anchor,1)

# Add news to heartbeat.
status_anchor='''        "rejection_summary": {'''
news_status='''        "news_intelligence": news_intel if "news_intel" in locals() else None,
        "news_policy": {
            "mode": "RESEARCH_ONLY",
            "can_override_validation": False,
            "cache_seconds": NEWS_CACHE_SECONDS,
            "sources": ["GDELT DOC 2.0", "Reddit public RSS best-effort"],
            "method": "headline metadata + dedup + source weighting + event tags; not a profit prediction",
        },
        "rejection_summary": {'''
if status_anchor not in src: raise SystemExit("ABORT: status anchor not found")
src=src.replace(status_anchor,news_status,1)

# Monitoring heartbeat should survive app restarts as ON by default.
old='auto_refresh = st.toggle("Auto-refresh dashboard every 30 seconds", value=False, key="test_auto_refresh")'
new='auto_refresh = st.toggle("Auto-refresh dashboard every 30 seconds", value=True, key="test_auto_refresh")'
if old not in src: raise SystemExit("ABORT: heartbeat toggle anchor not found")
src=src.replace(old,new,1)

p.write_text(src,encoding="utf-8")
try: py_compile.compile(str(p),doraise=True)
except Exception:
    shutil.copy2(bak,p); raise
print(f"PATCHED: {p}")
print(f"BACKUP:  {bak}")
print("SYNTAX:  PASS")
print("ENGINE:  V18.3-NEWSINTEL-1.3")
print("NEWS:    GDELT + best-effort Reddit, 15-minute cache, research-only")
print("HEARTBEAT: defaults ON after restart")
