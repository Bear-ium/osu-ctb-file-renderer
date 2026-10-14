# osu-ctb-file-renderer

A Discord bot that turns an osu!catch replay (`.osr`) into an MP4. Run `/render`, attach the replay as `osr_clip`, and the bot replies with a video. Replays from any other mode are rejected.

The video looks like an osu! replay: the map's own background fills the frame (dimmed, and lighter during breaks), the map's music plays in sync, and the playfield sits in the middle with an overlay HUD showing accuracy, misses and combo. Fruits and the catcher can use an osu! skin.

The catch logic is ported from osu!'s own ruleset (`osu.Game.Rulesets.Catch` in [ppy/osu](https://github.com/ppy/osu)): juice stream droplet generation, hard rock offsets, banana positions, hyperdash detection and catcher width all follow the game. A replay doesn't store judgements, so the renderer works out which fruits were caught from the catcher's position at each fruit's time.

## Requirements

- Python 3.11+
- `ffmpeg` on `PATH` (or set `FFMPEG_PATH`), with the AAC encoder (standard builds have it)
- A Discord bot token
- osu! API v2 client credentials (create an OAuth application at <https://osu.ppy.sh/home/account/edit>, the callback URL can be anything)

## Setup

```sh
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
python -m bot
```

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | required | Bot token |
| `OSU_CLIENT_ID` / `OSU_CLIENT_SECRET` | required for new maps | osu! API credentials used to look up beatmaps by hash |
| `CACHE_DIR` | `./cache` | Where downloaded `.osu` files, songs and backgrounds are cached |
| `MAX_UPLOAD_MB` | `10` | Largest video the bot will try to upload. The bot also respects the limit Discord reports for the server |
| `MAX_CONCURRENT_RENDERS` | `1` | Renders running at once |
| `RENDER_TIMEOUT_SECONDS` | `600` | A render is killed after this long, including the song download |
| `USER_COOLDOWN_SECONDS` | `60` | Wait between renders for one user |
| `MAX_QUEUE_LENGTH` | `10` | Renders allowed to wait in the queue |
| `DEV_GUILD_ID` | unset | Sync commands to one server instantly while developing |
| `FFMPEG_PATH` | unset | Path to ffmpeg if it's not on `PATH` |
| `BEATMAP_MIRRORS` | see below | Comma-separated mirror URLs for the full beatmap set, tried in order. Each must be `https://` and contain `{set_id}` |
| `MAX_SET_DOWNLOAD_MB` | `80` | Largest beatmap set the bot will download |
| `CACHE_MAX_MB` | `500` | Size limit for cached songs and backgrounds; the least recently used are removed first |
| `BACKGROUND_DIM_PLAY` | `0.7` | How dark the background is during play, from 0 (not dimmed) to 1 (black) |
| `BACKGROUND_DIM_REST` | `0.4` | How dark it is before the first fruit, during breaks and after the last fruit |
| `SKIN_DIR` | unset | Folder with an osu! skin's catch images (see [Skins](#skins)) |

### Inviting the bot

In the Discord developer portal, open OAuth2 → URL Generator and tick the `bot` and `applications.commands` scopes, with the **Send Messages** and **Attach Files** permissions. Open the generated URL to add the bot to a server. `/render` is registered when the bot starts. Global commands can take a little while to appear the first time, so set `DEV_GUILD_ID` while testing.

## Music and background

The `.osu` file names the song and background, but they only exist in the full beatmap set (`.osz`), which osu! doesn't offer through its API. The bot downloads the set from a beatmap mirror, takes out just those two files, and caches them by beatmap. The defaults are:

1. `https://osu.direct/api/d/{set_id}?noVideo=1`
2. `https://api.nerinyan.moe/d/{set_id}?nv=1`

These are third-party services, not run by osu! or by this project. osu.direct's terms (<https://osu.direct/tos>) allow automatic downloading but ban spamming its API. nerinyan has no terms published on its site. catboy.best is left out because it refuses the bot's User-Agent; the bot always identifies itself honestly and never disguises its requests. Check each mirror's current terms before relying on it, and remove any that ask not to be used this way.

A set from a mirror is only used if it contains the exact `.osu` version the replay was played on, so the music always belongs to that version of the map. If no mirror has it, the bot falls back to the map's cover image from osu! as the background, then to a plain colour. The render still succeeds without music, and the reply says what was missing.

Music is in sync with the replay: with DT and HT it is sped up or slowed down without changing pitch, and with Nightcore it is sped up with the pitch raised, as in the game.

## Skins

Set `SKIN_DIR` to a folder containing an osu! skin's catch images to draw fruits and the catcher with them. The bot doesn't include any skin images. Copy them from a skin you have the right to use, such as one you made or one whose author allows it. The folder can be a whole skin folder; only these files are read:

- `fruit-pear`, `fruit-grapes`, `fruit-apple`, `fruit-orange`, each with an optional `-overlay`
- `fruit-drop` and `fruit-bananas`, each with an optional `-overlay`
- `fruit-catcher-idle` (or `fruit-catcher-idle-0` for an animated catcher)

All are PNG files, and `@2x` versions are used when present. Without the four fruit images the bot keeps its built-in look; any other missing image falls back to the built-in version of that piece.

Drawing follows osu!lazer's legacy skin code: the fruit type cycles pear, grapes, apple, orange by object, base images are tinted with the combo colour, overlays aren't tinted, and hyperdash fruits get a red glow behind them. The catcher is scaled so its visible width equals the width used for judging catches, with the image's 16th row on the plate line.

## Rendering from the command line

```sh
python render.py replay.osr out.mp4
python render.py replay.osr out.mp4 --beatmap path/to/map.osu   # skip the API and use a local .osu
python render.py replay.osr out.mp4 --max-mb 25
python render.py replay.osr out.mp4 --no-assets                 # no song or background download
```

Output is 1280x720 at 60 fps, H.264 with AAC audio, with the bitrate chosen so the file fits the size limit after paying for the audio. If the first encode overshoots, it is re-encoded at a lower bitrate.

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest
```

To also check the judgement logic against a real play, point the tests at a catch replay and its `.osu` file. The test checks that the computed object counts, misses and max combo match what the replay's header says:

```sh
CTB_SAMPLE_OSR=path/to/replay.osr CTB_SAMPLE_OSU=path/to/map.osu python -m pytest
```

## Deploying on Railway

1. Create a service from this repo. Railway builds the root `Dockerfile`, which installs Python and ffmpeg. The bot is a worker, so it needs no public port or domain.
2. Add `DISCORD_TOKEN`, `OSU_CLIENT_ID` and `OSU_CLIENT_SECRET` in the service's **Variables**.
3. Leave serverless / app sleeping **off**, or the bot goes offline when idle.
4. Optional: attach a volume mounted at `/data` and set `CACHE_DIR=/data` so maps, songs and backgrounds survive deploys. Without one, they're downloaded again after each deploy, which is fine. A service with a volume can't use replicas. Keep `CACHE_MAX_MB` below the volume's size.
5. Optional: to use a skin, add its catch images to the image (or the volume) and set `SKIN_DIR` to that folder.
6. Check the deploy logs for `ffmpeg found` and `Connected to Discord as ...`, then try a catch replay and a standard replay (which should be rejected).

Rendering is CPU-heavy and Railway bills CPU by usage, so renders cost money while an idle bot costs very little. Start with `MAX_CONCURRENT_RENDERS=1`. Each render logs its duration, the song download time and peak memory, so you can tune the limits from real numbers. The free plan's resource limits are tight for video encoding, so plan on Hobby. Check Railway's current pricing before committing. The account also needs to be out of the limited trial, because restricted outbound networking can stop the bot connecting to Discord.

## What happens to uploads

Uploaded files are treated as untrusted data. They are never run, opened by another program or sent anywhere.

- Only `.osr` files up to 5 MB are accepted. Each one is saved under a fixed name in a private temp folder and deleted after the render, whatever the outcome.
- The bot reads replays with its own strict parser. Every length, count and number is bounded, and the compressed movement data can't unpack past 8 MB. Anything that doesn't parse as an osu!catch replay is rejected.
- The beatmap hash in the replay has to match a real map on osu!, and the downloaded `.osu` file has to match that hash.
- Beatmaps are parsed with osu!'s own number limits, and there are caps on objects, slider complexity and file size. A hostile map can't exhaust memory or CPU, and every render is killed if it passes the time limit.
- Replays where the catcher moves more than 1.5x faster than osu!catch allows (outside hyperdashes) are rejected as edited. This catches teleporting and hand-edited movement. It can't prove a replay is legitimate, for example a cheat that moves within the game's speed limits.
- The only thing from the upload that is shown back is the player name, with mentions and Markdown escaped. The bot never re-shares the uploaded file itself.

## What happens to downloads

Beatmap sets, songs and cover images come from third parties, so they're treated as untrusted too.

- Downloads only go over HTTPS on port 443, and every redirect is checked before it is followed. Each connection goes to an address checked to be public, so a mirror can't point the bot at internal services.
- Downloads are streamed with size and time limits (90 seconds for the whole lookup), and compressed responses are refused.
- The `.osz` archive is never unpacked as a whole. Only the song and background named in the `.osu` file are copied out, under fixed names, after checking their names and sizes. Files in subfolders or with path tricks in their names are ignored.
- Songs must be MP3, OGG or WAV, and are checked to decode before use. ffmpeg may only open local files when reading them.
- Images must be JPEG or PNG and are checked for size before being decoded.
- Anything going wrong here never fails the render: the bot leaves out the music or background and says so in its reply.

## Known limitations

- osu!stable truncates some slider and juice stream timings to integers, so a few edge-case maps may look slightly off.
- Catches are judged with the catcher position interpolated at the fruit's exact time. This matches the game on the replays tested so far, but a catch right on the edge of the plate could be judged differently from the client.
- Hidden and Flashlight are shown in the mod list but not drawn. Storyboards, background videos, hitsounds and kiai effects aren't drawn either.
- Bananas are drawn at a fixed size instead of osu!'s shrinking and spinning animation.
