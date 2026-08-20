# Forge

Distributed video transcoding for a mixed-hardware homelab. Server on the NAS,
workers wherever the silicon is.

## Quick start (macOS)

```bash
unzip forge.zip && cd forge
chmod +x *.sh          # zip files can't carry the executable bit
brew install ffmpeg
./check-encoders.sh    # see what hardware encoding you actually have
./test-mac.sh          # starts the server and one worker
```

Then open http://127.0.0.1:8420 and click **Add a library**.

Run it natively on a Mac rather than in Docker — Docker Desktop can't reach
VideoToolbox, so a containerised Mac worker silently falls back to CPU x265.

## How it works

Jobs describe **intent**, not commands: "HEVC at quality 22, copy the audio."
Each worker detects and verifies its own encoders at startup, then translates
that intent into whatever its hardware speaks — `-cq` for NVENC,
`-global_quality` for QuickSync, `-qp` for AMF, `-crf` for x265. Add a new
machine and it just works; no per-encoder plugins to maintain.

**File access is per node.** A worker declares its mounts:

```json
MOUNTS='[{"server":"/media","local":"/mnt/nas/media"}]'
```

If a job's path falls under a mapped prefix, the worker opens the file
directly and writes the result back to the share. If not, the server streams
the source over HTTP and takes the finished file back. Remote nodes are only
given files over 512 MB, so the transfer is worth making.

Leases expire after 120 seconds. A node that dies mid-encode has its job
requeued automatically.

## Running it

```bash
# On the NAS
docker compose up -d forge worker-local

# On the GPU box
cd worker && docker compose -f compose.nvidia.yml up -d
```

Then open `http://nas:8420`. Edit the volume paths in `docker-compose.yml`
to match your share first.

To queue work by hand:

```bash
curl -X POST http://nas:8420/api/queue -H 'Content-Type: application/json' \
  -d '{"paths":["/media/Movies/example.mkv"],
       "spec":{"codec":"hevc","quality":22,"audio":"copy","container":"mkv"}}'
```

## Behind a reverse proxy

`PROXY.md`. The short version: pass websockets through or the live view never
updates, use a subdomain rather than a subfolder, and remember Forge has no
login of its own.

## Sending changes to GitHub

`UPDATING.md` — the short version is `./prep-repo.sh && git add -A && git
commit -m "..." && git push`.

## Running it from a container registry

`PUBLISHING.md` walks through putting this on GitHub so the images build
themselves and can be pulled with a `ghcr.io` address. Ready-made compose
files are in `deploy/`.

## What runs where

Forge is two pieces, and the split matters:

**The server** watches folders, works out what each file needs, keeps the
queue, renames finished files and files them away. It never converts
anything. The only outside program it runs is `ffprobe`, to look at a file
for a fraction of a second. It is happy on a NAS.

**Workers** do all the converting. They run on whichever computers you want
doing that work. A worker connects out to the server, so the server needs no
configuration when you add one — it appears in the interface within seconds.

```
        NAS                          your desktop
   +-------------+                +-----------------+
   |   server    | <------------- |     worker      |
   | watches,    |   connects out |  converts files |
   | queues,     |                |  using its GPU  |
   | files away  |                +-----------------+
   +-------------+                       ...and as many more as you like
```

### Running the server on a NAS

```bash
docker compose up -d
```

Edit the media volume in `docker-compose.yml` first. The paths you type into
Forge are the paths *inside* the container, so a share mounted at `/media`
means a watch folder of `/media/inbox`.

Nothing converts until you connect at least one worker. The interface says so
on the Nodes panel, with the exact command.

### Adding a machine that converts

On a Mac, run it natively — Docker on macOS can't reach VideoToolbox, so a
containerised Mac worker silently falls back to slow software encoding:

```bash
./run-node.sh http://your-nas:8420
```

On Linux with an NVIDIA or Intel GPU, either works. For Docker, copy the
`worker` folder to that machine, set `SERVER` in `worker/docker-compose.yml`,
uncomment the block matching its graphics hardware, and `docker compose up -d`.

If that machine can reach the same files over a network share, set `MOUNTS` so
Forge knows how its paths line up with the server's. Then it reads and writes
the share directly instead of copying files across the network, which is much
faster. Without it, files are streamed to the worker and the result sent back
— slower, but it works from anywhere.

### Stopping a machine converting

Set its **files at once** to **0** on the node card. It stays connected and
visible but is never given work. Useful for taking a desktop out of rotation
without stopping anything.

If you do want the NAS itself converting, uncomment the `worker-nas` service
in `docker-compose.yml`. It is off by default because most NAS hardware is
slow at this and will be tied up for hours.

## Why did one file fail to convert?

```bash
python3 check-media.py "/path/to/the/file.mkv"
```

Decodes every stream in turn and names the one that failed. A damaged audio
track is the usual answer, and the usual fix is to leave that library's audio
alone so the track is copied rather than decoded.

Run it on the machine holding the file — the worker, not the server.

## Why isn't Forge picking up a file?

```bash
python3 explain-file.py "/path/to/the/file.mkv"
```

