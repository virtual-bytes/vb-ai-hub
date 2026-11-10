# Reverse proxy and single sign-on

The hub speaks plain HTTP. Put it behind a reverse proxy for TLS, because transcripts can hold sensitive text.

Whatever proxy you use, turn buffering off for the hub, so live updates reach the page, and allow large uploads.

Behind a proxy, every visitor seems to come from the proxy's address. The hub uses visitors' addresses to slow down repeated wrong passwords, so tell it which proxies to believe: set `HUB_TRUSTED_PROXIES` to the proxy's address or range (for example `172.18.0.0/16` for a Docker network). A proxy that sends `HUB_PROXY_SECRET` is believed without it. The hub reads the last address in `X-Forwarded-For` that isn't a trusted proxy, because a visitor can put anything at the start of that header.

## Single sign-on

The hub trusts a signed-in user from the proxy only when two things are true: `HUB_PROXY_SECRET` is set on the hub, and the proxy sends the same value in `X-Hub-Proxy-Secret`, with the user's name in `X-Authentik-Username`. Single sign-on users act as admins.

`nginx-authentik.conf` does this with Authentik's forward auth, and works in Nginx Proxy Manager's Advanced tab.

When you use it:

- Don't publish port 8787. Remove `ports` from `docker-compose.yml` and put the hub on the proxy's Docker network, so nothing can skip sign-in or forge the proxy's headers.
- Set `HUB_PROXY_SECRET` in `.env`, and pass it to the container in `docker-compose.yml`.
- Requests with `Authorization: Bearer` and `/healthz` skip Authentik, and the hub checks the token itself. A request with a wrong token is refused, even if it also carries a hub session cookie.
- If Authentik is down, remove the proxy's Advanced block and sign in with the token under Settings.
