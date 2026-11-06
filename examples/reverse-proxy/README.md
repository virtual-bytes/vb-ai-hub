# Reverse proxy and single sign-on

The hub speaks plain HTTP. Put it behind a reverse proxy for TLS, because transcripts can hold sensitive text.

Whatever proxy you use, turn buffering off for the hub, so live updates reach the page, and allow large uploads.

## Single sign-on

The hub trusts a signed-in user from the proxy only when two things are true: `HUB_PROXY_SECRET` is set on the hub, and the proxy sends the same value in `X-Hub-Proxy-Secret`, with the user's name in `X-Authentik-Username`. Single sign-on users act as admins.

`nginx-authentik.conf` does this with Authentik's forward auth, and works in Nginx Proxy Manager's Advanced tab.

When you use it:

- Don't publish port 8787. Remove `ports` from `docker-compose.yml` and put the hub on the proxy's Docker network, so nothing can skip sign-in or forge the proxy's headers.
- Set `HUB_PROXY_SECRET` in `.env`, and pass it to the container in `docker-compose.yml`.
- Requests with `Authorization: Bearer` or `?token=` and `/healthz` skip Authentik, and the hub checks the token itself.
- If Authentik is down, remove the proxy's Advanced block and sign in with the token under Settings.
