# Securing the camp server

A checklist for the production setup in `docker-compose.yml`. Server commands
are run in the directory with `docker-compose.yml` and `.env`.

## 1. Run the checks

```bash
docker compose exec web ./manage.py check --deploy
```

Besides Django's own checks it runs the camp server's (`czeciecamp.*`). Fix
every **ERROR**; read every **WARNING**.

| Check | What it means |
|---|---|
| `czeciecamp.E001` | `USE_UNSAFE_EXEC = True`: participants' programs run without the sio2jail sandbox. The worker container has all capabilities, so a submitted program can take over the worker and its host. **Never run a contest like this.** |
| `czeciecamp.E002` | `SECRET_KEY` is a value published in the repository: anyone can forge sessions and password-reset links. |
| `czeciecamp.W001` | Problem packages run their makefiles unsandboxed on the server, and apps that let non-staff upload packages are enabled. |
| `czeciecamp.W002` | `ALLOWED_HOSTS` accepts any host name. |
| `security.W018` | `DEBUG = True`: error pages show settings and code to anyone. |
| `security.W004`, `W008`, `W012`, `W016` | The site is not served over HTTPS only (section 3). |

## 2. Settings to verify

In `$SIO_DATA_DIR/settings.py` (it was generated from a template meant for
development, so check these even if the site works):

```python
DEBUG = False
USE_UNSAFE_EXEC = False          # participants' code in sio2jail
ALLOWED_HOSTS = ['sio.example.org']  # the name(s) the site is served under
SECRET_KEY = '<long random value>'   # python3 -c 'import secrets; print(secrets.token_urlsafe(50))'
CONTEST_ADMINS_CAN_SU = False
```

After `USE_UNSAFE_EXEC = False`, restart (`docker compose restart web worker`)
and send a test solution: it must be judged normally. If judging fails with a
sio2jail error, fix the worker rather than turning the sandbox off.

`USE_SINOLPACK_MAKEFILES = True` (the default) runs the makefiles of uploaded
problem packages on the server without a sandbox. That is fine while only
trusted staff upload packages; set it to `False` if teachers or user contests
are enabled.

Changing `SECRET_KEY` logs everybody out; do it before the camp.

## 3. Serve the site over HTTPS

`web` speaks plain HTTP on `SIO_PORT`. Passwords and session cookies must not
cross a network, even the camp's LAN, in the clear. Put a TLS reverse proxy in
front, for example Caddy (it gets certificates by itself):

```
sio.example.org {
    reverse_proxy 127.0.0.1:8888
}
```

Then publish `web` on the loopback interface only: in `docker-compose.yml`
change the port line to `"127.0.0.1:${SIO_PORT:-8888}:8000"`, and add to
`settings.py`:

```python
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = ['https://sio.example.org']
SECURE_HSTS_SECONDS = 31536000
```

Only set these once the site really is reachable over HTTPS, or logins stop
working.

## 4. Keep the judging ports private

`sioworkersd` (port 7888) and `filetracker` (port 9999) have **no
authentication**. Anyone who can reach them can read all tests, packages and
submissions, and change results. They are published on `SIO_WORKERS_BIND`
(default `127.0.0.1`, i.e. not reachable from other machines).

If workers run on other machines:

- Set `SIO_WORKERS_BIND` to an address on a network that **only the worker
  machines** use — never the network participants' computers are on.
- Docker bypasses `ufw`; restrict the ports in the `DOCKER-USER` chain
  instead, allowing only the workers' addresses (here 10.0.0.11 and 10.0.0.12):

  ```bash
  sudo iptables -I DOCKER-USER -p tcp -m multiport --dports 7888,9999 -j DROP
  ```

  ```bash
  sudo iptables -I DOCKER-USER -p tcp -m multiport --dports 7888,9999 -s 10.0.0.11 -j ACCEPT
  ```

  ```bash
  sudo iptables -I DOCKER-USER -p tcp -m multiport --dports 7888,9999 -s 10.0.0.12 -j ACCEPT
  ```

  Make them permanent with `iptables-persistent` (or your distribution's
  equivalent). A WireGuard tunnel between the server and the workers is an
  alternative.
- Check from a participant's computer that both ports are closed:
  `nc -zv <server> 7888` must fail.

The database and RabbitMQ are not published at all; keep it that way.

## 5. Accounts

- Give staff their own accounts; do not share the superuser.
- Staff with admin rights should turn on two-factor login: **My account →
  Two-factor authentication** (`/account/two_factor/`).
- Basic admins see less than full contest admins (e.g. not the LLM detection
  panel); give full contest admin only to those who need it.

## 6. Updates

- Rebuild after updating the code: `docker compose build web` and
  `docker compose up -d` (and migrate, see the update notes of each change).
- The `czecie-camp` branch from November 2026 pins dependencies with 286 known
  vulnerabilities (Django, Pillow, aiohttp, cryptography, urllib3, ...). The
  `upstream-sync` and `dependency-updates` branches fix all but `twisted`
  (fixed in 26.10.0, allowed by the lock's 7-day rule from 6 November 2026).
- To check the locked Python dependencies yourself (on any machine with the
  repository):

  ```bash
  uvx pip-audit -r <(uv export --frozen --no-hashes --no-emit-project | grep -v '@')
  ```

  and the JavaScript ones:

  ```bash
  pnpm audit --prod
  ```

## 7. Backups and data

- Back up the database and the filetracker directory regularly:

  ```bash
  docker compose exec -T db pg_dump -U oioioi oioioi > ~/oioioi-$(date +%F).sql
  ```

  and `$SIO_DATA_DIR/filetracker` (tests, packages, submissions).
- Keep backups and `.env` readable only by the admins (`chmod 600`).
- After a camp, delete data you no longer need, e.g. the LLM detection data
  (`./manage.py llmdetect_purge <contest_id>`, if that module is enabled).
