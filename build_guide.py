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
Standard library only, plus Pillow (optional) for white copies of black logos. Run daily (GitHub Actions: .github/workflows/guide.yml).
"""
import contextlib, glob, gzip, io, json, os, re, sys, threading, time, urllib.error, urllib.request, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

PLAYLIST = "https://iptv-org.github.io/iptv/regions/amer.m3u"
API = "https://iptv-org.github.io/api/"   # feeds.json (languages, feed names), channels.json (names), logos.json
DEAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dead_streams.txt")
NUMBERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_numbers.tsv")   # numbers once given
REMOVED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "removed_channels.txt")
LEAVE_OUT_CATEGORIES = {"religious"}   # iptv-org categories the household does not want
# names that say the channel is in another language ("CBS en espanol", "Telemundo", "Cine Romantico")
NON_ENGLISH_NAME = re.compile(
    r"[áéíóúñ¿¡ãõç]|\b(en espa[nñ]ol|espa[nñ]ol|spanish|latin|latino|latinx|latina|m[eé]xico|deportes|cine|"
    r"novelas?|telenovelas?|crimenes|bollywood|hindi|desi|tamil|k-?pop|k\.?movies|korean|telemundo|"
    r"univision|unimas|estrella|azteca|canela|pasiones)\b", re.I)
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
LOGO_BASE = os.environ.get("LOGO_BASE", "logos/")   # where out/logos is published (white copies of black logos)
DARK_LOGO = 30   # average brightness (0-255) of a logo's drawn pixels below which it is made white
BEFORE, AHEAD = timedelta(hours=2), timedelta(hours=36)
TV_LOGOS_LIST = "https://api.github.com/repos/tv-logo/tv-logos/git/trees/main?recursive=1"
TV_LOGOS_RAW = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/"
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
    dark screen), transparent before JPEG, then the largest: [(url, not dark)]. A Wikimedia SVG comes as its 960 px PNG."""
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
    return [(u, rank[0]) for rank, u in sorted(found, key=lambda x: x[0], reverse=True)]


WIKIMEDIA = threading.Semaphore(2)   # Wikimedia refuses bursts (429): two at a time


def image_bytes(url, tries=5):
    """The picture at url, or None when it does not answer with one (a deleted imgur picture answers
    with its removed.png). Wikimedia refuses bursts (429), so a refusal is tried again after a pause."""
    for n in range(tries):
        try:
            with WIKIMEDIA if "wikimedia.org" in url else contextlib.nullcontext(), \
                    urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                ok = r.headers.get_content_type().startswith("image/") and "removed" not in r.geturl()
                return r.read() if ok else None
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503):
                return None
        except Exception:
            pass
        time.sleep(2 * (n + 1))
    return None


def whitened(data):
    """A logo drawn in black on a see-through background (lost on Kodi's dark screens) as the same shape
    in white, as YouTube TV shows such logos: PNG bytes, or None when the logo is not like that or
    Pillow is not installed."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    small = im.copy()
    small.thumbnail((128, 128))
    px = small.tobytes()
    seen = [px[i:i + 3] for i in range(0, len(px), 4) if px[i + 3] > 128]
    if not seen or len(seen) > 0.9 * (len(px) // 4):   # nothing drawn, or no see-through background
        return None
    if sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in seen) / len(seen) >= DARK_LOGO:
        return None
    white = Image.new("RGBA", im.size, (255, 255, 255, 0))
    white.putalpha(im.getchannel("A"))
    out = io.BytesIO()
    white.save(out, "PNG", optimize=True)
    return out.getvalue()


def logo_rank(data):
    """How good a logo looks on a TV: big enough to be sharp (up to 400 px) first, then see-through, PNG, and the
    larger (up to 1200 px). Without Pillow every logo ranks the same, so the first that loads wins."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        clear = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
        return min(max(im.size), 400), clear, im.format == "PNG", min(max(im.size), 1200)
    except Exception:
        return 0, False, False, 0


