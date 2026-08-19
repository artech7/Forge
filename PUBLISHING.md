# Putting Forge on GitHub and pulling it from ghcr.io

The end result is two images you can pull by address:

```
ghcr.io/YOURNAME/forge:latest          the server — run this on the NAS
ghcr.io/YOURNAME/forge-worker:latest   the worker — run this where the converting happens
```

GitHub builds them for you every time you push. You never build or push an
image by hand.

## 1. Make the repository

On github.com, create a new repository called `forge`. Private is fine —
you can still pull from it, you just log in once on the NAS (step 5).

Don't add a README or licence; the folder already has one and an empty
commit is simpler to push into.

## 2. Get the folder ready

Your Forge folder has things in it that shouldn't go to GitHub: test videos,
virtual environments, and `server/data/forge.db`, which holds your libraries
and settings.

```bash
./prep-repo.sh
```

That writes a `.gitignore` covering all of it, clears out cache folders, and
prints exactly what will and won't be uploaded. It deletes none of your
media or settings — it only stops them being included. Safe to run again
any time.

## 3. Push it

From inside the Forge folder:

```bash
git init
git add .
git commit -m "Forge"
git branch -M main
git remote add origin https://github.com/YOURNAME/forge.git
git push -u origin main
```

If it asks for a password, GitHub wants a personal access token rather than
your account password: github.com → Settings → Developer settings → Personal
access tokens → Tokens (classic) → Generate, with the `repo` scope ticked.

## 4. Wait for the build

Open the **Actions** tab on your repository. Two workflows run:

- **Checks** — the same tests used while building Forge
- **Publish images** — builds both images for amd64 and arm64 and pushes them

The first run takes about ten minutes, mostly compiling FFmpeg dependencies
for arm64. Later runs are much quicker because the layers are cached.

When it finishes, the images appear under the **Packages** section of your
GitHub profile.

## 5. Make the packages visible (optional)

New packages are private by default. That's fine — step 6 covers it. If you
would rather not log in on the NAS, make them public:

Your profile → Packages → `forge` → Package settings → Change visibility →
Public. Repeat for `forge-worker`.

## 6. Log in on the NAS (only if the packages are private)

Create a token with the `read:packages` scope, then on the NAS:

```bash
echo 'YOUR_TOKEN' | docker login ghcr.io -u YOURNAME --password-stdin
```

Dockhand uses the same credentials once this is done.

## 7. Run it

Use `deploy/forge.yml`, replacing `YOURNAME`, the media path, and the `user:`
line. Then in Dockhand, paste it as a new stack — or on the command line:

```bash
docker compose -f forge.yml up -d
```

Open `http://your-nas:58420`.

Nothing converts until you connect a worker. The Nodes panel tells you how,
with the address already filled in.

## Updating later

```bash
git add -A && git commit -m "what changed" && git push
```

GitHub rebuilds, and `pull_policy: always` means the NAS picks it up on the
next restart of the stack.

To pin a version instead of tracking `latest`:

```bash
git tag v1.0.0
git push --tags
```

That produces `:v1.0.0`, `:1.0` and `:1` alongside `:latest`.

## Mounting your folders

The left side of a volume line is the path **on the NAS**. The right side is
the path **inside the container**, and that right-hand path is what you type
into Forge. It has to start with a `/`.

### Mount the share once, not twice

If the watch folder and the destination live on the same share, mount the
share once and use folders inside it:

```yaml
volumes:
  - forge-data:/data
  - /volume1/media:/media
```

Then in Forge:

| | |
|---|---|
| Folder to watch | `/media/inbox` |
| Folder to move finished files to | `/media/Movies` |

Those folders need to exist on the NAS first: `/volume1/media/inbox` and
`/volume1/media/Movies`.

Mounting the same share twice under different names works, but Docker treats
them as separate filesystems. Moving a finished file between them becomes a
full copy followed by a delete rather than an instant rename — slow for a
40 GB film, and it needs room for both copies at once. One mount avoids that.

### If they really are on different shares

```yaml
volumes:
  - forge-data:/data
  - /volume1/downloads:/downloads
  - /volume1/media:/media
```

| | |
|---|---|
| Folder to watch | `/downloads` |
| Folder to move finished files to | `/media/Movies` |

Finished files are copied across rather than renamed, which is unavoidable
when the shares are genuinely separate.

### Don't point the destination at the top of a mount

Forge keeps originals next to the destination, so `/media/Movies` puts them
in `/media/Originals/Movies`. A destination of `/media` would put them in
`/Originals` — the container's own read-only filesystem — and the failure
would only appear after a file had been converted. Forge refuses that
combination when you add the library and explains why.

### Working with Radarr and Sonarr

`deploy/forge-with-arrs.yml` is set up for this. Radarr and Sonarr import
into a staging folder, Forge converts and files the result into your library,
and the original waits in `Originals` until the cleanup removes it.

```
/volume1/media/inbox/movies   Radarr imports here, Forge watches it
/volume1/media/inbox/tv       Sonarr imports here, Forge watches it
/volume1/media/Movies         Forge files finished films here
/volume1/media/TV             Forge files finished episodes here
/volume1/media/Originals      sources wait here, then are deleted
```

Two things to get right:

**Point your media server at `Movies` and `TV`, not at the top of the
share.** `Originals` sits alongside them, so a library pointed at
`/volume1/media` would index every original as a duplicate.

**Radarr and Sonarr lose track of the file.** They record where they put it,
and Forge then moves it somewhere else. The film plays fine and your media
server finds it, but Radarr still believes it lives in the staging folder, so
it may re-download or report it missing.

If that matters to you, run Forge over the library instead: point Radarr at
`/media/Movies` as usual, and give Forge a library that watches
`/media/Movies` with **no destination folder**. It then converts each file
where it sits, and Radarr's record stays correct. The trade-off is that
Forge's renaming has nothing to do — Radarr has already named everything —
so turn that off for such a library.

**If your downloads are hardlinked or still seeding**, the torrent client
keeps its own copy regardless of what Forge does, so you'll have two copies
until the torrent is removed. That's the download client's doing, not
Forge's.

### Converting on a Windows desktop

Run the worker natively so NVENC works directly:

```powershell
.\run-node.ps1 -Server http://your-nas:58420 -Mounts '{"server":"/media","local":"Z:/Media"}'
```

`-Mounts` says the server's `/media` is this machine's `Z:`. With it, the
worker opens files straight off the share; without it, every file is copied
across and back.

A UNC path works too: `"local":"//nas/media"`.

Needs Python and a full FFmpeg build on PATH:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
```

Then open a new terminal so the PATH change takes effect.

### Permissions

The `user:` line has to match whoever owns the media on the NAS, or Forge
writes files your media server can't read. On Synology:

```bash
id yourusername
```

Use the `uid` and the group id you want, for example `user: "1026:100"`.

## Notes on the compose files

**`user:`** — set this to the account that owns your media, or finished files
end up owned by the wrong user and your media server can't read them. On
Synology, `id yourusername` gives you the numbers.

**`read_only: true`** works because the server writes only to `/data`, which
is a volume. The worker writes partial encodes to `/scratch`, which is why
its compose file gives that a tmpfs — size it above your largest file, or
use a real volume if memory is tight.

**Ports** — the server listens on 8420 inside the container. Map it to
whatever you like outside; `deploy/forge.yml` uses 58420.

**The worker needs no ports at all.** It connects out to the server, so it
works from anywhere that can reach the NAS, including over a VPN.
