"""Builds a small TV guide for Kodi (IPTV Simple Client) that matches iptv-org's playlist.

Reads iptv-org's Americas playlist, keeps the US channels in English (iptv-org's own language data),
names each one as iptv-org's channel database does ("Court TV", not "Court TV (1080p)"), and finds
its listings in a few public XMLTV guides (by iptv-org channel id, then by name), plus whatever
iptv-org's own grabber fetched for the rest. Only channels with listings are kept.

  python build_guide.py plan    writes grab.channels.xml: the channels no public guide has, each with
                                the best site iptv-org/epg can grab it from (EPG_DIR: a checkout of
                                iptv-org/epg, for its sites/*/*.channels.xml)
  python build_guide.py         writes, into out/:
    us.m3u        the channels that have listings, with url-tvg pointing at guide.xml.gz
    guide.xml.gz  their listings from 2 hours ago to 36 hours ahead, under the playlist's own tvg-ids
    report.txt    how many channels have listings, from which source
                                (GRABBED: the grabber's guide.xml, read as one more source)
Standard library only. Run daily (GitHub Actions: .github/workflows/guide.yml).
"""
import glob, gzip, io, json, os, re, sys, time, urllib.error, urllib.request, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

PLAYLIST = "https://iptv-org.github.io/iptv/regions/amer.m3u"
API = "https://iptv-org.github.io/api/"   # feeds.json (languages, feed names), channels.json (names), logos.json
DEAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dead_streams.txt")
LANGUAGE = "eng"
SOURCES = [   # best first: a channel takes the first source that has it
    ("iptv-epg.org", "https://iptv-epg.org/files/epg-us.xml.gz"),
    ("Pluto TV", "https://i.mjh.nz/PlutoTV/us.xml.gz"),
    ("Samsung TV Plus", "https://i.mjh.nz/SamsungTVPlus/us.xml.gz"),
    ("Plex", "https://i.mjh.nz/Plex/us.xml.gz"),
    ("Roku", "https://i.mjh.nz/Roku/all.xml.gz"),
]
# iptv-org/epg sites to grab the rest from, in order of preference. tvtv.us maps the most US
# channels but returned nothing to GitHub's runners (2026-11-19), so it is only a last resort.
GRAB_SITES = ["tvpassport.com", "tvguide.com", "pluto.tv", "plex.tv", "ontvtonight.com", "watchyour.tv", "tvtv.us"]
GUIDE_URL = os.environ.get("GUIDE_URL", "guide.xml.gz")
BEFORE, AHEAD = timedelta(hours=2), timedelta(hours=36)
UA = {"User-Agent": "kodi-guide-builder/1.0 (+https://github.com)"}
# listings that are only a placeholder: "No Data", "Channel No Longer Available", "teleup-event_1679249810_2590"
PLACEHOLDER = re.compile(r"(no data|no information|channel no longer available|to be announced|[a-z-]+_\d+_\d+)\s*", re.I)
KEEP_TAGS = ("title", "sub-title", "desc", "category", "episode-num", "icon")   # smaller for Kodi


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=600) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def norm(name):
    """'ABC News Live (720p) [Geo-blocked]' -> 'abcnewslive'."""
    name = re.sub(r"\(.*?\)|\[.*?\]", "", name.lower())
    name = re.sub(r"\b(hd|sd|fhd|uhd|4k|east|tv channel)\b", "", name)
    return re.sub(r"[^a-z0-9]", "", name)


def xmltv_time(s):
    """'20261118210000 -0500' (or no offset, read as UTC) -> an aware datetime."""
    s = s.strip()
    if len(s) > 14:
        return datetime.strptime(s, "%Y%m%d%H%M%S %z")
    return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


# casing iptv-org has wrong, as the networks write their own names
FIX_NAME = {"bon appetit": "Bon Appétit", "ESPN 2": "ESPN2", "SPEEDVISION": "Speedvision"}


