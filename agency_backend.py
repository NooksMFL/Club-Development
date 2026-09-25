import shutil
from pathlib import Path
import os, sqlite3, requests, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE="https://api.playmfl.com"; DB="agency_development.db"
SEED_DB=Path(__file__).with_name("agency_seed.db")

def ensure_seed_database():
 try:
  p=Path(DB)
  needs=(not p.exists()) or p.stat().st_size < 4096
  if not needs:
   c=sqlite3.connect(DB)
   try:
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    needs=("ownership_v65" not in tables or c.execute("SELECT COUNT(*) FROM ownership_v65").fetchone()[0]==0)
   finally:
    c.close()
  if needs and SEED_DB.exists():
   shutil.copy2(SEED_DB,p)
   return True
 except Exception:
  return False
 return False

SEEDED_ON_START=ensure_seed_database()

H={"Accept":"*/*","Origin":"https://app.playmfl.com","Referer":"https://app.playmfl.com/",
"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0"}
STATS=("overall","pace","shooting","passing","dribbling","defense","physical")

def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init(c):
 c.executescript("""CREATE TABLE IF NOT EXISTS ownership_v65(
 wallet TEXT,player_id INTEGER,player_name TEXT,source TEXT,confidence TEXT,acquired_at TEXT,history_start TEXT,
 start_ovr REAL,start_pac REAL,start_sho REAL,start_pas REAL,start_dri REAL,start_def REAL,start_phy REAL,
 current_ovr REAL,current_pac REAL,current_sho REAL,current_pas REAL,current_dri REAL,current_def REAL,current_phy REAL,
 progression_owned INTEGER DEFAULT 0,last_progression TEXT,first_progression TEXT,event_count INTEGER DEFAULT 0,updated_at TEXT,
 PRIMARY KEY(wallet,player_id));
 CREATE TABLE IF NOT EXISTS tags(wallet TEXT,player_id INTEGER,tag TEXT,note TEXT,PRIMARY KEY(wallet,player_id));"""); c.commit()

def token():
 rt=os.getenv("MFL_REFRESH_TOKEN")
 if not rt: raise RuntimeError("MFL_REFRESH_TOKEN missing")
 last=None
 for attempt in range(4):
  try:
   r=requests.post(BASE+"/auth/refresh",headers=H,json={"refreshToken":rt},timeout=20)
   if r.status_code in (429,500,502,503,504):
    last=f"HTTP {r.status_code}: {r.text[:300]}"
    time.sleep(min(15,2*(2**attempt)))
    continue
   r.raise_for_status()
   d=r.json()
   a=d.get("access") or ((d.get("data") or {}).get("access") if isinstance(d.get("data"),dict) else None)
   if isinstance(a,dict): a=a.get("token")
   if not a: raise RuntimeError("No access token returned")
   return a
  except (requests.Timeout, requests.ConnectionError) as e:
   last=str(e); time.sleep(min(15,2*(2**attempt)))
 raise RuntimeError("MFL authentication is temporarily failing after retries. "
                    "This is an API/server response, not a lost agency database. "
                    f"Last response: {last}")

def ah(t):
 h=dict(H);h["Authorization"]="Bearer "+t;return h
def get(path,t,params=None):
 for attempt in range(5):
  r=requests.get(BASE+path,headers=ah(t),params=params,timeout=30)
  if r.status_code==429:
   wait=int(r.headers.get("Retry-After") or min(60,5*(2**attempt)))
   time.sleep(wait)
   continue
  if not r.ok:raise RuntimeError(f"{path} returned {r.status_code}: {r.text[:180]}")
  return r.json()
 raise RuntimeError(f"{path} is still rate-limited after retries. Please wait and try again later.")

def arr(d):
 if isinstance(d,list):return d
 if isinstance(d,dict):
  for k in ("data","items","results","players","listings","history"):
   x=d.get(k)
   if isinstance(x,list):return x
   if isinstance(x,dict):
    y=arr(x)
    if y:return y
 return []
def pid(p):
 for x in (p,p.get("metadata") or {},p.get("player") or {}):
  for k in ("id","playerId","playerID"):
   if x.get(k) is not None:return int(x[k])
 raise ValueError("No player id")
def roster(wallet,t):
 return arr(get("/players",t,{"ownerWalletAddress":wallet,"limit":1200}))
def unwrap(p):
 if isinstance(p,dict):
  for k in ("data","player"):
   if isinstance(p.get(k),dict):p=p[k]
 return p or {}
def stat(p,*names):
 p=unwrap(p);m=p.get("metadata") or {};a=p.get("attributes") or p.get("stats") or {};r=p.get("ratings") or {}
 for src in (p,a,r,m):
  for n in names:
   if isinstance(src,dict) and src.get(n) is not None:return src[n]

def deep_values(obj, key_names):
 keys={str(k).lower() for k in key_names}
 found=[]
 def walk(x,path=""):
  if isinstance(x,dict):
   for k,v in x.items():
    p=f"{path}.{k}" if path else str(k)
    if str(k).lower() in keys and v not in (None,"",[],{}): found.append((p,v))
    walk(v,p)
  elif isinstance(x,list):
   for i,v in enumerate(x): walk(v,f"{path}[{i}]")
 walk(obj)
 return found

def first_scalar(obj, names):
 for _,v in deep_values(obj,names):
  if isinstance(v,(str,int,float,bool)): return v
 return None

