# osu-ctb-file-renderer

A Discord bot that turns an osu!catch replay (`.osr`) into an MP4. Run `/render`, attach the replay as `osr_clip`, and the bot replies with a video. Replays from any other mode are rejected.

The catch logic is ported from osu!'s own ruleset (`osu.Game.Rulesets.Catch` in [ppy/osu](https://github.com/ppy/osu)): juice stream droplet generation, hard rock offsets, banana positions, hyperdash detection and catcher width all follow the game. A replay doesn't store judgements, so the renderer works out which fruits were caught from the catcher's position at each fruit's time.

## Requirements

- Python 3.11+
- `ffmpeg` on `PATH` (or set `FFMPEG_PATH`)
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
| `CACHE_DIR` | `./cache` | Where downloaded `.osu` files are cached |
| `MAX_UPLOAD_MB` | `10` | Largest video the bot will try to upload. The bot also respects the limit Discord reports for the server |
| `MAX_CONCURRENT_RENDERS` | `1` | Renders running at once |
| `RENDER_TIMEOUT_SECONDS` | `600` | A render is killed after this long |
| `USER_COOLDOWN_SECONDS` | `60` | Wait between renders for one user |
| `MAX_QUEUE_LENGTH` | `10` | Renders allowed to wait in the queue |
| `DEV_GUILD_ID` | unset | Sync commands to one server instantly while developing |
| `FFMPEG_PATH` | unset | Path to ffmpeg if it's not on `PATH` |

### Inviting the bot

In the Discord developer portal, open OAuth2 → URL Generator and tick the `bot` and `applications.commands` scopes, with the **Send Messages** and **Attach Files** permissions. Open the generated URL to add the bot to a server. `/render` is registered when the bot starts. Global commands can take a little while to appear the first time, so set `DEV_GUILD_ID` while testing.

## Rendering from the command line

```sh
python render.py replay.osr out.mp4
python render.py replay.osr out.mp4 --beatmap path/to/map.osu   # skip the API and use a local .osu
python render.py replay.osr out.mp4 --max-mb 25
```

Output is 1280x720 at 60 fps, H.264, with the bitrate chosen so the file fits the size limit. If the first encode overshoots, it is re-encoded at a lower bitrate.

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
4. Optional: attach a volume mounted at `/data` and set `CACHE_DIR=/data` so the beatmap cache survives deploys. Without one, maps are downloaded again after each deploy, which is fine. A service with a volume can't use replicas.
5. Check the deploy logs for `ffmpeg found` and `Connected to Discord as ...`, then try a catch replay and a standard replay (which should be rejected).

Rendering is CPU-heavy and Railway bills CPU by usage, so renders cost money while an idle bot costs very little. Start with `MAX_CONCURRENT_RENDERS=1`. Each render logs its duration and peak memory, so you can tune the limits from real numbers. The free plan's resource limits are tight for video encoding, so plan on Hobby. Check Railway's current pricing before committing. The account also needs to be out of the limited trial, because restricted outbound networking can stop the bot connecting to Discord.

## Known limitations

- No audio yet. That needs the full beatmap set, not just the `.osu` file.
- osu!stable truncates some slider and juice stream timings to integers, so a few edge-case maps may look slightly off.
- Catches are judged with the catcher position interpolated at the fruit's exact time. This matches the game on the replays tested so far, but a catch right on the edge of the plate could be judged differently from the client.
- Hidden and Flashlight are shown in the mod list but not drawn.