def tv_logos():
    """tv-logo/tv-logos' US logos (large see-through PNGs) by normalized channel name: {name: url}."""
    token = os.environ.get("GITHUB_TOKEN")   # GitHub allows 60 unsigned API calls an hour per address
    try:
        req = urllib.request.Request(TV_LOGOS_LIST, headers={**UA, **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(req, timeout=60) as r:
            paths = [t["path"] for t in json.load(r)["tree"]]
    except Exception:
        return {}
    return {norm(p.split("/")[-1][:-len("-us.png")]): TV_LOGOS_RAW + p
            for p in paths if p.startswith("countries/united-states/") and p.endswith("-us.png")}


def pick_logos(kept, logos, icons):
    """The best-looking logo that loads for each kept channel (logo_rank), from iptv-org's, the guides' icons,
    tv-logos and the playlist's own (a dark-tagged iptv-org logo only when nothing else loads); a black one
    as its white copy in out/logos: {tvg-id: url or ""}."""
    os.makedirs("out/logos", exist_ok=True)
    named = tv_logos()

    def one(c):
        own = (re.search(r'tvg-logo="([^"]*)"', c[2]) or [None, ""])[1]
        urls = logo_candidates(c[0], logos) + [(u, True) for u in icons.get(c[0], []) + [named.get(norm(c[4])), own]]
        best = None
        for u, light in dict.fromkeys((u, light) for u, light in urls if u):
            data = image_bytes(u)
            if data:
                rank = (light,) + logo_rank(data)
                if best is None or rank > best[0]:
                    best = (rank, u, data)
        if not best:   # nothing answered (Wikimedia refuses bursts): the top candidate, for players to try later
            return c[0], next((u for u, light in urls if u and light), "")
        white = whitened(best[2])
        if not white:
            return c[0], best[1]
        name = re.sub(r"[^A-Za-z0-9.@-]", "_", c[0]) + ".png"
        with open(os.path.join("out/logos", name), "wb") as f:
            f.write(white)
        return c[0], LOGO_BASE + name
    with ThreadPoolExecutor(16) as ex:
        return dict(ex.map(one, kept))


def removed_channels():
    """iptv-org channel ids left out by choice, from removed_channels.txt."""
    if not os.path.exists(REMOVED):
        return set()
    return {l.split()[0] for l in open(REMOVED, encoding="utf-8") if l.strip() and not l.startswith("#")}


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
    dead, removed, found = dead_streams(), removed_channels(), []
    for i, line in enumerate(text):
        if not line.startswith("#EXTINF") or i + 1 >= len(text):
            continue
        tid = (re.search(r'tvg-id="([^"]*)"', line) or [None, ""])[1]
        ch, _, fd = tid.partition("@")
        if not ch.endswith(".us") or LANGUAGE not in (feeds.get((ch, fd)) or {}).get("languages", []):
            continue
        if ch in removed or LEAVE_OUT_CATEGORIES & set((channels.get(ch) or {}).get("categories", [])):
            continue
        # English must be the channel's PRIMARY language, not merely present as a second audio track.
        # Distinct from the "eng in languages" check above: that one also drops channels with no
        # languages field, whereas this default ([LANGUAGE]) would keep them — so both are needed.
        if (feeds.get((ch, fd)) or {}).get("languages", [LANGUAGE])[0] != LANGUAGE:
            continue
        if NON_ENGLISH_NAME.search((channels.get(ch) or {}).get("name", "") + " " + line.rsplit(",", 1)[-1]):
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
                    icons.setdefault(tid, []).append(icon.get("src"))
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


def entry(head, name, logo, number):
    """The #EXTINF line with the viewer's name, the chosen logo and the channel number."""
    head = re.sub(r' tvg-(name|logo|chno)="[^"]*"', "", head)
    return head.replace("#EXTINF:-1", f'#EXTINF:-1 tvg-chno="{number}" tvg-name="{name}" tvg-logo="{logo}"', 1) + "," + name


# Channel numbers by kind, as a cable line-up has them: (first number, last number, iptv-org categories).
# A channel goes in the block of its first category; categories not listed go to "more channels".
BLOCKS = [(100, 149, {"News", "Business", "Weather"}), (200, 299, {"Sports", "Outdoor", "Auto"}),
          (300, 399, {"Movies"}), (400, 499, {"Series"}), (500, 599, {"Classic", "Comedy"}),
          (600, 699, {"Entertainment", "General"}), (700, 799, {"Lifestyle", "Cooking", "Travel", "Shop", "Relax", "Music"}),
          (800, 899, {"Kids", "Animation", "Family"}), (900, 949, {"Documentary", "Education", "Science", "Culture"}),
          (950, 999, set())]
OVERFLOW = 1100   # past every block (the 1000s are kept for the household's own channels)


def channel_numbers(kept, saved):
    """{tvg-id: number}: the number a channel had before (if still in its block), else the first free one in its
    block. saved ({tvg-id: number}) is updated in place, so a number, once given, stays with its channel."""
    def block(head):
        cat = (re.search(r'group-title="([^"]*)"', head) or [None, ""])[1].split(";")[0]
        return next((b for b in BLOCKS if cat in b[2]), BLOCKS[-1])
    taken = set(saved.values())
    for tid, _, head, *_ in sorted(kept, key=lambda c: c[1].lower()):
        lo, hi, _ = block(head)
        if lo <= saved.get(tid, -1) <= hi:
            continue
        free = next((n for n in range(lo, hi + 1) if n not in taken), None)
        if free is None:
            free = next(n for n in range(OVERFLOW, 10**6) if n not in taken)
        saved[tid] = free
        taken.add(free)
    return {tid: saved[tid] for tid, *_ in kept}


def logos_by_channel():
    by = {}
    for l in json.loads(fetch(API + "logos.json")):
        by.setdefault(l["channel"], []).append(l)
    return by


def write_outputs(chans, progs, report, now, icons):
    kept = [c for c in chans if c[0] in progs]   # a channel with no listings anywhere is left out
    kept = [c[:4] + (name,) for c, name in zip(kept, display_names([c[4] for c in kept])) if name]
    logos = pick_logos(kept, logos_by_channel(), icons)
    saved = {}
    if os.path.exists(NUMBERS):
        saved = {l.split("\t")[0]: int(l.split("\t")[1]) for l in open(NUMBERS, encoding="utf-8") if "\t" in l}
    numbers = channel_numbers(kept, saved)
    with open(NUMBERS, "w", encoding="utf-8") as f:
        f.write("".join(f"{tid}\t{n}\n" for tid, n in sorted(saved.items(), key=lambda kv: kv[1])))
    kept = sorted(((tid, name, entry(head, name, logos[tid], numbers[tid]), url) for tid, _, head, url, name in kept),
                  key=lambda c: numbers[c[0]])
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