def player_meta_from_payload(payload):
 p=unwrap(payload)
 m=p.get("metadata") if isinstance(p,dict) else {}
 if not isinstance(m,dict): m={}
 age=m.get("age")
 positions=m.get("positions") or []
 if isinstance(positions,str): positions=[positions]
 position=" / ".join(str(x) for x in positions if x) or None

 # Diagnostic confirmed the existing club search resolves the current club.
 club=None
 for key in ("activeContract","contract","currentClub","club"):
  candidates=deep_values(p,[key])
  for _,v in candidates:
   if isinstance(v,dict):
    club=first_scalar(v,["name","clubName","teamName"])
   elif isinstance(v,str):
    club=v
   if club: break
  if club: break
 return {"age":age,"position":position,"club":club}

def profile(pid,t):
 p=unwrap(get(f"/players/{pid}",t));m=p.get("metadata") or {}
 name=p.get("name") or m.get("name")
 if not name:name=(str(p.get("firstName") or m.get("firstName") or "")+" "+str(p.get("lastName") or m.get("lastName") or "")).strip()
 meta=player_meta_from_payload(p)
 return {"name":name or f"Player {pid}","age":meta.get("age"),"position":meta.get("position"),"club":meta.get("club"),
 "overall":stat(p,"overall","overallRating","ovr"),"pace":stat(p,"pace","PAC"),
 "shooting":stat(p,"shooting","SHO"),"passing":stat(p,"passing","PAS"),"dribbling":stat(p,"dribbling","DRI"),
 "defense":stat(p,"defense","defending","DEF"),"physical":stat(p,"physical","physicality","PHY")}
def todt(v):
 if v is None:return None
 try:
  x=float(v)
  if x>1e10:x/=1000
  return datetime.fromtimestamp(x,tz=timezone.utc)
 except:
  try:return datetime.fromisoformat(str(v).replace("Z","+00:00"))
  except:return None
def iso(x):return x.isoformat() if x else None
def sale_history(pid,t):return arr(get("/listings/feed",t,{"limit":25,"playerId":pid}))
def exp_history(pid,t):return arr(get(f"/players/{pid}/experiences/history",t))
def acquire(sales,wallet):
 w=wallet.lower(); buys=[]
 for e in sales:
  buyer=str(e.get("buyerAddress") or e.get("buyerWalletAddress") or "").lower()
  when=todt(e.get("purchaseDateTime") or e.get("createdAt") or e.get("date"))
  status=str(e.get("status") or "").upper()
  if buyer==w and when and (not status or status=="BOUGHT"):buys.append((when,e))
 if buys:return max(buys,key=lambda z:z[0])[0],"BOUGHT","VERIFIED"
 return None,"NO MARKET PURCHASE","UNVERIFIED"
def event_values(e):
 vals=e.get("values") or e.get("attributes") or {}
 out={}
 aliases={"overall":("overall","overallRating","ovr"),"pace":("pace","PAC"),"shooting":("shooting","SHO"),
 "passing":("passing","PAS"),"dribbling":("dribbling","DRI"),"defense":("defense","defending","DEF"),"physical":("physical","physicality","PHY")}
 for k,names in aliases.items():
  for src in (vals,e):
   for n in names:
    if isinstance(src,dict) and src.get(n) is not None:out[k]=src[n];break
   if k in out:break
 return out
def reconstruct(exps,acq,current):
 parsed=[]
 for e in exps:
  when=todt(e.get("date") or e.get("createdAt") or e.get("timestamp"))
  if when:parsed.append((when,e))
 parsed.sort(key=lambda z:z[0])
 initials=[x for x in parsed if str(x[1].get("reasonType") or x[1].get("type") or x[1].get("eventType") or "").upper()=="INITIAL"]
 if acq:
  effective=acq; baseline_label="PURCHASE"
 elif initials:
  effective=initials[0][0]; baseline_label="INITIAL"
 else:
  effective=None; baseline_label="UNKNOWN"
 state={k:None for k in STATS}; owned=0;last=None
 if effective:
  # Latest complete/partial state at or before the ownership anchor.
  for when,e in parsed:
   if when<=effective:
    for k,v in event_values(e).items():state[k]=v
   if when>=effective:
    owned+=1;last=when
 # Do not manufacture historical gains from today's profile.
 complete=all(state[k] is not None for k in STATS)
 if not complete:
  return effective,None,owned,last,baseline_label,parsed
 return effective,state,owned,last,baseline_label,parsed

def analyse(pid,wallet,t):
 cur=profile(pid,t);sales=sale_history(pid,t);exps=exp_history(pid,t);acq,source,confidence=acquire(sales,wallet)
 effective,start,owned,last,baseline_label,parsed=reconstruct(exps,acq,cur)
 # No marketplace purchase + INITIAL is evidence of an original/minted history, but not proof this wallet minted it.
 if source=="NO MARKET PURCHASE" and baseline_label=="INITIAL":
  source="NEW MINT / ORIGINAL"
  confidence="INITIAL HISTORY"
 if start is None:
  start={k:None for k in STATS}
 first=parsed[0][0] if parsed else None
 return pid,cur,source,confidence,(acq if confidence=="VERIFIED" else None),effective,start,owned,last,first,len(parsed)
