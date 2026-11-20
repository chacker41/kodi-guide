"""Checks for the guide builder: python3 test_build_guide.py"""
from datetime import datetime, timedelta, timezone
from build_guide import display_names, logo_candidates, norm, stream_lines, xmltv_time

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
print("ok")
