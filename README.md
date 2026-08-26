# Forge

Forge watches your media library, works out what each file actually needs,
and fixes it — transcoding for space, leveling audio that makes you reach
for the remote every ten minutes, catching files that are quietly damaged
before wasting an hour encoding them, and keeping everything named and
filed the way your media server expects. Point it at a folder once; after
that it runs unattended.

It's two pieces:

- **The server** watches folders, decides what needs doing, keeps the
  queue, and files finished work away. It never converts anything itself,
  so it's happy running on a NAS.
- **Workers** do the actual converting, on whatever hardware you point at
  them — one machine or several, GPU or CPU, mixed brands. A worker
  connects *out* to the server, so adding one needs no configuration on
  the server's side.

What's in it: automatic scanning and queueing with a step-by-step setup
wizard; per-library skip rules, bitrate ceilings, and work schedules; a
health check before every encode that catches damaged audio automatically
and can hand truly unrecoverable files to Radarr/Sonarr for a fresh copy;
HDR handling, subtitle and audio-track tidying, chapter and metadata
cleanup, automatic renaming with optional TMDB lookup; a Stats view with
per-library breakdowns you can click into down to individual files; and a
Library Health tab for the things a transcode alone won't fix — audio
loudness leveling, missing English audio/subtitles, missing chapters.

## Installing

Forge is two things you install separately: the **server** (once,
wherever your media lives) and one or more **workers** (on whatever
machines will actually do the converting).

### The server

Runs in Docker, on any Docker host — a Synology/QNAP NAS, a Linux box,
whatever's always on.

```bash
docker compose up -d
```

Edit the media volume path in `docker-compose.yml` first — the paths you
type into Forge are the paths *inside* the container, so a share mounted
at `/media` means a watch folder like `/media/Movies`.

Then open `http://<that-machine>:8420` and add your first library.
Nothing converts until at least one worker connects — the interface says
so, with the exact command to run.

### Adding a worker — macOS

Run it natively, not in Docker — Docker Desktop can't reach
VideoToolbox, so a containerized Mac worker silently falls back to slow
software encoding.

```bash
brew install ffmpeg
./run-node.sh http://<server-address>:8420
```

### Adding a worker — Linux

Native, for an NVIDIA or Intel GPU:

```bash
./run-node.sh http://<server-address>:8420
```

Or in Docker — copy the `worker` folder to that machine, set `SERVER` in
`worker/docker-compose.yml`, uncomment the block matching its GPU, then:

```bash
cd worker && docker compose up -d
```

### Adding a worker — Windows

```powershell
.\run-node.ps1 -Server http://<server-address>:8420
```

Stops when the window closes. For something that keeps running
unattended — through reboots, sign-outs, and Remote Desktop disconnects —
run this once instead, from an elevated PowerShell:

```powershell
.\worker\install-startup-task.ps1 -Server http://<server-address>:8420 -Mounts \\server\share
```

### If a worker can reach the same files over a network share

Set `-Mounts` (Windows) or `MOUNTS` (macOS/Linux) so Forge knows how that
machine's view of the share lines up with the server's. Then the worker
reads and writes the share directly instead of copying files across the
network — much faster. Without it, files are streamed to the worker and
the result sent back, which works from anywhere but is slower.

### Running it behind a reverse proxy

See `PROXY.md` — websockets need to be passed through explicitly or the
live view never updates, and Forge has no login of its own, so anything
reachable from outside your home network needs something in front of it.

## Future additions

Not built yet, roughly in the order they'd matter:

- A login/token gate, for anyone who wants Forge reachable outside their
  home network without relying entirely on a VPN
- Notifications (Discord/ntfy/webhook) for things that happen while
  you're not watching — a file landing in Ignored, a Radarr/Sonarr
  re-fetch, a deep scan finishing
- A backup/export story for `forge.db` — right now, everything Forge has
  ever learned about your library lives in one SQLite file
- More Library Health checks in the same shape as loudness leveling and
  the language/chapter checks — resolution consistency within a season,
  others as they come up
- A search box in the Queue for large libraries
- Per-library work schedules (right now, working hours are set once,
  globally)
- Subtitle burn-in
- User accounts, if it's ever more than one household on the same
  instance