def sync(wallet,progress=None,batch_size=12):
 wallet=wallet.strip().lower();t=token()
 ids=list(dict.fromkeys(pid(x) for x in roster(wallet,t)))
 c=db();init(c)
 cached={r["player_id"]:r for r in c.execute("SELECT * FROM ownership_v65 WHERE wallet=?",(wallet,))}
 uncached=[x for x in ids if x not in cached]
 todo=uncached[:batch_size]
 results=[];errors=[]
 for n,x in enumerate(todo,1):
  try:results.append(analyse(x,wallet,t))
  except Exception as e:
   errors.append((x,str(e)))
   if "rate-limit" in str(e).lower() or "429" in str(e):break
  if progress:progress(n,len(todo))
  time.sleep(0.45)
 now=datetime.now(timezone.utc).isoformat()
 for player_id,cur,source,confidence,acq,hstart,start,owned,last,first,event_count in results:
  c.execute("""INSERT INTO ownership_v65 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  ON CONFLICT(wallet,player_id) DO UPDATE SET player_name=excluded.player_name,source=excluded.source,confidence=excluded.confidence,
  acquired_at=excluded.acquired_at,history_start=excluded.history_start,start_ovr=excluded.start_ovr,start_pac=excluded.start_pac,
  start_sho=excluded.start_sho,start_pas=excluded.start_pas,start_dri=excluded.start_dri,start_def=excluded.start_def,start_phy=excluded.start_phy,
  current_ovr=excluded.current_ovr,current_pac=excluded.current_pac,current_sho=excluded.current_sho,current_pas=excluded.current_pas,
  current_dri=excluded.current_dri,current_def=excluded.current_def,current_phy=excluded.current_phy,progression_owned=excluded.progression_owned,
  last_progression=excluded.last_progression,first_progression=excluded.first_progression,event_count=excluded.event_count,updated_at=excluded.updated_at""",
  (wallet,player_id,cur["name"],source,confidence,iso(acq),iso(hstart),
   start["overall"],start["pace"],start["shooting"],start["passing"],start["dribbling"],start["defense"],start["physical"],
   cur["overall"],cur["pace"],cur["shooting"],cur["passing"],cur["dribbling"],cur["defense"],cur["physical"],owned,iso(last),iso(first),event_count,now))
 c.commit()
 analysed=c.execute("SELECT COUNT(*) FROM ownership_v65 WHERE wallet=?",(wallet,)).fetchone()[0]
 c.close()
 return len(ids),len(results),errors,analysed,len(todo)


def finish_import(wallet, progress=None, chunk_size=12, pause_seconds=8, max_chunks=40):
    """Process uncached players in safe chunks during one Streamlit run."""
    summary={"total":0,"analysed":0,"added":0,"errors":[],"complete":False,"rate_limited":False}
    for chunk in range(max_chunks):
        total,added,errors,analysed,planned=sync(wallet,None,batch_size=chunk_size)
        summary.update(total=total,analysed=analysed,added=summary["added"]+added)
        summary["errors"].extend(errors)
        if progress: progress(analysed,total,chunk+1)
        if planned==0 or analysed>=total:
            summary["complete"]=True
            break
        limited=any(("429" in msg or "rate-limit" in msg.lower() or "rate limit" in msg.lower()) for _,msg in errors)
        if limited:
            summary["rate_limited"]=True
            break
        time.sleep(pause_seconds)
    return summary

def ensure_v2(c):
 c.executescript("""CREATE TABLE IF NOT EXISTS player_meta(
 wallet TEXT,player_id INTEGER,age REAL,position TEXT,club TEXT,PRIMARY KEY(wallet,player_id));
 CREATE TABLE IF NOT EXISTS snapshots(
 wallet TEXT,player_id INTEGER,snapshot_at TEXT,ovr REAL,pac REAL,sho REAL,pas REAL,dri REAL,defn REAL,phy REAL,
 PRIMARY KEY(wallet,player_id,snapshot_at));
 CREATE TABLE IF NOT EXISTS activity(
 wallet TEXT,player_id INTEGER,last_event_at TEXT,match_events INTEGER DEFAULT 0,training_events INTEGER DEFAULT 0,total_events INTEGER DEFAULT 0,
 PRIMARY KEY(wallet,player_id));""");c.commit()

def event_reason(e):
 return str(e.get("reasonType") or e.get("type") or e.get("eventType") or "").upper()

def refresh_current(wallet, progress=None, batch_size=20):
 """Refresh current profile + activity using profile/history only; no sale-history call."""
 wallet=wallet.strip().lower();t=token();c=db();init(c);ensure_v2(c)
 ids=[r["player_id"] for r in c.execute("SELECT player_id FROM ownership_v65 WHERE wallet=? ORDER BY player_id",(wallet,))]
 done=0;errors=[]
 for x in ids[:batch_size]:
  try:
   cur=profile(x,t); exps=exp_history(x,t); parsed=[]
   for e in exps:
    when=todt(e.get("date") or e.get("createdAt") or e.get("timestamp"))
    if when:parsed.append((when,e))
   parsed.sort(key=lambda z:z[0])
   match_count=sum(1 for _,e in parsed if event_reason(e)=="MATCH")
   train_count=sum(1 for _,e in parsed if "TRAIN" in event_reason(e))
   last=parsed[-1][0] if parsed else None
   c.execute("""INSERT INTO player_meta VALUES(?,?,?,?,?) ON CONFLICT(wallet,player_id) DO UPDATE SET
    age=excluded.age,position=excluded.position,club=excluded.club""",(wallet,x,cur.get("age"),cur.get("position"),cur.get("club")))
   c.execute("""INSERT INTO activity VALUES(?,?,?,?,?,?) ON CONFLICT(wallet,player_id) DO UPDATE SET
    last_event_at=excluded.last_event_at,match_events=excluded.match_events,training_events=excluded.training_events,total_events=excluded.total_events""",
    (wallet,x,iso(last),match_count,train_count,len(parsed)))
   now=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
   c.execute("""INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)""",
    (wallet,x,now,cur.get("overall"),cur.get("pace"),cur.get("shooting"),cur.get("passing"),cur.get("dribbling"),cur.get("defense"),cur.get("physical")))
   # current values in the main cache should move with refreshes; historical baseline remains unchanged.
   c.execute("""UPDATE ownership_v65 SET current_ovr=?,current_pac=?,current_sho=?,current_pas=?,current_dri=?,current_def=?,current_phy=?,updated_at=? WHERE wallet=? AND player_id=?""",
    (cur.get("overall"),cur.get("pace"),cur.get("shooting"),cur.get("passing"),cur.get("dribbling"),cur.get("defense"),cur.get("physical"),now,wallet,x))
   c.commit();done+=1
  except Exception as e:
   errors.append((x,str(e)))
   if "429" in str(e) or "rate-limit" in str(e).lower():break
  if progress:progress(done,len(ids))
  time.sleep(.45)
 c.close();return done,len(ids),errors

