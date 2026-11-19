"""Checks for the guide builder: python3 test_build_guide.py"""
from datetime import datetime, timedelta, timezone
from build_guide import norm, xmltv_time

assert xmltv_time("20261118210000 -0500") == datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)
assert xmltv_time("20261118210000 +0530").utcoffset() == timedelta(hours=5, minutes=30)
assert xmltv_time("20261118210000") == datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)
assert norm("ABC News Live (720p) [Geo-blocked]") == "abcnewslive"
assert norm("CBS News HD") == "cbsnews"
print("ok")
