#!/usr/bin/env python3
"""vinyl — catalog records by playing them.

FivePis (vinyl-id.service) logs Shazam matches and silence events to events.jsonl.
This tool pulls that log, splits it into sides, resolves each side to an album (or a
45) via MusicBrainz, auto-confirms the obvious ones, and publishes confirmed records
to docs/catalog.json for the static GitHub Pages site.

Album-level only: we care which songs are on the shelf, not which pressing.
"""
import argparse, datetime as dt, difflib, hashlib, json, os, re, sqlite3, subprocess, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
DOCS = REPO / "docs"
DB = Path(os.environ.get("VINYL_DB", Path.home() / ".local/share/vinyl/vinyl.db"))
PULL = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-T", "fivepis-events"]
MB = "https://musicbrainz.org/ws/2"
UA = "vinyl-catalog/0.1 ( https://github.com/se7enmilgram/vinyl )"
SESSION_BREAK = 12 * 60      # no events this long = new side, even without a side_over (seed data)
FLIP_WINDOW = 10 * 60        # a 45's B-side within this of its A-side = same record
AUTO_MIN_TRACKS = 4          # distinct album tracks in one session = confirmed, if side layout is unknown
TZ = dt.timezone(dt.timedelta(hours=-6))


# ---------- text matching ----------