def base_name(tid, raw, feeds, channels):
    """iptv-org's own name for the channel and its feed: ('CNN', 'HD', is_main)."""
    ch, _, fd = tid.partition("@")
    c, f = channels.get(ch), feeds.get((ch, fd)) or {}
    if not c:   # not in the database: drop "(720p)", "[Geo-blocked]"
        return re.sub(r"\s*(\(\d+p\)|\[[^]]*\])", "", raw).strip(), "", True
    name = FIX_NAME.get(c["name"], c["name"])
    return name, ("" if f.get("is_main") else f.get("name") or ""), bool(f.get("is_main"))


def display_names(entries):
    """[(name, feed, is_main)] -> names as a TV service lists them, one entry per network: the brand alone
    ("CNN", not "CNN (HD)"), a West feed as "TNT West" (its own, later schedule), and None for a second
    feed of a channel already listed (the main feed is the one kept)."""
    names = [n + " West" if q == "West" else n for n, q, _ in entries]
    seen = set()
    for i in sorted(range(len(entries)), key=lambda i: (not entries[i][2], i)):
        if names[i] in seen:
            names[i] = None
        else:
            seen.add(names[i])
    return names


def logo_candidates(tid, logos):
    """iptv-org's logos for the channel that Kodi can draw, best first: no black or dark versions (lost on a
    dark screen), transparent before JPEG, then the largest: [(url, width)]. A Wikimedia SVG comes as its 960 px PNG."""
    ch, _, fd = tid.partition("@")
    found = []
    for l in logos.get(ch, []):
        if l.get("feed") not in (None, fd):
            continue
        url, fmt, width = l["url"], l.get("format"), l.get("width") or 0
        if fmt == "SVG":
            m = re.match(r"https://upload\.wikimedia\.org/wikipedia/([\w-]+)/(\w)/(\w\w)/([^/]+\.svg)$", url)
            if not m:
                continue
            url, width = f"https://upload.wikimedia.org/wikipedia/{m[1]}/thumb/{m[2]}/{m[3]}/{m[4]}/960px-{m[4]}.png", 960
        elif fmt not in ("PNG", "WebP", "JPEG"):
            continue
        dark = bool(set(l.get("tags") or []) & {"black", "dark"})
        found.append(((not dark, fmt != "JPEG", min(width, 1200), bool(l.get("in_use"))), url))
    return [(u, rank[2]) for rank, u in sorted(found, key=lambda x: x[0], reverse=True)]


def image_loads(url, tries=3):
    """True when url answers with an image (a deleted imgur picture answers with its removed.png).
    Wikimedia refuses bursts (429), so a refusal is tried again after a pause."""
    for n in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return r.headers.get_content_type().startswith("image/") and "removed" not in r.geturl()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503):
                return False
        except Exception:
            pass
        time.sleep(2 * (n + 1))
    return False


def pick_logos(kept, logos, icons):
    """The first logo that loads for each kept channel: iptv-org's, then the guides' icon, then the
    playlist's own: {tvg-id: url or ""}."""
    def one(c):
        own = (re.search(r'tvg-logo="([^"]*)"', c[2]) or [None, ""])[1]
        found = logo_candidates(c[0], logos)
        urls = [u for u, _ in found]
        # a small catalog logo (an old upload) loses to the guide's own icon, which is usually large
        urls = [icons.get(c[0])] + urls if found and found[0][1] < 300 else urls + [icons.get(c[0])]
        return c[0], next((u for u in urls + [own] if u and image_loads(u)), "")
    with ThreadPoolExecutor(16) as ex:
        return dict(ex.map(one, kept))


def dead_streams():
    """Stream addresses found dead (not found, timed out, no such server), from dead_streams.txt."""
    if not os.path.exists(DEAD):
        return set()
    return {l.split()[0] for l in open(DEAD, encoding="utf-8") if l.strip() and not l.startswith("#")}


def stream_lines(text, i):
    """The lines after the #EXTINF at text[i] up to its address: option lines (#EXTVLCOPT referrer or user
    agent, #KODIPROP) kept above it, as IPTV Simple reads them. None when the entry has no address."""
    opts = []
    for line in (l.strip() for l in text[i + 1:]):
        if line.startswith("#EXTINF"):
            return None
        if line.startswith(("#EXTVLCOPT", "#KODIPROP")):
            opts.append(line)
        elif line and not line.startswith("#"):
            return "\n".join(opts + [line])
    return None