def roster_payload(wallet,t):
 return arr(get("/players",t,{"ownerWalletAddress":wallet,"limit":1200}))

def roster_meta_map(wallet,t):
 out={}
 for raw in roster_payload(wallet,t):
  p=unwrap(raw)
  pid=p.get("id") or p.get("playerId") or p.get("playerID")
  try: pid=int(pid)
  except: continue
  out[pid]=player_meta_from_payload(p)
 return out

def refresh_current_v21(wallet, progress=None, batch_size=20):
 wallet=wallet.strip().lower();t=token();c=db();init(c);ensure_v2(c)
 # One roster call gives us the metadata shape that previously supplied age successfully.
 rmeta=roster_meta_map(wallet,t)
 ids=[r["player_id"] for r in c.execute("SELECT player_id FROM ownership_v65 WHERE wallet=? ORDER BY player_id",(wallet,))]
 # Refresh least-recently snapshotted first, so each press advances through the agency.
 ordered=[]
 for pid in ids:
  row=c.execute("SELECT MAX(snapshot_at) x FROM snapshots WHERE wallet=? AND player_id=?",(wallet,pid)).fetchone()
  ordered.append((row["x"] if row and row["x"] else "",pid))
 ordered=[pid for _,pid in sorted(ordered,key=lambda z:z[0])]
 done=0;errors=[]
 for x in ordered[:batch_size]:
  try:
   raw=get(f"/players/{x}",t);cur=profile(x,t) if False else None
   # Parse this already-fetched profile without making a duplicate API request.
   p=unwrap(raw);m=p.get("metadata") or {}
   name=p.get("name") or m.get("name")
   pm=player_meta_from_payload(p); rm=rmeta.get(x,{})
   age=pm.get("age") if pm.get("age") is not None else rm.get("age")
   position=pm.get("position") or rm.get("position")
   club=pm.get("club") or rm.get("club")
   current={"overall":stat(p,"overall","overallRating","ovr"),"pace":stat(p,"pace","PAC"),
    "shooting":stat(p,"shooting","SHO"),"passing":stat(p,"passing","PAS"),"dribbling":stat(p,"dribbling","DRI"),
    "defense":stat(p,"defense","defending","DEF"),"physical":stat(p,"physical","physicality","PHY")}
   exps=exp_history(x,t);parsed=[]
   for e in exps:
    when=todt(e.get("date") or e.get("createdAt") or e.get("timestamp"))
    if when:parsed.append((when,e))
   parsed.sort(key=lambda z:z[0])
   match_count=sum(1 for _,e in parsed if event_reason(e)=="MATCH")
   train_count=sum(1 for _,e in parsed if "TRAIN" in event_reason(e))
   last=parsed[-1][0] if parsed else None
   c.execute("""INSERT INTO player_meta VALUES(?,?,?,?,?) ON CONFLICT(wallet,player_id) DO UPDATE SET
    age=excluded.age,position=excluded.position,club=excluded.club""",(wallet,x,age,position,club))
   c.execute("""INSERT INTO activity VALUES(?,?,?,?,?,?) ON CONFLICT(wallet,player_id) DO UPDATE SET
    last_event_at=excluded.last_event_at,match_events=excluded.match_events,training_events=excluded.training_events,total_events=excluded.total_events""",
    (wallet,x,iso(last),match_count,train_count,len(parsed)))
   now=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
   c.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
    (wallet,x,now,current["overall"],current["pace"],current["shooting"],current["passing"],current["dribbling"],current["defense"],current["physical"]))
   c.execute("""UPDATE ownership_v65 SET current_ovr=?,current_pac=?,current_sho=?,current_pas=?,current_dri=?,current_def=?,current_phy=?,updated_at=? WHERE wallet=? AND player_id=?""",
    (current["overall"],current["pace"],current["shooting"],current["passing"],current["dribbling"],current["defense"],current["physical"],now,wallet,x))
   c.commit();done+=1
  except Exception as e:
   errors.append((x,str(e)))
   if "429" in str(e) or "rate-limit" in str(e).lower():break
  if progress:progress(done,min(batch_size,len(ordered)))
  time.sleep(.45)
 c.close();return done,len(ids),errors

def refresh_whole_agency(wallet, progress=None, chunk_size=20, pause_seconds=6, max_chunks=30):
 """Refresh metadata/current stats/activity for the whole cached agency in safe rotating chunks."""
 wallet=wallet.strip().lower()
 c=db();init(c);ensure_v2(c)
 total=c.execute("SELECT COUNT(*) n FROM ownership_v65 WHERE wallet=?",(wallet,)).fetchone()["n"]
 c.close()
 completed=0;errors=[];rate_limited=False
 # refresh_current_v21 chooses least-recently snapshotted players, so successive
 # calls naturally move through the agency rather than repeating the same group.
 for chunk in range(max_chunks):
  done,agency_total,errs=refresh_current_v21(wallet,None,chunk_size)
  completed += done
  errors.extend(errs)
  if progress: progress(min(completed,total),total,chunk+1)
  limited=any(("429" in msg or "rate-limit" in msg.lower() or "rate limit" in msg.lower()) for _,msg in errs)
  if limited:
   rate_limited=True;break
  if done==0 or completed>=total:break
  time.sleep(pause_seconds)
 return {"refreshed":min(completed,total),"total":total,"errors":errors,"rate_limited":rate_limited,"complete":completed>=total}

