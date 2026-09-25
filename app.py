
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import streamlit as st
import agency_backend as ab

st.set_page_config(page_title="MFL Club Development", page_icon="📈", layout="wide")
try:
    if "MFL_REFRESH_TOKEN" in st.secrets:
        os.environ["MFL_REFRESH_TOKEN"] = st.secrets["MFL_REFRESH_TOKEN"]
except Exception:
    pass

# S17 baseline is configurable so the app does not hard-code an unverified season boundary.
DEFAULT_S17_START = os.getenv("S17_START", "2026-08-01T00:00:00+00:00")

def valid_wallet(v):
    v=(v or "").strip()
    return len(v)>=10 and v.lower().startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in v[2:])

def to_dt(v):
    if v is None: return None
    try:
        x=float(v)
        if x>1e10: x/=1000
        return datetime.fromtimestamp(x, tz=timezone.utc)
    except Exception:
        try:
            d=pd.to_datetime(v, utc=True)
            return d.to_pydatetime()
        except Exception:
            return None

ATTRS = ["pace","shooting","passing","dribbling","defense","physical"]
SHORT = {"pace":"PAC","shooting":"SHO","passing":"PAS","dribbling":"DRI","defense":"DEF","physical":"PHY"}

def unwrap_event(e):
    return e if isinstance(e,dict) else {}

def event_reason(e):
    return str(e.get("reasonType") or e.get("type") or e.get("eventType") or "").upper()

def event_values(e):
    v=e.get("values")
    return v if isinstance(v,dict) else {}

def event_date(e):
    return to_dt(e.get("date") or e.get("createdDateTime") or e.get("timestamp"))

def progression_events(pid, token):
    raw=ab.get(f"/players/{pid}/experiences/history", token)
    if isinstance(raw,dict):
        for k in ("data","items","results","history","experiences"):
            if isinstance(raw.get(k),list):
                raw=raw[k]; break
    return raw if isinstance(raw,list) else []

def roster_rows(wallet, token):
    raw=ab.roster_payload(wallet, token)
    rows=[]
    for item in raw:
        p=ab.unwrap(item)
        pid=p.get("id") or p.get("playerId") or p.get("playerID")
        try: pid=int(pid)
        except: continue
        meta=ab.player_meta_from_payload(p)
        m=p.get("metadata") if isinstance(p.get("metadata"),dict) else {}
        name=p.get("name") or m.get("name")
        if not name:
            name=(str(p.get("firstName") or m.get("firstName") or "")+" "+str(p.get("lastName") or m.get("lastName") or "")).strip()
        rows.append({"player_id":pid,"player":name or f"Player {pid}","club":meta.get("club") or "Unassigned"})
    return rows

def state_at_or_before(events, cutoff):
    parsed=[]
    for e in events:
        d=event_date(e)
        v=event_values(e)
        if d and v:
            parsed.append((d,v))
    parsed.sort(key=lambda x:x[0])
    prior=[x for x in parsed if x[0] <= cutoff]
    if prior:
        return dict(prior[-1][1]), prior[-1][0]
    # If the player's INITIAL event occurs after S17 starts, use INITIAL as their personal S17 baseline.
    for d,v in parsed:
        if d > cutoff:
            return dict(v), d
    return {}, None

def latest_state(events):
    parsed=[(event_date(e),event_values(e)) for e in events if event_date(e) and event_values(e)]
    parsed.sort(key=lambda x:x[0])
    return (dict(parsed[-1][1]),parsed[-1][0]) if parsed else ({},None)

def n(v):
    try:return float(v)
    except:return None

def delta(cur, start, key):
    a=n(cur.get(key)); b=n(start.get(key))
    return (a-b) if a is not None and b is not None else 0

