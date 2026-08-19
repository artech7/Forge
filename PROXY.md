# Running Forge behind a reverse proxy

Forge works behind a proxy without any special configuration on its side.
It builds no absolute URLs, and the interface works out its own address from
the browser, so `https://forge.example.com` behaves exactly like a direct
connection.

Three things do need attention.

## 1. WebSockets have to be passed through

This is the one that catches people. The interface holds a websocket open to
`/ws` and everything live flows through it — progress, throughput, the queue,
node status. Without the upgrade headers the page loads and then simply never
updates, which looks like Forge being broken rather than the proxy.

**nginx**

```nginx
location / {
    proxy_pass http://192.168.1.x:58420;
    proxy_http_version 1.1;

    # These four lines are what makes the live view work.
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

The long timeouts matter: an idle websocket is still an open connection, and
the default 60 seconds would drop it every minute.

**SWAG** — copy `forge.subdomain.conf.sample` from an existing app config and
change the upstream to `192.168.1.x` port `58420`. SWAG's samples already
carry the websocket headers.

**Caddy** handles websockets on its own:

```
forge.example.com {
    reverse_proxy 192.168.1.x:58420
}
```

**Traefik**, as labels on the container:

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.forge.rule=Host(`forge.example.com`)
  - traefik.http.services.forge.loadbalancer.server.port=8420
```

Traefik upgrades websockets automatically.

**Nginx Proxy Manager** — tick **Websockets Support** on the proxy host. It's
off by default.

## 2. Use a subdomain, not a subfolder

`forge.example.com` works. `example.com/forge` does not — the interface asks
for `/api/...` and `/static/...` from the root, and a subfolder mount would
send those to whatever else lives there.

Radarr and Sonarr have a "URL Base" setting for this; Forge doesn't yet. If
you need one, say so and it can be added — it's a small change, just not one
worth making blind.

## 3. Forge has no login

Anyone who reaches the interface can change libraries, delete entries and
clear the Originals folder — which is where your only copy of a source file
lives after a conversion.

That is fine on a private network. It is not fine exposed to the internet
without something in front of it.

If your proxy already runs Authelia, Authentik, or basic auth for the *arrs,
put Forge behind the same rule and there is nothing more to do. If it
doesn't, either keep Forge off the public hostname or ask and a token login
can be added.

Worth checking rather than assuming: open the Forge address from a phone on
mobile data. If it loads without asking for anything, so would anyone else.

## The worker should not go through the proxy

Point the worker at the NAS directly:

```
-Server http://192.168.1.x:58420
```

not at `https://forge.example.com`. The worker uploads whole video files when
it can't reach the share directly, and proxies routinely cap request bodies
at a few megabytes and time out long uploads. Going straight to the NAS
avoids both, and skips TLS on traffic that never leaves your network.

If the worker genuinely has to come in from outside, a VPN is the better
answer. Failing that, on nginx you need:

```nginx
client_max_body_size 0;
proxy_request_buffering off;
proxy_read_timeout 3600s;
```

## Checking it works

Once it's proxied, open the interface and watch a conversion. If the
percentage climbs, websockets are working. If the page loads but nothing ever
moves, the upgrade headers are missing.

The browser console shows it plainly too: a failed upgrade logs a websocket
error against `/ws` immediately on load.
