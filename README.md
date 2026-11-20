# kodi-guide

A daily TV guide for Kodi's IPTV Simple Client, matched to iptv-org's US channels.

`build_guide.py` reads iptv-org's Americas playlist, keeps the US channels in English (iptv-org's
language data), names them from iptv-org's channel database, and takes each
channel's listings from public XMLTV guides (iptv-epg.org, then i.mjh.nz's Pluto TV, Samsung TV
Plus, Plex and Roku guides), matched by iptv-org channel id and then by name. Channels none of
those have are grabbed with [iptv-org/epg](https://github.com/iptv-org/epg) (tvtv.us, Pluto TV and
others). Channels with no listings anywhere are left out.

Names follow a TV service's line-up: one entry per network under its brand ("CNN", not "CNN (HD)"),
West feeds as "TNT West". Each channel's logo is the best one in iptv-org's logo database that
loads (no black or dark versions, transparent PNG first, then the largest; Wikimedia SVGs as 960 px
PNGs), else the guides' own channel icon. Streams found dead from a home connection are listed in
`dead_streams.txt` and left out. A GitHub Action runs it every morning
and publishes to the `output` branch:

- Playlist: `https://raw.githubusercontent.com/chacker41/kodi-guide/output/us.m3u`
- Guide: `https://raw.githubusercontent.com/chacker41/kodi-guide/output/guide.xml.gz`
- What matched: `output/report.txt`

Kodi: IPTV Simple Client, M3U play list URL and XMLTV URL set to the two links above.
GitHub pauses scheduled workflows after 60 days without activity in a repository; if the guide
stops updating, open Actions and re-enable the workflow.
