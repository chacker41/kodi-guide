"""Checks for the guide builder: python3 test_build_guide.py"""
from datetime import datetime, timedelta, timezone
from build_guide import NON_ENGLISH_NAME, PLACEHOLDER, channel_numbers, display_names, whitened, logo_candidates, logo_rank, norm, stream_lines, xmltv_time

# placeholder listings are no listings
assert all(PLACEHOLDER.fullmatch(s) for s in ("No Data", "Channel No Longer Available", "teleup-event_1679249810_2590"))
assert not any(PLACEHOLDER.fullmatch(s) for s in ("Data", "No Data Found Here: The Movie", "3ABN Today"))

# channels named in another language are left out; English names that only look close are not
assert all(NON_ENGLISH_NAME.search(n) for n in ("CBS en espanol", "Telemundo West", "Cine Romantico", "MTV Flow Latino", "NEW K-POP"))
assert not any(NON_ENGLISH_NAME.search(n) for n in ("Cinemax West", "CineMoi", "Latitude TV", "Discovery Channel", "Crime 360"))

# channel numbers: by kind, and a number once given stays with its channel
def ch(tid, cat): return (tid, tid, f'#EXTINF:-1 group-title="{cat}"', "u")
saved = {}
n = channel_numbers([ch("cnn", "News"), ch("espn", "Sports"), ch("abc", "News;General"), ch("x", "")], saved)
assert n == {"cnn": 101, "espn": 200, "abc": 100, "x": 950}, n
n = channel_numbers([ch("aaa", "News"), ch("cnn", "News"), ch("espn", "Sports")], saved)   # abc gone, aaa new
assert n == {"aaa": 102, "cnn": 101, "espn": 200} and saved["abc"] == 100, n   # abc's 100 is kept for it
n = channel_numbers([ch("cnn", "Movies")], saved)   # moved to another kind: a number in its new block
assert n == {"cnn": 300}, n

# names as a TV service lists them
assert display_names([("CNN", "HD", False), ("TNT", "West", False), ("TNT", "", True), ("ABC", "WWSB", False)]) == \
    ["CNN", "TNT West", "TNT", "ABC"]
assert display_names([("ESPN", "HD", False), ("ESPN", "", True)]) == [None, "ESPN"]   # one per network: the main feed
assert display_names([("Comedy", "CA", False), ("Comedy", "US", False)]) == ["Comedy", None]

# logos: no dark ones, PNG before JPEG, then the largest; a Wikimedia SVG as its 960 px PNG
logos = {"X.us": [
    {"feed": None, "format": "PNG", "width": 2000, "tags": ["black"], "url": "https://a/black.png"},
    {"feed": None, "format": "JPEG", "width": 1500, "tags": [], "url": "https://a/big.jpg"},
    {"feed": None, "format": "PNG", "width": 300, "tags": ["color"], "url": "https://a/small.png"},
    {"feed": None, "format": "SVG", "width": 100, "tags": [], "url": "https://upload.wikimedia.org/wikipedia/commons/d/df/X_logo.svg"},
    {"feed": "Other", "format": "PNG", "width": 900, "tags": [], "url": "https://a/other-feed.png"},
    {"feed": None, "format": "GIF", "width": 900, "tags": [], "url": "https://a/x.gif"}]}
assert [light for _, light in logo_candidates("X.us@SD", logos)] == [True, True, True, False]
assert [u for u, _ in logo_candidates("X.us@SD", logos)] == [
    "https://upload.wikimedia.org/wikipedia/commons/thumb/d/df/X_logo.svg/960px-X_logo.svg.png",
    "https://a/small.png", "https://a/big.jpg", "https://a/black.png"]

m3u = ['#EXTINF:-1 tvg-id="AE.us@East",A&E', "#EXTVLCOPT:http-referrer=https://tvnow.st", "https://a.example/ae.m3u8",
       '#EXTINF:-1 tvg-id="X.us",No address', '#EXTINF:-1 tvg-id="B.us",B', "", "https://b.example/b.m3u8"]
assert stream_lines(m3u, 0) == "#EXTVLCOPT:http-referrer=https://tvnow.st\nhttps://a.example/ae.m3u8"
assert stream_lines(m3u, 3) is None
assert stream_lines(m3u, 4) == "https://b.example/b.m3u8"

assert xmltv_time("20261118210000 -0500") == datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)
assert xmltv_time("20261118210000 +0530").utcoffset() == timedelta(hours=5, minutes=30)
assert xmltv_time("20261118210000") == datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)
assert norm("ABC News Live (720p) [Geo-blocked]") == "abcnewslive"
assert norm("CBS News HD") == "cbsnews"

# a black logo on a see-through background comes back white; colour, or a solid square, is left alone
try:
    import io
    from PIL import Image

    def png(fill, see_through=True):
        im = Image.new("RGBA", (40, 20), (0, 0, 0, 0) if see_through else (255, 255, 255, 255))
        im.paste(Image.new("RGBA", (20, 10), fill), (10, 5))
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
    w = whitened(png((0, 0, 0, 255)))
    assert w and Image.open(io.BytesIO(w)).getpixel((15, 8)) == (255, 255, 255, 255)
    assert Image.open(io.BytesIO(w)).getpixel((0, 0))[3] == 0
    assert whitened(png((200, 30, 30, 255))) is None
    assert whitened(png((0, 0, 0, 255), see_through=False)) is None
except ImportError:
    print("Pillow not installed: white logos not checked")
print("ok")

try:   # a see-through PNG beats a bigger JPEG; between see-through PNGs, the bigger wins
    from PIL import Image
    def img(size, fmt, mode):
        b = io.BytesIO(); Image.new(mode, size).save(b, fmt); return b.getvalue()
    clear_small, clear_big, jpeg_big = img((300, 300), "PNG", "RGBA"), img((900, 400), "PNG", "RGBA"), img((1600, 900), "JPEG", "RGB")
    assert logo_rank(clear_small) > logo_rank(jpeg_big)
    assert logo_rank(clear_big) > logo_rank(clear_small)
    assert logo_rank(b"not a picture") == (False, False, 0)
except ImportError:
    print("Pillow not installed: logo_rank checks skipped")