def fill_metadata_from_roster(wallet):
 """Populate age/position/club for every cached player from one roster request."""
 wallet=wallet.strip().lower();t=token();c=db();init(c);ensure_v2(c)
 raw=roster_payload(wallet,t);updated=0
 for item in raw:
  p=unwrap(item)
  pid=p.get("id") or p.get("playerId") or p.get("playerID")
  try:pid=int(pid)
  except:continue
  meta=player_meta_from_payload(p)
  c.execute("""INSERT INTO player_meta(wallet,player_id,age,position,club) VALUES(?,?,?,?,?)
   ON CONFLICT(wallet,player_id) DO UPDATE SET
   age=COALESCE(excluded.age,player_meta.age),
   position=COALESCE(excluded.position,player_meta.position),
   club=COALESCE(excluded.club,player_meta.club)""",
   (wallet,pid,meta.get("age"),meta.get("position"),meta.get("club")))
  updated+=1
 c.commit();c.close();return updated

def metadata_counts(wallet):
 c=db();init(c);ensure_v2(c)
 r=c.execute("""SELECT COUNT(*) total,
 SUM(CASE WHEN age IS NOT NULL THEN 1 ELSE 0 END) ages,
 SUM(CASE WHEN position IS NOT NULL AND position<>'' THEN 1 ELSE 0 END) positions,
 SUM(CASE WHEN club IS NOT NULL AND club<>'' THEN 1 ELSE 0 END) clubs
 FROM player_meta WHERE wallet=?""",(wallet.strip().lower(),)).fetchone()
 c.close()
 return dict(r) if r else {"total":0,"ages":0,"positions":0,"clubs":0}

def get_fast(path,t,params=None,timeout=12):
 r=requests.get(BASE+path,headers=ah(t),params=params,timeout=timeout)
 if r.status_code==429:
  retry=r.headers.get("Retry-After")
  raise RuntimeError(f"MFL_RATE_LIMITED|{retry or ''}")
 if not r.ok:
  raise RuntimeError(f"MFL {r.status_code}: {r.text[:180]}")
 return r.json()

def fill_metadata_fast(wallet):
 """One roster call, no exponential waiting. Returns immediately on 429."""
 wallet=wallet.strip().lower();t=token()
 raw=arr(get_fast("/players",t,{"ownerWalletAddress":wallet,"limit":1200},timeout=12))
 c=db();init(c);ensure_v2(c);updated=0
 for item in raw:
  p=unwrap(item)
  pid=p.get("id") or p.get("playerId") or p.get("playerID")
  try:pid=int(pid)
  except:continue
  meta=player_meta_from_payload(p)
  c.execute("""INSERT INTO player_meta(wallet,player_id,age,position,club) VALUES(?,?,?,?,?)
   ON CONFLICT(wallet,player_id) DO UPDATE SET
   age=COALESCE(excluded.age,player_meta.age),
   position=COALESCE(excluded.position,player_meta.position),
   club=COALESCE(excluded.club,player_meta.club)""",
   (wallet,pid,meta.get("age"),meta.get("position"),meta.get("club")))
  updated+=1
 c.commit()
 counts=c.execute("""SELECT
  SUM(CASE WHEN age IS NOT NULL THEN 1 ELSE 0 END) ages,
  SUM(CASE WHEN position IS NOT NULL AND position<>'' THEN 1 ELSE 0 END) positions,
  SUM(CASE WHEN club IS NOT NULL AND club<>'' THEN 1 ELSE 0 END) clubs
  FROM player_meta WHERE wallet=?""",(wallet,)).fetchone()
 c.close()
 return updated,dict(counts)

def inspect_activity_events(player_id):
 """Fetch one player's progression history and summarize reasonType values."""
 t=token(); events=exp_history(int(player_id),t)
 reasons={}
 for e in events:
  r=event_reason(e) or "UNKNOWN"
  reasons[r]=reasons.get(r,0)+1
 matches=[e for e in events if event_reason(e)=="MATCH"]
 return {"event_count":len(events),"reasons":reasons,"match_count":len(matches),
         "sample_match":matches[-1] if matches else None,
         "sample_events":events[-5:] if events else []}

def ensure_activity_v28(c):
 c.execute("""CREATE TABLE IF NOT EXISTS ownership_activity_v28(
  wallet TEXT NOT NULL, player_id INTEGER NOT NULL,
  match_events_owned INTEGER NOT NULL DEFAULT 0,
  last_match_at TEXT, first_match_at TEXT,
  activity_scanned_at TEXT,
  PRIMARY KEY(wallet,player_id))""")
 c.commit()

def activity_event_date(e):
 """Return a timezone-aware datetime for an MFL progression event."""
 return todt(e.get("date") or e.get("createdAt") or e.get("timestamp"))

def _iso_event_date(e):
 dt=activity_event_date(e)
 return dt.isoformat() if dt else None

def refresh_owned_activity_one(wallet,player_id,t=None):
 """Count MATCH progression events from the current ownership baseline onward."""
 wallet=wallet.strip().lower(); player_id=int(player_id); t=t or token()
 c=db();init(c);ensure_v2(c);ensure_activity_v28(c)
 row=c.execute("""SELECT source,acquired_at,history_start
                  FROM ownership_v65 WHERE wallet=? AND player_id=?""",
               (wallet,player_id)).fetchone()
 if not row:
  c.close(); raise RuntimeError("Player ownership baseline not found")
 anchor=row["acquired_at"] if row["source"]=="BOUGHT" and row["acquired_at"] else row["history_start"]
 anchor_dt=todt(anchor) if anchor else None
 events=exp_history(player_id,t)
 owned=[]
 for e in events:
  if event_reason(e)!="MATCH": continue
  dt=activity_event_date(e)
  if dt and (anchor_dt is None or dt>=anchor_dt): owned.append(dt)
 owned.sort()
 first=owned[0].isoformat() if owned else None
 last=owned[-1].isoformat() if owned else None
 now=datetime.now(timezone.utc).isoformat()
 c.execute("""INSERT INTO ownership_activity_v28(wallet,player_id,match_events_owned,first_match_at,last_match_at,activity_scanned_at)
 VALUES(?,?,?,?,?,?)
 ON CONFLICT(wallet,player_id) DO UPDATE SET
 match_events_owned=excluded.match_events_owned,
 first_match_at=excluded.first_match_at,
 last_match_at=excluded.last_match_at,
 activity_scanned_at=excluded.activity_scanned_at""",
 (wallet,player_id,len(owned),first,last,now))
 c.commit();c.close()
 return len(owned),last