def load_channels():
    """The playlist's US English channels (not the dead ones), and lookups by id and name."""
    feeds = {(f["channel"], f["id"]): f for f in json.loads(fetch(API + "feeds.json"))}
    channels = {c["id"]: c for c in json.loads(fetch(API + "channels.json"))}
    text = fetch(PLAYLIST).decode("utf-8", "replace").splitlines()
    dead, found = dead_streams(), []
    for i, line in enumerate(text):
        if not line.startswith("#EXTINF") or i + 1 >= len(text):
            continue
        tid = (re.search(r'tvg-id="([^"]*)"', line) or [None, ""])[1]
        ch, _, fd = tid.partition("@")
        if not ch.endswith(".us") or LANGUAGE not in (feeds.get((ch, fd)) or {}).get("languages", []):
            continue
        url = stream_lines(text, i)
        if url and url.splitlines()[-1] not in dead:
            found.append((tid, base_name(tid, line.rsplit(",", 1)[-1].strip(), feeds, channels), line.rsplit(",", 1)[0], url))
    # (tvg-id, iptv-org's name, the #EXTINF up to its title, address lines, (name, feed, is main));
    # the names the viewer sees are settled once the kept channels are known (write_outputs)
    chans = [(tid, base[0], head, url, base) for tid, base, head, url in found]
    by_base = {c[0].split("@")[0].lower(): c[0] for c in chans}
    by_name = {}
    for tid, name, *_ in chans:   # the clean name, then iptv-org's other names for the channel
        c = channels.get(tid.split("@")[0]) or {}
        for n in [name, c.get("name", "")] + c.get("alt_names", []):
            by_name.setdefault(norm(n), tid)
    return chans, by_base, by_name


def read_guides(sources, by_base, by_name, lo, hi):
    """Listings per playlist tvg-id from each (label, raw XMLTV bytes or None) in turn; a report; and each
    channel's icon in those guides (a logo of last resort)."""
    claimed, progs, report, icons = set(), {}, [], {}
    for label, raw in sources:
        if raw is None:
            report.append(f"{label}: unavailable")
            continue
        here, n = {}, 0   # source channel id -> playlist tvg-id
        for _, el in ET.iterparse(io.BytesIO(raw)):
            if el.tag == "channel":
                sid = el.get("id", "")
                base = sid.split("@")[0].lower()
                # iptv-epg.org also writes a service suffix: GravitasMovies_Plex.us
                tid = by_base.get(base) or by_base.get(re.sub(r"_[a-z0-9]+(\.[a-z]{2})$", r"\1", base))
                if not tid:
                    tid = next((by_name[k] for k in (norm(dn.text or "") for dn in el.findall("display-name")) if k in by_name), None)
                if tid and tid not in claimed:
                    claimed.add(tid)
                    here[sid] = tid
                icon = el.find("icon")
                if tid and icon is not None and icon.get("src"):
                    icons.setdefault(tid, icon.get("src"))
                el.clear()
                continue
            if el.tag != "programme":
                continue
            tid = here.get(el.get("channel", ""))
            try:
                keep = tid and xmltv_time(el.get("stop", "")) > lo and xmltv_time(el.get("start", "")) < hi
            except ValueError:
                keep = False
            if keep and PLACEHOLDER.fullmatch(el.findtext("title") or ""):
                keep = False   # a placeholder is no listing (a channel left with none is left out)
            if not keep:
                el.clear()
                continue
            el.set("channel", tid)
            for kid in list(el):
                if kid.tag not in KEEP_TAGS:
                    el.remove(kid)
            progs.setdefault(tid, []).append(el)
            n += 1
        report.append(f"{label}: {len(here)} channels, {n} listings")
    return progs, report, icons


def public_sources():
    for label, url in SOURCES:
        try:
            yield label, fetch(url)
        except Exception as e:
            print(f"{label}: unavailable ({e})")
            yield label, None


