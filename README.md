# vinyl

A record catalog built by playing records.
Site: https://se7enmilgram.github.io/vinyl/ (served from `docs/`; data in [`docs/catalog.json`](docs/catalog.json)).

## How it works

1. A **Raspberry Pi at the turntable** streams the mixer and identifies each song with Shazam. It treats silence as a
   track or side boundary and keeps a log of what it heard.
2. A **home server** runs `vinyl sync` every few minutes. It pulls that log, splits it into sides, and resolves each side
   to a MusicBrainz release group: the album that contains the songs heard, not whatever box set or edition Shazam names.
3. Records are **auto-confirmed** when a full vinyl side was heard (one miss allowed on sides with 4+ tracks), when 4+
   album tracks were heard in one session, or when both sides of a 45 were heard. Everything else stays pending.
4. Records can also be added by name (`vinyl add`, or `vinyl import` for a whole shelf).
5. `vinyl publish` writes confirmed records (album-level, not pressing-level) to `docs/catalog.json`, fetches 250px
   covers from the Cover Art Archive, and pushes. The page is static and renders the JSON in the browser.

The working database (raw play events, candidates, MusicBrainz cache) stays private on the server; only confirmed records
are published.

## CLI

```
vinyl sync [--publish]       pull plays, update candidates, auto-confirm (and publish if changed)
vinyl pending | list | confirmed
vinyl show ID                tracklist (♪ = heard)
vinyl confirm ID… [--single] accept a candidate (mark as a 45)
vinyl reject ID…             wrong ID / not mine; stays rejected
vinyl add "Artist" "Album"   add a record without playing it
vinyl import FILE [--apply]  bulk add "Artist | Album [| rg=<mbid>]" lines; dry run unless --apply
vinyl have "song or artist"  do I have it?
vinyl retrack                re-pick tracklists (after a selection fix)
vinyl publish [--dry-run]
```

## catalog.json

```json
{"version": 1, "generated": "YYYY-MM-DD", "count": N,
 "albums": [{"id": "rg:<mbid>", "kind": "LP | 7\"", "artist": "…", "title": "…", "year": "1975",
             "cover": "covers/<mbid>.jpg", "last_played": "YYYY-MM-DD", "musicbrainz": "https://…",
             "tracks": [{"n": "A1", "title": "…", "heard": true}]}]}
```
