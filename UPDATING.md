# Sending changes to GitHub

You already have the repository, so this is the whole loop.

## Every time

From inside the Forge folder:

```bash
cd ~/Downloads/forge
./prep-repo.sh
git add -A
git commit -m "what changed"
git push
```

`prep-repo.sh` refreshes `.gitignore` and prints what will and won't be
uploaded. Worth glancing at — it's what keeps your database, test videos and
virtual environments out of the repository.

`git add -A` includes deletions as well as new and changed files, which
matters when a release removes something.

## Then watch the build

GitHub rebuilds both images automatically. Check the **Actions** tab; it takes
about ten minutes. When it's green, on the NAS:

```bash
docker compose pull && docker compose up -d
```

Or in Dockhand, redeploy the stack. `pull_policy: always` means it fetches
the new image rather than reusing the cached one.

## If git refuses to push

Someone or something changed the repository on GitHub since you last pulled:

```bash
git pull --rebase
git push
```

## If it asks for a password

GitHub wants a personal access token, not your account password. Settings >
Developer settings > Personal access tokens > Tokens (classic), with the
`repo` scope. Paste the token where it asks for a password.

To stop being asked every time:

```bash
git config --global credential.helper osxkeychain    # macOS
git config --global credential.helper store          # Linux
```

## Pinning a version

`latest` moves with every push. To be able to go back:

```bash
git tag v1.1.0
git push --tags
```

That publishes `:v1.1.0`, `:1.1` and `:1` as well. Change the image line in
your stack to `ghcr.io/artech7/forge:v1.1.0` and it stays there until you
change it.

## Checking before you push

```bash
python3 check-server.py
node check-ui.js
```

Both run on GitHub too, so a broken commit shows as a failed check rather
than a broken container.