def refresh_owned_activity_batch(wallet,limit=10):
 """Refresh least-recently scanned players. Stops cleanly if MFL rate-limits."""
 wallet=wallet.strip().lower();t=token()
 c=db();init(c);ensure_v2(c);ensure_activity_v28(c)
 rows=c.execute("""SELECT o.player_id,o.player_name,a.activity_scanned_at
 FROM ownership_v65 o LEFT JOIN ownership_activity_v28 a
 ON a.wallet=o.wallet AND a.player_id=o.player_id
 WHERE o.wallet=?
 ORDER BY CASE WHEN a.activity_scanned_at IS NULL THEN 0 ELSE 1 END,
          a.activity_scanned_at ASC, o.player_name ASC LIMIT ?""",(wallet,int(limit))).fetchall()
 c.close()
 done=[]; stopped=None
 for r in rows:
  try:
   n,last=refresh_owned_activity_one(wallet,r["player_id"],t)
   done.append({"player_id":r["player_id"],"player_name":r["player_name"],"match_events_owned":n,"last_match_at":last})
   time.sleep(1.0)
  except Exception as e:
   if "429" in str(e) or "RATE_LIMIT" in str(e):
    stopped=str(e);break
   done.append({"player_id":r["player_id"],"player_name":r["player_name"],"error":str(e)})
 return done,stopped

def activity_counts(wallet):
 wallet=wallet.strip().lower();c=db();init(c);ensure_activity_v28(c)
 r=c.execute("""SELECT COUNT(*) scanned,
 SUM(CASE WHEN match_events_owned>0 THEN 1 ELSE 0 END) active
 FROM ownership_activity_v28 WHERE wallet=?""",(wallet,)).fetchone()
 c.close();return dict(r)

def agency_v28(wallet):
 """Dashboard rows with verified ownership-baseline match activity."""
 wallet=wallet.strip().lower();c=db();init(c);ensure_v2(c);ensure_activity_v28(c)
 rows=c.execute("""SELECT o.*,m.age,m.position,m.club,
 a.match_events_owned,a.first_match_at,a.last_match_at,a.activity_scanned_at
 FROM ownership_v65 o
 LEFT JOIN player_meta m ON m.wallet=o.wallet AND m.player_id=o.player_id
 LEFT JOIN ownership_activity_v28 a ON a.wallet=o.wallet AND a.player_id=o.player_id
 WHERE o.wallet=?""",(wallet,)).fetchall()
 c.close()
 out=[]
 now=datetime.now(timezone.utc)
 for r in rows:
  d=dict(r)
  last=todt(d.get("last_match_at")) if d.get("last_match_at") else None
  d["days_since_match"]=((now-last).days if last else None)
  d["ovr_gain"]=(d["current_ovr"]-d["start_ovr"]) if d.get("current_ovr") is not None and d.get("start_ovr") is not None else None
  out.append(d)
 return out

def refresh_unscanned_activity_batch(wallet,limit=10):
 """Scan only players with no v2.8 activity row yet; never cycles back automatically."""
 wallet=wallet.strip().lower(); t=token()
 c=db();init(c);ensure_v2(c);ensure_activity_v28(c)
 rows=c.execute("""SELECT o.player_id,o.player_name
 FROM ownership_v65 o
 LEFT JOIN ownership_activity_v28 a ON a.wallet=o.wallet AND a.player_id=o.player_id
 WHERE o.wallet=? AND a.player_id IS NULL
 ORDER BY o.player_name ASC LIMIT ?""",(wallet,int(limit))).fetchall()
 c.close()
 done=[];stopped=None
 for r in rows:
  try:
   n,last=refresh_owned_activity_one(wallet,r["player_id"],t)
   done.append({"player_id":r["player_id"],"player_name":r["player_name"],
                "match_events_owned":n,"last_match_at":last})
   time.sleep(1.0)
  except Exception as e:
   if "429" in str(e) or "RATE_LIMIT" in str(e):
    stopped=str(e);break
   done.append({"player_id":r["player_id"],"player_name":r["player_name"],"error":str(e)})
 return done,stopped

def reset_activity_scan(wallet):
 wallet=wallet.strip().lower();c=db();init(c);ensure_activity_v28(c)
 c.execute("DELETE FROM ownership_activity_v28 WHERE wallet=?",(wallet,))
 c.commit();c.close()

def unscanned_activity_count(wallet):
 wallet=wallet.strip().lower();c=db();init(c);ensure_activity_v28(c)
 n=c.execute("""SELECT COUNT(*) n FROM ownership_v65 o
 LEFT JOIN ownership_activity_v28 a ON a.wallet=o.wallet AND a.player_id=o.player_id
 WHERE o.wallet=? AND a.player_id IS NULL""",(wallet,)).fetchone()["n"]
 c.close();return int(n)

