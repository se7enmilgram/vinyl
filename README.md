# vinyl

Record catalog for se7enmilgram, built by playing records.
Site: https://se7enmilgram.github.io/vinyl/ (served from `docs/`; data in [`docs/catalog.json`](docs/catalog.json)).

## How it works

1. **FivePis** (Raspberry Pi on the turntable) streams the mixer and runs `vinyl-id.service`. That service identifies each
   song with Shazam, uses silence to mark track and side boundaries, and logs evidence to `~/vinyl-id/events.jsonl`.
2. **chombo** runs `vinyl sync` every few minutes (systemd user timer). It pulls that log over a read-only SSH key, splits it
   into sides, and resolves each side to a MusicBrainz release group: the one containing the songs heard, not the box set
   or edition Shazam names.
3. Records are **auto-confirmed** when a full vinyl side was heard (one miss allowed on sides with 4+ tracks), when 4+
   album tracks were heard in one session, or when both sides of a 45 were heard. Everything else stays pending.
4. `vinyl publish` writes confirmed records (album-level, not pressing-level) to `docs/catalog.json`, fetches 250px
   covers from the Cover Art Archive, and pushes. The page is static and renders the JSON client-side.

The working database (`~/.local/share/vinyl/vinyl.db`: raw events, candidates, MusicBrainz cache) never leaves chombo.

## CLI

```
vinyl sync [--publish]       pull events, update candidates, auto-confirm (and publish if changed)
vinyl pending | list | confirmed
vinyl show ID                tracklist (♪ = heard)
vinyl confirm ID… [--single] accept a candidate (mark as a 45)
vinyl reject ID…             wrong ID / not mine; stays rejected
vinyl add "Artist" "Album"   add a record without playing it
vinyl have "song or artist"  do I have it?
vinyl publish [--dry-run]
```

## catalog.json

```json
{"version": 1, "generated": "YYYY-MM-DD", "count": N,
 "albums": [{"id": "rg:<mbid>", "kind": "LP | 7\"", "artist": "…", "title": "…", "year": "1975",
             "cover": "covers/<mbid>.jpg", "last_played": "YYYY-MM-DD", "musicbrainz": "https://…",
             "tracks": [{"n": "A1", "title": "…", "heard": true}]}]}
```
