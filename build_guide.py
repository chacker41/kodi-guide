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
import glob, gzip, io, json, os, re, sys, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

PLAYLIST = "https://iptv-org.github.io/iptv/regions/amer.m3u"
API = "https://iptv-org.github.io/api/"   # feeds.json (languages, feed names), channels.json (names)
LANGUAGE = "eng"
SOURCES = [   # best first: a channel takes the first source that has it
    ("iptv-epg.org", "https://iptv-epg.org/files/epg-us.xml.gz"),
    ("Pluto TV", "https://i.mjh.nz/PlutoTV/us.xml.gz"),
    ("Samsung TV Plus", "https://i.mjh.nz/SamsungTVPlus/us.xml.gz"),
    ("Plex", "https://i.mjh.nz/Plex/us.xml.gz"),
    ("Roku", "https://i.mjh.nz/Roku/all.xml.gz"),
]
# iptv-org/epg sites to grab the rest from, most complete for US channels first
GRAB_SITES = ["tvtv.us", "tvpassport.com", "tvguide.com", "pluto.tv", "plex.tv", "ontvtonight.com", "watchyour.tv"]
GUIDE_URL = os.environ.get("GUIDE_URL", "guide.xml.gz")
BEFORE, AHEAD = timedelta(hours=2), timedelta(hours=36)
UA = {"User-Agent": "kodi-guide-builder/1.0 (+https://github.com)"}
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


def clean_name(tid, raw, feeds, channels):
    """iptv-org's own name for the channel, plus the feed's name when it is not the main feed."""
    ch, _, fd = tid.partition("@")
    c, f = channels.get(ch), feeds.get((ch, fd))
    if not c:
        return re.sub(r"\s*(\(\d+p\)|\[[^]]*\])", "", raw).strip()   # not in the database: drop "(720p)", "[Geo-blocked]"
    return c["name"] + (f" ({f['name']})" if f and not f.get("is_main") and f.get("name") else "")


def load_channels():
    """The playlist's US English channels: [(tvg-id, clean name, extinf line, url)], and lookups by id and name."""
    feeds = {(f["channel"], f["id"]): f for f in json.loads(fetch(API + "feeds.json"))}
    channels = {c["id"]: c for c in json.loads(fetch(API + "channels.json"))}
    text = fetch(PLAYLIST).decode("utf-8", "replace").splitlines()
    chans = []
    for i, line in enumerate(text):
        if not line.startswith("#EXTINF") or i + 1 >= len(text):
            continue
        tid = (re.search(r'tvg-id="([^"]*)"', line) or [None, ""])[1]
        ch, _, fd = tid.partition("@")
        if not ch.endswith(".us") or LANGUAGE not in (feeds.get((ch, fd)) or {}).get("languages", []):
            continue
        name = clean_name(tid, line.rsplit(",", 1)[-1].strip(), feeds, channels)
        extinf = re.sub(r' tvg-name="[^"]*"', "", line.rsplit(",", 1)[0]).replace("#EXTINF:-1", f'#EXTINF:-1 tvg-name="{name}"', 1)
        chans.append((tid, name, extinf + "," + name, text[i + 1].strip()))
    by_base = {c[0].split("@")[0].lower(): c[0] for c in chans}
    by_name = {}
    for tid, name, *_ in chans:   # the clean name, then iptv-org's other names for the channel
        for n in [name] + (channels.get(tid.split("@")[0]) or {}).get("alt_names", []):
            by_name.setdefault(norm(n), tid)
    return chans, by_base, by_name


def read_guides(sources, by_base, by_name, lo, hi):
    """Listings per playlist tvg-id from each (label, raw XMLTV bytes or None) in turn; and a report."""
    claimed, progs, report = set(), {}, []
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
                el.clear()
                continue
            if el.tag != "programme":
                continue
            tid = here.get(el.get("channel", ""))
            try:
                keep = tid and xmltv_time(el.get("stop", "")) > lo and xmltv_time(el.get("start", "")) < hi
            except ValueError:
                keep = False
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
    return progs, report


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


def write_outputs(chans, progs, report, now):
    kept = [c for c in chans if c[0] in progs]   # a channel with no listings anywhere is left out
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
               f"US English channels: {len(chans)}, with listings (kept): {len(kept)}, left out: {len(chans) - len(kept)}"] + report
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
    progs, report = read_guides(sources, by_base, by_name, now - BEFORE, now + AHEAD)
    if sys.argv[1:] == ["plan"]:
        plan(chans, progs)
    else:
        write_outputs(chans, progs, report, now)


if __name__ == "__main__":
    main()
