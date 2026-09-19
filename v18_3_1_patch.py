from pathlib import Path
import sys, shutil, py_compile

if len(sys.argv)!=2: raise SystemExit("Usage: py v18_3_1_patch.py <dashboard.py>")
p=Path(sys.argv[1]).resolve()
if not p.exists(): raise SystemExit(f"Not found: {p}")
src=p.read_text(encoding="utf-8")
if 'V18.3-NEWSINTEL-1.3' not in src: raise SystemExit("ABORT: V18.3 base not detected.")
bak=p.with_name("dashboard_V18_3_BEFORE_V18_3_1.py"); shutil.copy2(p,bak)

src=src.replace('V16_ENGINE_VERSION = "V18.3-NEWSINTEL-1.3"','V16_ENGINE_VERSION = "V18.3.1-NEWSINTEL-1.31"',1)
src=src.replace('### V18.3 News & Event Intelligence','### V18.3.1 News & Event Intelligence',1)
src=src.replace('V18.3 NEWS + QUANT ENGINE','V18.3.1 NEWS + QUANT ENGINE',1)

start=src.index('def _news_reddit(query, limit=8):')
end=src.index('\ndef _news_bundle(targets):', start)
new=r'''def _news_reddit(query, limit=8):
    # Public forum metadata, but only raw collection here; relevance is enforced below.
    try:
        import xml.etree.ElementTree as ET
        q=urllib.parse.quote_plus(query)
        url=f"https://www.reddit.com/search.rss?q={q}&sort=new&t=day"
        req=urllib.request.Request(url,headers={"User-Agent":"NordnetPaperResearch/18.3.1"})
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

def _news_tokens(query):
    import re
    q=re.sub(r'[^A-Za-z0-9 ]+',' ',str(query).lower())
    stop={"or","and","the","a","an","markets","stocks"}
    return {x for x in q.split() if len(x)>=3 and x not in stop}

def _news_is_fresh(seen, hours=30):
    try:
        ts=pd.to_datetime(seen,utc=True)
        return (pd.Timestamp.now(tz="UTC")-ts).total_seconds() <= hours*3600
    except Exception:
        return False

def _news_relevant(item, query, target_key):
    title=str(item.get("title","")).lower()
    if not _news_is_fresh(item.get("seen","")): return False, "stale_or_unparseable"
    toks=_news_tokens(query)
    if target_key=="MACRO":
        # Require finance/economy context AND an actual macro/geopolitical concept.
        finance={"market","markets","stock","stocks","shares","investor","investors","economy","economic","oil","gas","inflation","tariff","tariffs","sanction","sanctions","central","bank","rates"}
        event={"inflation","interest","rate","rates","oil","gas","tariff","tariffs","sanction","sanctions","war","conflict","attack","ceasefire","recession","gdp","unemployment"}
        if not any(w in title for w in finance): return False, "no_finance_context"
        if not any(w in title for w in event): return False, "no_macro_event"
        # Prevent obvious entertainment false positives such as Gears of War.
        if any(x in title for x in ("gears of war","star wars","warhammer","call of duty")): return False, "entertainment_collision"
        return True, "accepted_macro"
    # Company target: require meaningful company-query token in title.
    strong={t for t in toks if len(t)>=4}
    if not strong or not any(t in title for t in strong): return False, "company_not_in_title"
    return True, "accepted_company"

def _news_analyze(items, query="", target_key=""):
    seen=set(); accepted=[]; rejected=[]; duplicate_count=0
    for x in items:
        key=" ".join(str(x.get("title","")).lower().split())
        if not key: continue
        if key in seen:
            duplicate_count+=1; continue
        seen.add(key)
        ok,why=_news_relevant(x,query,target_key)
        if ok: accepted.append(x)
        else:
            y=dict(x); y["rejection_reason"]=why; rejected.append(y)
    weighted=0.0; denom=0.0; cats={}; enriched=[]
    category_scores={}
    for x in accepted:
        title=str(x.get("title","")); low=title.lower()
        pos=sum(1 for w in _NEWS_POS if w in low); neg=sum(1 for w in _NEWS_NEG if w in low)
        raw=max(-3,min(3,pos-neg))
        domain=str(x.get("domain","")).lower(); source_type=x.get("source_type","news")
        quality=1.0 if any(d in domain for d in _HIGH_QUALITY_DOMAINS) else (0.25 if source_type=="forum" else 0.65)
        item_cats=[]
        for cat,words in _EVENT_WORDS.items():
            if any(w in low for w in words):
                cats[cat]=cats.get(cat,0)+1; item_cats.append(cat)
                category_scores.setdefault(cat,[]).append(raw*quality)
        weighted+=raw*quality; denom+=3.0*quality
        y=dict(x); y["headline_sentiment"]=raw; y["source_weight"]=quality; y["categories"]=item_cats; enriched.append(y)
    score=round(100.0*weighted/denom,1) if denom else 0.0
    cat_out={k:round(sum(v)/max(len(v),1),3) for k,v in category_scores.items()}
    return {"score":score,"article_count":len(enriched),"accepted_count":len(enriched),
            "rejected_count":len(rejected),"duplicate_count":duplicate_count,
            "categories":cats,"category_scores":cat_out,"items":enriched[:12],
            "rejected_samples":rejected[:8]}
'''
src=src[:start]+new+src[end:]

src=src.replace('result["targets"][key]=_news_analyze(items)',
                'result["targets"][key]=_news_analyze(items, query=query, target_key=key)',1)

old='''news_table.append({"Target":key,"News score":info.get("score",0.0),"Unique items":info.get("article_count",0),
                       "Event categories":", ".join(f"{k}:{v}" for k,v in info.get("categories",{}).items()) or "—"})'''
newtable='''news_table.append({"Target":key,"News score":info.get("score",0.0),
                       "Accepted":info.get("accepted_count",info.get("article_count",0)),
                       "Rejected":info.get("rejected_count",0),
                       "Duplicates":info.get("duplicate_count",0),
                       "Event categories":", ".join(f"{k}:{v}" for k,v in info.get("categories",{}).items()) or "—"})'''
if old not in src: raise SystemExit("ABORT: news table anchor not found")
src=src.replace(old,newtable,1)

oldcap='Headlines/metadata from global news plus best-effort public forum metadata. Source weighting, deduplication and event tags are applied. News cannot override failed quantitative validation.'
newcap='Fresh headline metadata from global news plus best-effort public forums. V18.3.1 rejects stale/off-topic matches before scoring, deduplicates evidence, separates event categories and heavily discounts forum evidence. News cannot override failed quantitative validation.'
src=src.replace(oldcap,newcap,1)

src=src.replace('"method": "headline metadata + dedup + source weighting + event tags; not a profit prediction",',
'''"method": "freshness + relevance gate + dedup + source weighting + event/category scores; not a profit prediction",
            "relevance_filter": "V18.3.1 strict",''',1)

p.write_text(src,encoding="utf-8")
try: py_compile.compile(str(p),doraise=True)
except Exception:
    shutil.copy2(bak,p); raise
print(f"PATCHED: {p}")
print(f"BACKUP:  {bak}")
print("SYNTAX:  PASS")
print("ENGINE:  V18.3.1-NEWSINTEL-1.31")
print("NEWS:    strict freshness/relevance gates + accepted/rejected/duplicate audit")
print("HEARTBEAT: remains ON by default")