@st.cache_data(ttl=1800, show_spinner=False)
def build_live(wallet, season_start_iso):
    token=ab.token()
    season_start=pd.to_datetime(season_start_iso,utc=True).to_pydatetime()
    roster=roster_rows(wallet,token)

    def load_one(r):
        ev=progression_events(r["player_id"],token)
        start,start_dt=state_at_or_before(ev,season_start)
        cur,last_dt=latest_state(ev)
        if not cur:
            return None
        gains={k:max(0,delta(cur,start,k)) for k in ATTRS}
        ovr=max(0,delta(cur,start,"overall"))
        return {
            **r,
            "baseline_date":start_dt,
            "last_progression":last_dt,
            "start_ovr":n(start.get("overall")),
            "current_ovr":n(cur.get("overall")),
            "ovr_gain":ovr,
            "attr_gain":sum(gains.values()),
            **{SHORT[k]:gains[k] for k in ATTRS}
        }

    rows=[]
    errors=[]
    # Bounded pool: much faster than hundreds of sequential requests, without
    # opening an excessive number of connections to MFL.
    workers=min(16, max(4, len(roster)//20 if roster else 4))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(load_one,r):r for r in roster}
        for future in as_completed(futures):
            r=futures[future]
            try:
                row=future.result()
                if row:
                    rows.append(row)
            except Exception as e:
                errors.append(f"{r.get('player')} ({r.get('player_id')}): {e}")

    out=pd.DataFrame(rows)
    out.attrs["errors"]=errors
    out.attrs["roster_count"]=len(roster)
    out.attrs["loaded_count"]=len(rows)
    return out

st.title("📈 MFL Club Development")
st.caption("Live Season 17 development across your clubs")

if "wallet" not in st.session_state:
    st.session_state.wallet=""

if not st.session_state.wallet:
    st.subheader("Load your agency")
    wallet=st.text_input("Dapper wallet address",placeholder="0x…")
    if st.button("Load club development",type="primary"):
        if not valid_wallet(wallet):
            st.error("Enter a valid Dapper wallet address.")
        else:
            st.session_state.wallet=wallet.strip().lower()
            st.rerun()
    st.stop()

wallet=st.session_state.wallet
c1,c2=st.columns([4,1])
with c1:
    st.caption(f"Wallet: {wallet}")
with c2:
    if st.button("Change wallet"):
        st.session_state.wallet=""
        st.rerun()

with st.expander("Season settings"):
    season_start=st.text_input("Season 17 start (UTC)",value=DEFAULT_S17_START,
        help="This is configurable until the exact official S17 boundary is confirmed.")
else_start=DEFAULT_S17_START
season_start=locals().get("season_start",else_start)

if st.button("🔄 Refresh live data",type="primary"):
    build_live.clear()

with st.spinner("Loading player progression from MFL — first load may take a little while…"):
    df=build_live(wallet,season_start)

if df.empty:
    st.warning("No progression data could be loaded for this wallet.")
    errs=df.attrs.get("errors",[])
    roster_count=df.attrs.get("roster_count",0)
    st.caption(f"Roster players found: {roster_count}")
    if errs:
        with st.expander("Technical details"):
            st.code("\n".join(errs[:8]))
    st.stop()

clubs=(df.groupby("club",dropna=False)
    .agg(Players=("player_id","count"),OVR_gain=("ovr_gain","sum"),ATTR_gain=("attr_gain","sum"),
         PAC=("PAC","sum"),SHO=("SHO","sum"),PAS=("PAS","sum"),DRI=("DRI","sum"),DEF=("DEF","sum"),PHY=("PHY","sum"))
    .reset_index())
clubs["Avg OVR / player"]=clubs["OVR_gain"]/clubs["Players"]
clubs=clubs.sort_values(["OVR_gain","ATTR_gain","Avg OVR / player"],ascending=False).reset_index(drop=True)
clubs.index=clubs.index+1

m1,m2,m3,m4=st.columns(4)
m1.metric("Clubs",len(clubs))
m2.metric("Players",len(df))
m3.metric("Total OVR gained",f"+{clubs['OVR_gain'].sum():g}")
m4.metric("Total attributes gained",f"+{clubs['ATTR_gain'].sum():g}")

loaded=len(df)
st.caption(f"Loaded progression for {loaded} players. Results are cached for 30 minutes; use Refresh live data when you want a fresh MFL pull.")

st.subheader("🏆 Club development leaderboard")
show=clubs.rename(columns={"club":"Club","OVR_gain":"OVR ↑","ATTR_gain":"ATTR ↑"})
st.dataframe(show,use_container_width=True,hide_index=False,
    column_config={
        "OVR ↑":st.column_config.NumberColumn(format="+%.0f"),
        "ATTR ↑":st.column_config.NumberColumn(format="+%.0f"),
        "PAC":st.column_config.NumberColumn(format="+%.0f"),
        "SHO":st.column_config.NumberColumn(format="+%.0f"),
        "PAS":st.column_config.NumberColumn(format="+%.0f"),
        "DRI":st.column_config.NumberColumn(format="+%.0f"),
        "DEF":st.column_config.NumberColumn(format="+%.0f"),
        "PHY":st.column_config.NumberColumn(format="+%.0f"),
        "Avg OVR / player":st.column_config.NumberColumn(format="+%.2f"),
    })

st.subheader("🔍 Club detail")
club=st.selectbox("Choose a club",clubs["club"].tolist())
detail=df[df.club==club].copy().sort_values(["ovr_gain","attr_gain"],ascending=False)
detail["baseline_date"]=pd.to_datetime(detail["baseline_date"],utc=True,errors="coerce")
detail["last_progression"]=pd.to_datetime(detail["last_progression"],utc=True,errors="coerce")
detail=detail.rename(columns={
    "player":"Player","start_ovr":"S17 start OVR","current_ovr":"Current OVR",
    "ovr_gain":"OVR ↑","attr_gain":"ATTR ↑","baseline_date":"S17 baseline","last_progression":"Last progression"
})
st.dataframe(detail[["Player","S17 start OVR","Current OVR","OVR ↑","ATTR ↑","PAC","SHO","PAS","DRI","DEF","PHY","S17 baseline","Last progression"]],
    use_container_width=True,hide_index=True,
    column_config={
        "S17 baseline":st.column_config.DatetimeColumn(format="DD MMM YYYY"),
        "Last progression":st.column_config.DatetimeColumn(format="DD MMM YYYY"),
    })

st.caption("Club credit currently follows each player's current club. Historical intra-agency club moves will be attributed precisely once club-movement snapshots accumulate.")
