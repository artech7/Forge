# Running a machine as a Forge worker

This folder is everything a computer needs to convert files for Forge. Copy
it to that machine — it doesn't need the rest of Forge, and it never needs
anything opened or forwarded, because it connects out to the server.

## macOS

Run it natively, not in Docker. Docker on macOS can't reach VideoToolbox, so
a containerised worker falls back to software encoding and runs roughly
thirty times slower.

```bash
brew install ffmpeg
./run-node.sh http://your-nas:8420
```

(`run-node.sh` lives in the main Forge folder, one level up.)

## Linux or Windows, with Docker

Edit `docker-compose.yml`: set `SERVER` to your NAS address, and uncomment
the block matching this machine's graphics hardware. Then:

```bash
docker compose up -d
```

## The first minute

On startup the worker tests every encoder FFmpeg claims to have, by actually
encoding with each one, and measures how fast it really is — at 8-bit and at
10-bit separately. That takes a minute or two and only happens once per run.

It matters because hardware encoders will happily accept work they then do in
software. Measuring catches that, and Forge sends each file to whichever
encoder is genuinely fastest for it.

## Sharing files instead of copying them

If this machine can see the same files as the server over a network share,
tell it how the paths line up:

```
MOUNTS: '[{"server":"/media","local":"/mnt/nas/media"}]'
```

- `server` is the path the **server** sees
- `local` is the path **this machine** sees

With that set, the worker reads and writes the share directly. Without it,
files are streamed over HTTP and the result sent back — which works from
anywhere, including over the internet, but is slower and only used for
larger files where the transfer is worth it.

## Stopping it

Ctrl-C, or `docker compose down`. To keep it connected but idle, set its
**files at once** to 0 on the node card in the interface.
