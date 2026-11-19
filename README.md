# kodi-guide

A daily TV guide for Kodi's IPTV Simple Client, matched to iptv-org's US channels.

`build_guide.py` reads iptv-org's Americas playlist, keeps the US channels, and takes each
channel's listings from public XMLTV guides (iptv-epg.org, then i.mjh.nz's Pluto TV, Samsung TV
Plus, Plex and Roku guides), matched by iptv-org channel id and then by name. A GitHub Action
runs it every morning and publishes to the `output` branch:

- Playlist: `https://raw.githubusercontent.com/chacker41/kodi-guide/output/us.m3u`
- Guide: `https://raw.githubusercontent.com/chacker41/kodi-guide/output/guide.xml.gz`
- What matched: `output/report.txt`

Kodi: IPTV Simple Client, M3U play list URL and XMLTV URL set to the two links above.
GitHub pauses scheduled workflows after 60 days without activity in a repository; if the guide
stops updating, open Actions and re-enable the workflow.