def finish_activity_scan(wallet, progress_callback=None, max_players=None):
 """Process every currently-unscanned player sequentially.
 On 429, obey Retry-After then continue automatically.
 Each player is committed by refresh_owned_activity_one before moving on.
 """
 wallet=wallet.strip().lower(); t=token()
 c=db();init(c);ensure_v2(c);ensure_activity_v28(c)
 rows=c.execute("""SELECT o.player_id,o.player_name
 FROM ownership_v65 o
 LEFT JOIN ownership_activity_v28 a ON a.wallet=o.wallet AND a.player_id=o.player_id
 WHERE o.wallet=? AND a.player_id IS NULL
 ORDER BY o.player_name ASC""",(wallet,)).fetchall()
 c.close()
 if max_players: rows=rows[:int(max_players)]
 total=len(rows); done=0; errors=[]
 for r in rows:
  attempts=0
  while True:
   try:
    n,last=refresh_owned_activity_one(wallet,r["player_id"],t)
    done+=1
    if progress_callback: progress_callback(done,total,r["player_name"],None)
    time.sleep(1.0)
    break
   except Exception as e:
    msg=str(e)
    if ("429" in msg or "RATE_LIMIT" in msg) and attempts<8:
     attempts+=1
     # Existing GET helper may already have exhausted retries; use a conservative
     # cooldown and surface it to the UI.
     wait=195
     if progress_callback: progress_callback(done,total,r["player_name"],wait)
     time.sleep(wait)
     # Refresh auth after a long wait.
     try:t=token()
     except:pass
     continue
    errors.append({"player_id":r["player_id"],"player_name":r["player_name"],"error":msg})
    if progress_callback: progress_callback(done,total,r["player_name"],-1)
    break
 return {"completed":done,"attempted":total,"errors":errors}

def probe_match_endpoints(player_id):
 """Diagnostic only: test plausible first-party MFL routes for actual match/appearance data."""
 pid=int(player_id); t=token()
 candidates=[
  (f"/players/{pid}/matches",None),
  (f"/players/{pid}/match-history",None),
  (f"/players/{pid}/games",None),
  (f"/players/{pid}/appearances",None),
  ("/matches",{"playerId":pid,"limit":10}),
  ("/matches/feed",{"playerId":pid,"limit":10}),
  ("/matches/history",{"playerId":pid,"limit":10}),
 ]
 out=[]
 for path,params in candidates:
  try:
   r=requests.get(BASE+path,headers=ah(t),params=params,timeout=12)
   item={"path":path,"params":params,"status":r.status_code}
   try:item["json"]=r.json()
   except Exception:item["text"]=r.text[:800]
   out.append(item)
   if r.status_code==429:break
  except Exception as e:out.append({"path":path,"params":params,"error":str(e)})
 return out

APP_BACKEND_VERSION = "2.23"

def probe_match_feed_filters(player_id, club_id=None, squad_id=None):
 """Targeted diagnostic based on the confirmed /matches/feed route.
 Tests query parameter shapes instead of inventing new route families.
 """
 pid=int(player_id); t=token()
 tests=[
  {"playerId":pid,"limit":25},
  {"playerIds":str(pid),"limit":25},
  {"player":pid,"limit":25},
  {"players":str(pid),"limit":25},
  {"participantId":pid,"limit":25},
  {"status":"ENDED","playerId":pid,"limit":25},
  {"status":"FINISHED","playerId":pid,"limit":25},
 ]
 if club_id:
  cid=int(club_id)
  tests += [
   {"clubId":cid,"limit":25},
   {"clubIds":str(cid),"limit":25},
   {"status":"ENDED","clubId":cid,"limit":25},
  ]
 if squad_id:
  sid=int(squad_id)
  tests += [
   {"squadId":sid,"limit":25},
   {"squadIds":str(sid),"limit":25},
   {"status":"ENDED","squadId":sid,"limit":25},
  ]
 out=[]
 for params in tests:
  try:
   r=requests.get(BASE+"/matches/feed",headers=ah(t),params=params,timeout=12)
   item={"params":params,"status":r.status_code}
   try:
    js=r.json()
    item["count"]=len(js) if isinstance(js,list) else None
    # Compact signatures are enough to tell whether a filter changed the feed.
    if isinstance(js,list):
     item["sample"]=[{
       "id":x.get("id"),"status":x.get("status"),"type":x.get("type"),
       "home":x.get("homeTeamName"),"away":x.get("awayTeamName"),
       "homeSquadId":((x.get("homeSquad") or {}).get("id") if isinstance(x.get("homeSquad"),dict) else None),
       "awaySquadId":((x.get("awaySquad") or {}).get("id") if isinstance(x.get("awaySquad"),dict) else None),
       "startDate":x.get("startDate")
      } for x in js[:3]]
    else:item["json"]=js
   except Exception:item["text"]=r.text[:800]
   out.append(item)
   if r.status_code==429:break
  except Exception as e:out.append({"params":params,"error":str(e)})
 return out

