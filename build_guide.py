"""Builds a small TV guide for Kodi (IPTV Simple Client) that matches iptv-org's playlist.

Reads iptv-org's Americas playlist, keeps the US channels, and finds each channel's listings in a few
public XMLTV guides: first by iptv-org channel id, then by channel name. Writes, into out/:
  us.m3u        the US channels, with url-tvg pointing at guide.xml.gz
  guide.xml.gz  their listings from 2 hours ago to 36 hours ahead, under the playlist's own tvg-ids
  report.txt    how many channels have listings, from which source
Standard library only. Run daily (GitHub Actions: .github/workflows/guide.yml).
"""
import gzip, io, os, re, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

PLAYLIST = "https://iptv-org.github.io/iptv/regions/amer.m3u"
SOURCES = [   # best first: a channel takes the first source that has it
    ("iptv-epg.org", "https://iptv-epg.org/files/epg-us.xml.gz"),
    ("Pluto TV", "https://i.mjh.nz/PlutoTV/us.xml.gz"),
    ("Samsung TV Plus", "https://i.mjh.nz/SamsungTVPlus/us.xml.gz"),
    ("Plex", "https://i.mjh.nz/Plex/us.xml.gz"),
    ("Roku", "https://i.mjh.nz/Roku/all.xml.gz"),
]
GUIDE_URL = os.environ.get("GUIDE_URL", "guide.xml.gz")
BEFORE, AHEAD = timedelta(hours=2), timedelta(hours=36)
UA = {"User-Agent": "kodi-guide-builder/1.0 (+https://github.com)"}

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

def main():
    now = datetime.now(timezone.utc)
    lo, hi = now - BEFORE, now + AHEAD
    text = fetch(PLAYLIST).decode("utf-8", "replace").splitlines()
    chans = []   # (tvg-id, name, extinf line, url)
    for i, line in enumerate(text):
        if line.startswith("#EXTINF") and i + 1 < len(text):
            tid = (re.search(r'tvg-id="([^"]*)"', line) or [None, ""])[1]
            if ".us@" in tid or tid.endswith(".us"):
                chans.append((tid, line.rsplit(",", 1)[-1].strip(), line, text[i + 1].strip()))
    by_base = {c[0].split("@")[0].lower(): c[0] for c in chans}
    by_name = {}
    for c in chans:
        by_name.setdefault(norm(c[1]), c[0])

    claimed = set()       # playlist tvg-ids that already have a source (the first one wins)
    progs = {}            # playlist tvg-id -> [programme elements]
    report = []
    for label, url in SOURCES:
        try:
            raw = fetch(url)
        except Exception as e:
            report.append(f"{label}: unavailable ({e})"); continue
        here = {}         # source channel id -> playlist tvg-id
        n = 0
        for _, el in ET.iterparse(io.BytesIO(raw)):
            if el.tag == "channel":
                sid = el.get("id", "")
                base = sid.split("@")[0].lower()
                # iptv-epg.org also writes a service suffix: GravitasMovies_Plex.us
                tid = by_base.get(base) or by_base.get(re.sub(r"_[a-z0-9]+(\.[a-z]{2})$", r"\1", base))
                if not tid:
                    for dn in el.findall("display-name"):
                        tid = by_name.get(norm(dn.text or ""))
                        if tid: break
                if tid and tid not in claimed:
                    claimed.add(tid); here[sid] = tid
                el.clear()
            elif el.tag == "programme":
                tid = here.get(el.get("channel", ""))
                if tid:
                    try:
                        s, e = xmltv_time(el.get("start", "")), xmltv_time(el.get("stop", ""))
                    except ValueError:
                        s = e = None
                    if s and e > lo and s < hi:
                        el.set("channel", tid)
                        for kid in list(el):   # titles, descriptions and pictures only: smaller for Kodi
                            if kid.tag not in ("title", "sub-title", "desc", "category", "episode-num", "icon"):
                                el.remove(kid)
                        progs.setdefault(tid, []).append(el); n += 1
                        continue
                el.clear()
        report.append(f"{label}: {len(here)} channels, {n} listings")
        del raw

    os.makedirs("out", exist_ok=True)
    tv = ET.Element("tv", {"generator-info-name": "kodi-guide-builder"})
    for tid, name, *_ in chans:
        if tid in progs:
            ch = ET.SubElement(tv, "channel", {"id": tid}); ET.SubElement(ch, "display-name").text = name
    for tid in progs:
        for p in sorted(progs[tid], key=lambda p: p.get("start")):
            tv.append(p)
    with gzip.open("out/guide.xml.gz", "wb", compresslevel=9) as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8"))
    with open("out/us.m3u", "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{GUIDE_URL}"\n')
        for tid, name, extinf, url in chans:
            f.write(extinf + "\n" + url + "\n")
    with_listings = sum(1 for c in chans if c[0] in progs)
    summary = [f"built {now:%Y-%m-%d %H:%M} UTC", f"US channels: {len(chans)}, with listings: {with_listings} ({with_listings * 100 // max(1, len(chans))}%)"] + report
    open("out/report.txt", "w").write("\n".join(summary) + "\n")
    print("\n".join(summary))

if __name__ == "__main__":
    main()