def norm(s, keep_paren=False):
    """Loose title/artist key: no accents, no (feat. …)/[Remastered]/(Bonus Track), no punctuation."""
    s = re.sub(r"[\u2010-\u2015\u2212]", " ", s or "")            # en/em dashes: "1967–1970"
    s = re.sub(r"['\u2018\u2019`]", "", s)                        # apostrophes: Don’t = Don't = Dont
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    if not keep_paren:
        s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s)
    s = re.sub(r"\s+-\s+.*\b(remaster\w*|version|edit|mono|stereo|live|mix)\b.*$", "", s)
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def same(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):   # "Teenage Lightning 1" vs "… 2"
        return False
    if a == b or (min(len(a), len(b)) >= 6 and (a in b or b in a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


def main_artist(s):
    return re.split(r"\s+(?:&|and|feat\.?|ft\.?|featuring|x|with)\s+|,\s*", s or "", flags=re.I)[0].strip()


def ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


# ---------- storage ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts TEXT, kind TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS mb_cache (url TEXT PRIMARY KEY, body TEXT, fetched TEXT);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE,             -- rg:<mbid> / single:<…> / unresolved:<…>
    kind TEXT,                   -- LP | 7"
    artist TEXT, title TEXT, year TEXT,
    rgid TEXT, release_id TEXT,
    status TEXT DEFAULT 'pending',   -- pending | confirmed | rejected
    confirmed_by TEXT,           -- auto | manual
    reason TEXT,                 -- why auto-confirm fired (or why not)
    tracks TEXT DEFAULT '[]',    -- [{"n": "A1", "title": …}]
    heard TEXT DEFAULT '[]',     -- titles as Shazam reported them
    first_heard TEXT, last_played TEXT,
    manual INTEGER DEFAULT 0     -- added with `vinyl add`; never rebuilt from events
);
"""


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


# ---------- MusicBrainz (1 req/s, cached forever) ----------

_last = [0.0]


class MBError(Exception):
    pass


def mb(c, path, **params):
    url = f"{MB}/{path}?" + urllib.parse.urlencode({**params, "fmt": "json"})
    row = c.execute("SELECT body FROM mb_cache WHERE url=?", (url,)).fetchone()
    if row:
        return json.loads(row["body"])
    for attempt in range(4):
        time.sleep(max(0, 1.1 - (time.time() - _last[0])))
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode()
            c.execute("INSERT OR REPLACE INTO mb_cache VALUES (?,?,?)", (url, body, dt.datetime.now(TZ).isoformat()))
            c.commit()
            return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503):
                raise MBError(f"{e.code} {url}")
        except (urllib.error.URLError, TimeoutError) as e:
            err = e
        time.sleep(2 * (attempt + 1))
    raise MBError(f"MusicBrainz unavailable: {url}")


def q(s):
    return '"' + re.sub(r'["\\]', " ", s) + '"'


def recordings(c, artist, title):
    """MB recordings matching artist+title -> [(release_group, release)]."""
    a = main_artist(artist)
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title).strip() or title
    res = mb(c, "recording", query=f"recording:{q(t)} AND artist:{q(a)}", limit=25)
    out = []
    for rec in res.get("recordings", []):
        credit = " ".join(x.get("name", "") for x in rec.get("artist-credit", []))
        if rec.get("score", 0) < 80 or not same(rec.get("title"), title) or not same(main_artist(credit), a):
            continue
        for rel in rec.get("releases", []):
            if rel.get("release-group"):
                out.append((rel["release-group"], rel))
    return out


def best_release(c, rgid, heard):
    """Pick the release in a release group that best covers what was heard (vinyl preferred)."""
    res = mb(c, "release", **{"release-group": rgid, "inc": "media recordings", "limit": 100})
    best = None
    for rel in res.get("releases", []):
        tracks = [{"n": t.get("number") or str(t.get("position")), "title": t.get("title")}
                  for m in rel.get("media", []) for t in m.get("tracks", [])]
        vinyl = any("vinyl" in (m.get("format") or "").lower() or '"' in (m.get("format") or "")
                    for m in rel.get("media", []))
        cover = sum(any(same(h, t["title"]) for t in tracks) for h in heard)
        score = (cover, vinyl, -len(tracks) if vinyl else 0, -(int((rel.get("date") or "9999")[:4] or 9999)))
        if tracks and (best is None or score > best[0]):
            best = (score, rel, tracks, vinyl)
    if not best:
        return None
    _, rel, tracks, vinyl = best
    years = [r.get("date", "")[:4] for r in res.get("releases", []) if r.get("date")]
    return {"release_id": rel["id"], "tracks": tracks, "vinyl": vinyl, "year": min(years) if years else None}


def resolve_album(c, artist, tracks, shazam_album):
    """Release group that contains the most of these songs (studio album preferred)."""
    tally = {}
    for t in tracks:
        seen = set()
        for rg, rel in recordings(c, artist, t["title"]):
            if rg["id"] in seen:
                continue
            seen.add(rg["id"])
            e = tally.setdefault(rg["id"], {"rg": rg, "hits": 0})
            e["hits"] += 1
    if not tally:
        return None

    def score(e):
        rg = e["rg"]
        studio = rg.get("primary-type") == "Album" and not rg.get("secondary-types")
        return (e["hits"], studio, difflib.SequenceMatcher(None, norm(rg.get("title")), norm(shazam_album)).ratio())

    best = max(tally.values(), key=score)
    if best["hits"] < min(2, len(tracks)):
        return None
    return best["rg"]


def resolve_single(c, artist, titles):
    """Release group of type Single containing all given songs (A and B side when both heard)."""
    groups = None
    for t in titles:
        rgs = {rg["id"]: rg for rg, _ in recordings(c, artist, t)
               if rg.get("primary-type") in ("Single", "EP")}
        groups = rgs if groups is None else {k: v for k, v in groups.items() if k in rgs}
    return next(iter(groups.values()), None) if groups else None


# ---------- events -> sides -> records ----------

def pull(c):
    out = subprocess.run(PULL, capture_output=True, text=True, timeout=60)
    if out.returncode:
        sys.exit(f"pull from FivePis failed: {out.stderr.strip()}")
    new = 0
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        cur = c.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                        (hashlib.sha1(line.encode()).hexdigest(), d["ts"], d["kind"], line))
        new += cur.rowcount
    c.commit()
    return new


def sides(c):
    """Group match events into listening sides: split on side_over or long quiet."""
    evs = sorted((json.loads(r["data"]) for r in c.execute("SELECT data FROM events")), key=lambda e: ts(e["ts"]))
    out, cur, last = [], [], None
    for e in evs:
        t = ts(e["ts"])
        if cur and last and (t - last).total_seconds() > SESSION_BREAK:
            out.append(cur); cur = []
        if e["kind"] == "side_over":
            if cur:
                out.append(cur); cur = []
        elif e["kind"] == "match" and not any(same(x["title"], e["title"]) and same(x["artist"], e["artist"]) for x in cur):
            cur.append(e)
        last = t
    if cur:
        out.append(cur)
    return out


def groups(c):
    """Each side split by artist -> [(artist, [match events])] in time order."""
    out = []
    for side in sides(c):
        by = {}
        for e in side:
            by.setdefault(norm(main_artist(e["artist"])), []).append(e)
        out += [(v[0]["artist"], v) for v in by.values()]
    return sorted(out, key=lambda g: ts(g[1][0]["ts"]))


def album_auto(tracks, heard_in_session, vinyl):
    """Reason string if one session's songs prove the album, else None."""
    hit = lambda t: any(same(h, t["title"]) for h in heard_in_session)
    if vinyl:
        by_side = {}
        for t in tracks:
            m = re.match(r"([A-Z]+)\d", t["n"] or "")
            if m:
                by_side.setdefault(m.group(1), []).append(t)
        for s, ts_ in by_side.items():
            got = sum(hit(t) for t in ts_)
            if got == len(ts_) or (len(ts_) >= 4 and got >= len(ts_) - 1):
                return f"full side {s} heard ({got}/{len(ts_)})"
    n = sum(hit(t) for t in tracks)
    if n >= AUTO_MIN_TRACKS:
        return f"{n} album tracks heard in one session"
    return None


def build(c, verbose=True):
    """Recompute candidates from all events; keep each record's status."""
    found = {}   # key -> record dict

    def merge(key, rec, heard, when, reason):
        r = found.setdefault(key, {**rec, "heard": [], "first": when, "last": when, "reason": None})
        r["heard"] += [h for h in heard if not any(same(h, x) for x in r["heard"])]
        r["first"], r["last"] = min(r["first"], when), max(r["last"], when)
        r["reason"] = r["reason"] or reason

    gs = groups(c)
    used = set()
    for i, (artist, evs) in enumerate(gs):
        if i in used:
            continue
        titles = [e["title"] for e in evs]
        when = evs[-1]["ts"]
        try:
            if len(evs) == 1:                                     # one song on a side: a 45?
                pair = next((j for j in range(i + 1, len(gs)) if j not in used and len(gs[j][1]) == 1
                             and same(main_artist(gs[j][0]), main_artist(artist))
                             and 0 < (ts(gs[j][1][0]["ts"]) - ts(evs[0]["ts"])).total_seconds() <= FLIP_WINDOW
                             and not same(gs[j][1][0]["title"], titles[0])), None)
                both = titles + ([gs[pair][1][0]["title"]] if pair is not None else [])
                rg = resolve_single(c, artist, both)
                if pair is not None and rg:
                    used.add(pair)
                    when = gs[pair][1][-1]["ts"]
                    reason = "both sides of the 45 heard"
                else:
                    rg = rg if pair is None else resolve_single(c, artist, titles)
                    reason = None
                key = f"rg:{rg['id']}" if rg else f"single:{norm(artist)}|{norm(titles[0])}"
                rec = {"kind": '7"', "artist": main_artist(artist), "rgid": rg and rg["id"],
                       "title": " / ".join(both if reason else titles), "release_id": None, "tracks": [], "year": None}
                merge(key, rec, both if reason else titles, when, reason)
                continue

            rg = resolve_album(c, artist, evs, evs[0].get("album") or "")
            if rg:
                rel = best_release(c, rg["id"], titles) or {"release_id": None, "tracks": [], "vinyl": False, "year": None}
                reason = album_auto(rel["tracks"], titles, rel["vinyl"]) if rel["tracks"] else None
                rec = {"kind": "LP", "artist": main_artist(artist), "title": rg["title"], "rgid": rg["id"],
                       "release_id": rel["release_id"], "tracks": rel["tracks"], "year": rel["year"]}
                merge(f"rg:{rg['id']}", rec, titles, when, reason)
            else:
                album = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", evs[0].get("album") or "?").strip()
                rec = {"kind": "LP", "artist": main_artist(artist), "title": album, "rgid": None,
                       "release_id": None, "tracks": [], "year": None}
                merge(f"unresolved:{norm(artist)}|{norm(album)}", rec, titles, when, None)
        except MBError as e:
            print(f"  ! skipped {artist}: {e}", file=sys.stderr)

    changed = []
    for key, r in found.items():
        old = c.execute("SELECT * FROM records WHERE key=?", (key,)).fetchone()
        if old and old["manual"]:
            continue
        heard = json.loads(old["heard"]) if old else []
        heard += [h for h in r["heard"] if not any(same(h, x) for x in heard)]
        status, by = (old["status"], old["confirmed_by"]) if old else ("pending", None)
        if status == "pending" and r["reason"]:
            status, by = "confirmed", "auto"
            changed.append(f"auto-confirmed: {r['artist']} — {r['title']} ({r['reason']})")
        elif not old:
            changed.append(f"new candidate: {r['artist']} — {r['title']}")
        c.execute("""INSERT INTO records (key, kind, artist, title, year, rgid, release_id, status, confirmed_by,
                         reason, tracks, heard, first_heard, last_played)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, artist=excluded.artist, title=excluded.title,
                         year=excluded.year, rgid=excluded.rgid, release_id=excluded.release_id,
                         status=excluded.status, confirmed_by=excluded.confirmed_by,
                         reason=COALESCE(records.reason, excluded.reason), tracks=excluded.tracks,
                         heard=excluded.heard, first_heard=MIN(records.first_heard, excluded.first_heard),
                         last_played=MAX(records.last_played, excluded.last_played)""",
                  (key, r["kind"], r["artist"], r["title"], r["year"], r["rgid"], r["release_id"], status, by,
                   r["reason"], json.dumps(r["tracks"]), json.dumps(heard), r["first"], r["last"]))
    stale = [k for (k,) in c.execute("SELECT key FROM records WHERE status='pending' AND manual=0") if k not in found]
    for k in stale:                       # e.g. a "45?" that became an LP side once more songs played
        c.execute("DELETE FROM records WHERE key=?", (k,))
    c.commit()
    if verbose:
        for line in changed:
            print("  " + line)
    return changed


# ---------- publishing ----------

def cover(rec):
    """Fetch a 250px front cover from the Cover Art Archive into docs/covers; return its site path."""
    if not rec["rgid"]:
        return None
    path = DOCS / "covers" / f"{rec['rgid']}.jpg"
    if path.exists():
        return f"covers/{path.name}"
    for kind, mbid in (("release-group", rec["rgid"]), ("release", rec["release_id"])):
        if not mbid:
            continue
        try:
            req = urllib.request.Request(f"https://coverartarchive.org/{kind}/{mbid}/front-250", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return f"covers/{path.name}"
        except (urllib.error.URLError, TimeoutError):
            continue
    return None


def export(c):
    albums = []
    for r in c.execute("SELECT * FROM records WHERE status='confirmed' ORDER BY artist COLLATE NOCASE, year, title"):
        heard = json.loads(r["heard"])
        tracks = json.loads(r["tracks"]) or [{"n": None, "title": h} for h in heard]
        albums.append({
            "id": r["key"], "kind": r["kind"], "artist": r["artist"], "title": r["title"], "year": r["year"],
            "cover": cover(r),
            "tracks": [{"n": t["n"], "title": t["title"], "heard": any(same(h, t["title"]) for h in heard)} for t in tracks],
            "last_played": (r["last_played"] or "")[:10] or None,
            "musicbrainz": f"https://musicbrainz.org/release-group/{r['rgid']}" if r["rgid"] else None,
        })
    return {"version": 1, "generated": dt.datetime.now(TZ).strftime("%Y-%m-%d"), "count": len(albums), "albums": albums}


def publish(c, dry=False):
    cat = export(c)
    DOCS.mkdir(exist_ok=True)
    path = DOCS / "catalog.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    if {**old, "generated": None} == {**cat, "generated": None}:
        print("catalog unchanged; nothing to publish")
        return False
    if dry:
        print(json.dumps(cat, indent=1)[:3000])
        return False
    path.write_text(json.dumps(cat, indent=1, ensure_ascii=False) + "\n")
    git = lambda *a: subprocess.run(["git", "-C", str(REPO), *a], check=True, capture_output=True, text=True)
    git("add", "docs")
    if not git("status", "--porcelain", "docs").stdout.strip():
        print("no changes")
        return False
    git("commit", "-q", "-m", f"Catalog: {cat['count']} records")
    git("push", "-q", "origin", "main")
    print(f"published {cat['count']} records")
    return True


# ---------- CLI ----------

def show_row(r, detail=False):
    heard = json.loads(r["heard"])
    tracks = json.loads(r["tracks"])
    have = f"{sum(any(same(h, t['title']) for h in heard) for t in tracks)}/{len(tracks)} tracks heard" if tracks else f"{len(heard)} heard"
    print(f"{r['id']:>4}  {r['status']:<9} {r['kind']:<3} {r['artist']} — {r['title']}"
          f"{' (' + r['year'] + ')' if r['year'] else ''}  [{have}; last {(r['last_played'] or '')[:10]}]")
    if r["reason"]:
        print(f"{'':>16}auto: {r['reason']}")
    if detail:
        for t in tracks or [{"n": "", "title": h} for h in heard]:
            mark = "♪" if any(same(h, t["title"]) for h in heard) else " "
            print(f"{'':>8}{mark} {t['n'] or '':>4}  {t['title']}")
        if r["rgid"]:
            print(f"{'':>8}https://musicbrainz.org/release-group/{r['rgid']}")


def cmd_sync(a, c):
    if not a.no_pull:
        print(f"pulled {pull(c)} new events from FivePis")
    build(c)
    if a.publish:
        publish(c)


def cmd_list(a, c):
    where = {"pending": "status='pending'", "all": "1", "confirmed": "status='confirmed'"}[a.which]
    for r in c.execute(f"SELECT * FROM records WHERE {where} ORDER BY artist COLLATE NOCASE, title"):
        show_row(r)


def cmd_show(a, c):
    r = c.execute("SELECT * FROM records WHERE id=?", (a.id,)).fetchone() or sys.exit("no such record")
    show_row(r, detail=True)


def cmd_set(a, c, status):
    for i in a.ids:
        cur = c.execute("UPDATE records SET status=?, confirmed_by=? WHERE id=?",
                        (status, "manual" if status == "confirmed" else None, i))
        if getattr(a, "single", False):
            c.execute("UPDATE records SET kind='7\"' WHERE id=?", (i,))
        print(f"{i}: {status}" if cur.rowcount else f"{i}: no such record")
    c.commit()


ALIASES = {"first": "1", "1st": "1", "one": "1", "second": "2", "2nd": "2", "two": "2", "three": "3",
           "ii": "2", "iii": "3", "iv": "4", "vi": "6",
           "vol": "volume", "pt": "part", "the": ""}


def title_tier(found, wanted):
    """2 = same album title (word order/aliases aside, e.g. "Mack the Knife: Ella in Berlin"), 1 = loose, 0 = no.
    Keeps parentheses so "(Volume One)" and "(Volume Two)" stay different."""
    words = lambda x: sorted(w for w in (ALIASES.get(t, t) for t in norm(x, keep_paren=True).split()) if w)
    if words(found) == words(wanted):
        return 2
    f, w = norm(found, keep_paren=True), norm(wanted, keep_paren=True)
    return 1 if same(found, wanted) and difflib.SequenceMatcher(None, f, w).ratio() >= 0.75 else 0


def find_album(c, artist, album):
    """Best MusicBrainz release group for artist + album (albums preferred over singles/EPs), or None."""
    a = main_artist(artist)
    rank = {"Album": 3, "EP": 1, "Single": 0}

    def score(g):
        credit = " ".join(x.get("name", "") for x in g.get("artist-credit", []))
        year = int((g.get("first-release-date") or "9999")[:4] or 9999)
        return (title_tier(g.get("title"), album), same(main_artist(credit), a),
                rank.get(g.get("primary-type"), 1), -year, g.get("score", 0))   # originals over reissues

    for query in (f"releasegroup:{q(album)} AND artist:{q(a)}",                  # exact phrase
                  f"releasegroup:({norm(album)}) AND artist:{q(a)}"):            # any word order / punctuation
        res = mb(c, "release-group", query=query, limit=25)
        ok = [g for g in res.get("release-groups", []) if score(g)[0] and score(g)[1]]
        if ok:
            return max(ok, key=score)
    return None


UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def review_reason(rg, album, rel):
    """Why a name match shouldn't be added unattended (None = looks safe)."""
    year = int((rg.get("first-release-date") or "0")[:4] or 0)
    comp = "Compilation" in (rg.get("secondary-types") or [])
    if not rel["vinyl"]:
        return "no vinyl release on MusicBrainz (likely a CD/digital-era edition)"
    if comp and year >= 1990:
        return f"compilation dated {year}"
    if title_tier(rg.get("title"), album) < 2:
        return f'title only loosely matches ("{rg.get("title")}")'
    return None


def add_album(c, artist, album, dry=False, rgid=None, force=False):
    """Add one owned record by name (or pinned MusicBrainz release-group id).
    Returns (status, description); status 'review' = found but not trusted enough to add unattended."""
    if rgid:
        rg = mb(c, f"release-group/{rgid}", inc="artist-credits")
    else:
        rg = find_album(c, artist, album)
    if not rg:
        return "notfound", f"{artist} — {album}"
    credit = " ".join(x.get("name", "") + x.get("joinphrase", "") for x in rg.get("artist-credit", [])).strip() or artist
    credit = re.sub(r"\s+", " ", credit)
    key = f"rg:{rg['id']}"
    year = (rg.get("first-release-date") or "")[:4]
    desc = f"{credit} — {rg['title']} ({year or '?'}, {rg.get('primary-type')})"
    old = c.execute("SELECT status FROM records WHERE key=?", (key,)).fetchone()
    if old and old["status"] == "confirmed":
        return "have", desc
    rel = best_release(c, rg["id"], []) or {"release_id": None, "tracks": [], "vinyl": False, "year": None}
    why = None if (rgid or force) else review_reason(rg, album, rel)
    if why:
        return "review", f"{desc}  <- REVIEW: {why}"
    if dry:
        return "would-add", desc
    kind = '7"' if rg.get("primary-type") == "Single" else "LP"
    c.execute("""INSERT INTO records (key, kind, artist, title, year, rgid, release_id, status, confirmed_by, tracks, manual)
                 VALUES (?,?,?,?,?,?,?, 'confirmed', 'manual', ?, 1)
                 ON CONFLICT(key) DO UPDATE SET status='confirmed', confirmed_by='manual'""",
              (key, kind, credit, rg["title"], year or rel["year"], rg["id"], rel["release_id"], json.dumps(rel["tracks"])))
    c.commit()
    return "added", f"{desc} [{len(rel['tracks'])} tracks]"


def cmd_add(a, c):
    status, desc = add_album(c, a.artist, a.album, force=True)   # explicit single add: trust the user
    print(f"{status}: {desc}")
    if status == "notfound":
        sys.exit(1)


def cmd_import(a, c):
    """Bulk add from a text file: one "Artist | Album" per line; '#' comments and '?' (unconfirmed) lines skipped."""
    lines = [l.strip() for l in open(a.file, encoding="utf-8")]
    todo = [l for l in lines if l and not l.startswith(("#", "?")) and "|" in l]
    skipped = [l for l in lines if l.startswith("?")]
    tally = {}
    for l in todo:
        parts = [x.strip() for x in l.split("|")]
        artist, album = parts[0], parts[1]
        rgid = next((p[3:] for p in parts[2:] if p.startswith("rg=") and UUID.match(p[3:])), None)
        try:
            status, desc = add_album(c, artist, album, dry=not a.apply, rgid=rgid, force=a.include_review)
        except MBError as e:
            status, desc = "error", f"{artist} — {album}: {e}"
        tally[status] = tally.get(status, 0) + 1
        mark = {"added": "+", "would-add": "+", "have": "=", "notfound": "?", "error": "!", "review": "~"}[status]
        print(f"{mark} {desc}" + ("" if status != "notfound" else "   <- NOT FOUND: fix spelling in the file"))
    print("\n" + ", ".join(f"{v} {k}" for k, v in tally.items()) + (f", {len(skipped)} unconfirmed (?) skipped" if skipped else ""))
    if tally.get("review"):
        print("~ = needs a look: pin the right one with '| rg=<musicbrainz id>' on its line, or add all with --include-review")
    if not a.apply:
        print("dry run; re-run with --apply to add them")


def cmd_have(a, c):
    hits = 0
    for r in c.execute("SELECT * FROM records WHERE status!='rejected'"):
        heard = json.loads(r["heard"])
        for t in json.loads(r["tracks"]) or [{"n": "", "title": h} for h in heard]:
            if norm(a.query) in norm(t["title"]) or norm(a.query) in norm(r["artist"]):
                hits += 1
                flag = "" if r["status"] == "confirmed" else f"  ({r['status']})"
                print(f"♪ {t['title']} — {r['artist']}, {r['title']} [{r['kind']}]{flag}")
    if not hits:
        print("not in the catalog (yet)")


def main():
    p = argparse.ArgumentParser(prog="vinyl", description=__doc__.split("\n\n")[0])
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("sync", help="pull FivePis events, update candidates, auto-confirm")
    s.add_argument("--no-pull", action="store_true", help="rebuild from local events only")
    s.add_argument("--publish", action="store_true", help="publish afterwards if the catalog changed")
    s.set_defaults(f=cmd_sync)
    for name, which in (("pending", "pending"), ("list", "all"), ("confirmed", "confirmed")):
        sp.add_parser(name, help=f"show {which} records").set_defaults(f=cmd_list, which=which)
    s = sp.add_parser("show", help="show one record with its tracklist"); s.add_argument("id", type=int); s.set_defaults(f=cmd_show)
    s = sp.add_parser("confirm", help="confirm records by id"); s.add_argument("ids", type=int, nargs="+")
    s.add_argument("--single", action="store_true", help='mark as a 7" single'); s.set_defaults(f=lambda a, c: cmd_set(a, c, "confirmed"))
    s = sp.add_parser("reject", help="reject records by id (wrong ID / not mine)"); s.add_argument("ids", type=int, nargs="+")
    s.set_defaults(f=lambda a, c: cmd_set(a, c, "rejected"))
    s = sp.add_parser("add", help="add a record you own by artist + album"); s.add_argument("artist"); s.add_argument("album")
    s.set_defaults(f=cmd_add)
    s = sp.add_parser("import", help='bulk add "Artist | Album" lines from a file (dry run unless --apply)')
    s.add_argument("file"); s.add_argument("--apply", action="store_true")
    s.add_argument("--include-review", action="store_true", help="also add matches flagged for review")
    s.set_defaults(f=cmd_import)
    s = sp.add_parser("have", help="do I have this song/artist?"); s.add_argument("query"); s.set_defaults(f=cmd_have)
    s = sp.add_parser("publish", help="write docs/catalog.json, commit, push"); s.add_argument("--dry-run", action="store_true")
    s.set_defaults(f=lambda a, c: publish(c, a.dry_run))
    a = p.parse_args()
    a.f(a, db())


if __name__ == "__main__":
    main()
