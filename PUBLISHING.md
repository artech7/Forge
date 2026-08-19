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