Walks the same decisions the scanner makes and says which one rejected it —
wrong file type, still copying, already handled, a skip rule, or simply that
nothing needs doing.

## Checking a change didn't break something

```bash
python3 check-server.py    # calls every server function against a temp database
node check-ui.js           # renders the interface against a fake DOM
./verify.sh                # probes a running server for missing/broken endpoints
```

The first two catch the failure mode that syntax checks miss: code that
imports cleanly but breaks on a specific call, usually because an edit landed
in the wrong function.

## The scripts

| Script | What it does |
|---|---|
| `test-mac.sh` | Starts the server and one worker natively on macOS. Use this, not Docker — Docker Desktop can't reach VideoToolbox. |
| `check-encoders.sh` | Shows exactly which hardware encoders work here, with the real FFmpeg errors for any that don't. |
| `make-test-files.sh` | Builds a test library from one real video, with a file for every decision Forge can make. |
| `setup.sh` | Only needed if files were downloaded individually and lost their folders. |
| `queue-all.sh` | Queues everything the scanner has found, for manual testing. |

## How a file gets handled

Forge works out the least it can do to get what you asked for:

| Situation | What happens |
|---|---|
| Already H.265, audio is AC3 | Audio only — video copied bit-for-bit, takes seconds |
| Already H.265 and AAC | Skipped |
| Right codecs, wrong container | Repackaged, no re-encoding |
| H.265 but way over the bitrate limit | Video re-encoded |
| H.264 with AAC | Video only |
| H.264 with DTS | Full conversion |

Bitrate limits are per resolution, because a number that's generous at
720p is absurd at 4K. Defaults: 1500 / 3000 / 6000 / 18000 kbps for
SD / 720p / 1080p / 4K.

## Naming

Forge rewrites release names into the layout media servers expect:

```
Wrath.of.Man.2021.1080p.BluRay.x264.DUALAUDIO-GROUP.mkv
  -> Wrath of Man (2021)/Wrath of Man (2021).mkv
```

Jellyfin, Emby and Plex all read the same shape. They differ only in how
provider IDs are written — Jellyfin and Emby use `[tmdbid-123]`, Plex uses
`{tmdb-123}` and does not read the bracket form.

Offline it works from the filename alone, and leaves a name untouched when
there's no year or episode number to anchor it. With a TMDB key in Settings
it confirms titles against the database, fills in episode names, and adds
provider IDs so your server matches on the first try.

TMDB keys are free from themoviedb.org (Settings -> API -> Developer). Only
the title being identified is ever sent. This product uses the TMDB API but
is not endorsed or certified by TMDB.

## When a file won't get smaller

Some files are already about as small as they sensibly get — animation,
clean digital masters, anything previously encoded well. Re-encoding those
makes them *bigger*, which is the worst outcome: larger and a generation of
quality worse.

Forge handles that without asking:

1. A conversion that comes out bigger is rejected and the original put back.
2. It tries again at a smaller setting, working down the steps you chose.
3. If none of them help, it stops trying to shrink the picture. The original
   video stream is copied through untouched while the audio, tracks,
   subtitles, naming and filing all still happen.

The result is a properly processed, correctly named file in your library
with no quality lost. Nothing is stranded in the inbox.

This depends on the library keeping originals — with "delete" or "leave it
where it is" there is nothing to fall back on, and the wizard says so.

## What's here, and what isn't

Built: node registration and capability detection, job queue with leasing and
requeue, mixed local/streamed transport, live progress over WebSocket, library
scanner with ffprobe cache, savings stats, the UI.

Built since: watch-folder libraries with a step-by-step setup wizard, automatic
scanning and queueing, skip rules (extension, format, name, size, length,
bitrate), per-resolution bitrate ceilings, audio-only and remux fast paths, a
work schedule by day and time, and scheduled Originals cleanup that verifies
the replacement exists before deleting anything.

Also built: stream-level handling — reordering tracks, removing embedded
cover art, HDR to SDR conversion, 10-bit handling, colour tagging, preferred
audio languages, forced-subtitle preservation, chapters, metadata cleanup,
stereo downmixing and loudness levelling. Plus automatic renaming with
optional TMDB lookup.

Not yet: subtitle burn-in, per-library schedules, notifications, and any kind
of user accounts.

## Before you point it at real media

Jobs replace the original file. Test on copies until you trust it, and
consider adding a `keep_original` flag to the spec if you want a safety net.

## Testing on a Mac

Run it natively, not in Docker — Docker Desktop can't reach VideoToolbox, so
a containerised Mac worker falls back to CPU x265 and you learn nothing about
hardware performance.

```bash
brew install ffmpeg
chmod +x test-mac.sh queue-all.sh
./test-mac.sh
```

That generates three throwaway clips in `./testmedia`, starts the server and
one worker in a venv, and prints the UI URL. Click **Scan library**, then run
`./queue-all.sh` in another terminal. Ctrl-C stops both processes.

The worker should report `hevc_videotoolbox` as a verified encoder — green
badge in the node card. If it only shows `libx265`, your FFmpeg build lacks
VideoToolbox; check `ffmpeg -encoders | grep videotoolbox`.

Nothing outside `./testmedia` is touched.