def plan(chans, progs):
    """grab.channels.xml for iptv-org/epg: each channel still without listings, from the best site that has it."""
    sites = {}   # iptv-org channel id -> {site: (site_id, lang, name)}
    for f in glob.glob(os.path.join(os.environ.get("EPG_DIR", "epg"), "sites", "*", "*.channels.xml")):
        for c in ET.parse(f).getroot():
            ch = (c.get("xmltv_id") or "").split("@")[0]
            if ch:
                sites.setdefault(ch, {}).setdefault(c.get("site"), (c.get("site_id"), c.get("lang") or "en", c.text or ""))
    rank = {s: i for i, s in enumerate(GRAB_SITES)}
    lines = []
    for tid, name, *_ in chans:
        options = sites.get(tid.split("@")[0])
        if tid in progs or not options:
            continue
        site = min(options, key=lambda s: rank.get(s, len(rank)))
        site_id, lang, _ = options[site]
        # xmltv_id is the playlist's own id, so the grabbed listings land on this exact feed
        lines.append(f'  <channel site={quoteattr(site)} site_id={quoteattr(site_id)} lang={quoteattr(lang)} xmltv_id={quoteattr(tid)}>{escape(name)}</channel>')
    with open("grab.channels.xml", "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n' + "\n".join(lines) + "\n</channels>\n")
    print(f"grab.channels.xml: {len(lines)} channels for iptv-org/epg")


def entry(head, name, logo):
    """The #EXTINF line with the viewer's name and the chosen logo."""
    head = re.sub(r' tvg-(name|logo)="[^"]*"', "", head)
    return head.replace("#EXTINF:-1", f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{logo}"', 1) + "," + name


def logos_by_channel():
    by = {}
    for l in json.loads(fetch(API + "logos.json")):
        by.setdefault(l["channel"], []).append(l)
    return by


def write_outputs(chans, progs, report, now, icons):
    kept = [c for c in chans if c[0] in progs]   # a channel with no listings anywhere is left out
    kept = [c[:4] + (name,) for c, name in zip(kept, display_names([c[4] for c in kept])) if name]
    logos = pick_logos(kept, logos_by_channel(), icons)
    kept = [(tid, name, entry(head, name, logos[tid]), url) for tid, _, head, url, name in kept]
    tv = ET.Element("tv", {"generator-info-name": "kodi-guide-builder"})
    for tid, name, *_ in kept:
        ET.SubElement(ET.SubElement(tv, "channel", {"id": tid}), "display-name").text = name
    for tid, *_ in kept:
        for p in sorted(progs[tid], key=lambda p: p.get("start")):
            tv.append(p)
    os.makedirs("out", exist_ok=True)
    with gzip.open("out/guide.xml.gz", "wb", compresslevel=9) as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8"))
    with open("out/us.m3u", "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{GUIDE_URL}"\n' + "".join(extinf + "\n" + url + "\n" for _, _, extinf, url in kept))
    summary = [f"built {now:%Y-%m-%d %H:%M} UTC",
               f"US English channels: {len(chans)}, with listings (kept): {len(kept)}, left out: {len(chans) - len(kept)}",
               f"logos: {sum(1 for t in logos.values() if t)} of {len(kept)}"] + report
    with open("out/report.txt", "w") as f:
        f.write("\n".join(summary) + "\n")
    print("\n".join(summary))


def main():
    now = datetime.now(timezone.utc)
    chans, by_base, by_name = load_channels()
    sources = list(public_sources())
    grabbed = os.environ.get("GRABBED")
    if grabbed and os.path.exists(grabbed):
        with open(grabbed, "rb") as f:
            sources.append(("iptv-org/epg grab", f.read()))
    progs, report, icons = read_guides(sources, by_base, by_name, now - BEFORE, now + AHEAD)
    if sys.argv[1:] == ["plan"]:
        plan(chans, progs)
    else:
        write_outputs(chans, progs, report, now, icons)


if __name__ == "__main__":
    main()