def probe_club_history(club_id, squad_id=None):
 """Diagnostic for the club-history path used by MFL's public club pages."""
 cid=int(club_id); sid=int(squad_id) if squad_id else None; t=token()
 candidates=[
  (f"/clubs/{cid}/matches",None),
  (f"/clubs/{cid}/matches/history",None),
  (f"/clubs/{cid}/history",None),
  (f"/clubs/{cid}/schedule",None),
  ("/matches",{"clubId":cid,"limit":25}),
  ("/matches/feed",{"clubId":cid,"limit":25}),
  ("/matches/feed",{"club":cid,"limit":25}),
 ]
 if sid:
  candidates += [
   (f"/squads/{sid}/matches",None),
   (f"/squads/{sid}/history",None),
   ("/matches",{"squadId":sid,"limit":25}),
   ("/matches/feed",{"squadId":sid,"limit":25}),
   ("/matches/feed",{"squad":sid,"limit":25}),
  ]
 out=[]
 for path,params in candidates:
  try:
   r=requests.get(BASE+path,headers=ah(t),params=params,timeout=12)
   item={"path":path,"params":params,"status":r.status_code}
   try:
    js=r.json()
    item["count"]=len(js) if isinstance(js,list) else None
    if isinstance(js,list):
     item["sample"]=[{
      "id":x.get("id"),"status":x.get("status"),"type":x.get("type"),
      "home":x.get("homeTeamName"),"away":x.get("awayTeamName"),
      "homeSquadId":((x.get("homeSquad") or {}).get("id") if isinstance(x.get("homeSquad"),dict) else None),
      "awaySquadId":((x.get("awaySquad") or {}).get("id") if isinstance(x.get("awaySquad"),dict) else None),
      "startDate":x.get("startDate")
     } for x in js[:5]]
    else:item["json"]=js
   except Exception:item["text"]=r.text[:1000]
   out.append(item)
   if r.status_code==429:break
  except Exception as e:out.append({"path":path,"params":params,"error":str(e)})
 return out


def auth_health():
 try:
  token()
  return {"ok":True,"message":"MFL API connected"}
 except Exception as e:
  return {"ok":False,"message":str(e)}


def auth_diagnostic():
    """Read-only auth diagnostic; never returns token values."""
    rt=os.getenv("MFL_REFRESH_TOKEN")
    result={
        "refresh_token_present": bool(rt),
        "refresh_token_length": len(rt) if rt else 0,
        "refresh_token_shape": "JWT-like" if rt and rt.count(".")==2 else ("opaque" if rt else "missing"),
        "tests":[]
    }
    if not rt:
        return result
    tests=[
        ("current_browser_headers", H, {"refreshToken":rt}),
        ("minimal_json_headers", {
            "Accept":"application/json",
            "Content-Type":"application/json",
            "User-Agent":H.get("User-Agent","Mozilla/5.0")
        }, {"refreshToken":rt}),
    ]
    for name,headers,payload in tests:
        item={"name":name}
        try:
            r=requests.post(BASE+"/auth/refresh",headers=headers,json=payload,timeout=20)
            item["status"]=r.status_code
            item["content_type"]=r.headers.get("content-type")
            item["retry_after"]=r.headers.get("retry-after")
            try:
                js=r.json()
                if isinstance(js,dict):
                    item["body"]={k:("[redacted]" if any(x in str(k).lower() for x in ("token","access","refresh")) else v) for k,v in js.items()}
                else:
                    item["body"]=js
            except Exception:
                item["body_text"]=r.text[:500]
        except Exception as e:
            item["error"]=f"{type(e).__name__}: {e}"
        result["tests"].append(item)
    return result


def refresh_token_metadata():
    """Decode safe JWT timing claims locally without exposing the token."""
    import base64, json
    from datetime import datetime, timezone
    rt=os.getenv("MFL_REFRESH_TOKEN")
    if not rt:
        return {"present":False}
    out={"present":True,"shape":"JWT-like" if rt.count(".")==2 else "opaque","length":len(rt)}
    if rt.count(".") != 2:
        return out
    try:
        payload=rt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims=json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        safe={}
        for key in ("iat","exp","nbf"):
            val=claims.get(key)
            if isinstance(val,(int,float)):
                safe[key]=val
                safe[key+"_utc"]=datetime.fromtimestamp(val,tz=timezone.utc).isoformat()
        exp=claims.get("exp")
        if isinstance(exp,(int,float)):
            now=datetime.now(timezone.utc).timestamp()
            safe["expired"]=now >= exp
            safe["seconds_until_expiry"]=int(exp-now)
        out["claims"]=safe
    except Exception as e:
        out["decode_error"]=f"{type(e).__name__}: {e}"
    return out


def refresh_metadata_v23(wallet):
    """Backfill age/position/club for a wallet. Uses one roster request first,
    then player profiles only for fields still missing."""
    wallet=wallet.strip().lower()
    t=token(); c=db(); init(c); ensure_v2(c)
    roster=roster_payload(wallet,t)
    unresolved=[]
    updated=0
    for item in roster:
        p=unwrap(item)
        pid=p.get("id") or p.get("playerId") or p.get("playerID")
        try: pid=int(pid)
        except: continue
        meta=player_meta_from_payload(p)
        age,position,club=meta.get("age"),meta.get("position"),meta.get("club")
        c.execute("""INSERT INTO player_meta(wallet,player_id,age,position,club) VALUES(?,?,?,?,?)
          ON CONFLICT(wallet,player_id) DO UPDATE SET
          age=COALESCE(excluded.age,player_meta.age),
          position=COALESCE(excluded.position,player_meta.position),
          club=COALESCE(excluded.club,player_meta.club)""",
          (wallet,pid,age,position,club))
        if age is None or not position or not club:
            unresolved.append(pid)
        updated+=1
    c.commit()
    # Profile endpoint has the full metadata shape. Keep this conservative to avoid
    # hammering MFL; missing fields can be completed on later sessions.
    for pid in unresolved[:40]:
        try:
            p=unwrap(get(f"{BASE}/players/{pid}",t))
            meta=player_meta_from_payload(p)
            c.execute("""INSERT INTO player_meta(wallet,player_id,age,position,club) VALUES(?,?,?,?,?)
              ON CONFLICT(wallet,player_id) DO UPDATE SET
              age=COALESCE(excluded.age,player_meta.age),
              position=COALESCE(excluded.position,player_meta.position),
              club=COALESCE(excluded.club,player_meta.club)""",
              (wallet,pid,meta.get("age"),meta.get("position"),meta.get("club")))
        except Exception:
            continue
    c.commit(); c.close()
    return {"roster_rows":updated,"profile_backfill_attempted":min(len(unresolved),40)}
