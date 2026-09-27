# Deploying ARIA on the dev EC2 (10.224.134.56)

Everything here reflects what is actually running as of 2026-08-12.

## Services

Five systemd units, all enabled at boot, all running as `mohanax25`:

| Unit | What it is |
|---|---|
| `aria-api.service` | uvicorn on `127.0.0.1:8000` |
| `aria-web.service` | Vite on `0.0.0.0:80` (still a dev server -- see Known gaps) |
| `aria-worker@1..5.service` | five worker processes |

    sudo cp deploy/systemd/*.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now aria-api aria-web
    for i in 1 2 3 4 5; do sudo systemctl enable --now aria-worker@$i; done

Logs land in `logs/api.log`, `logs/worker-N.log`, `logs/web8080.log` (named before the move to port 80).

**After pulling new code, restart -- systemd does not notice a code change:**

    sudo systemctl restart aria-api aria-worker@{1,2,3,4,5}

## Why five workers

The worker is single-threaded by design (`worker/main.py`: *"Scale by running more
processes rather than by threading inside one"*), and a report takes 1-3 minutes. With
one worker, a second user's job sat in `queued` -- and a queued job is one another
machine can claim. Five workers means a free worker is essentially always available.

Safe to run in parallel: `db/dialect.py:claim_next_run` claims with
`SELECT ... FOR UPDATE SKIP LOCKED`, so two workers can never take the same run.

Each worker honours `LLM_MAX_CONCURRENCY` (default 8) independently, so five workers can
put up to 40 concurrent requests on the LLM gateway. Lower it in `.env` if the gateway
starts throttling.

## Database guard

`deploy/sql/001_worker_allowlist_guard.sql` stops any machine other than this server from
claiming jobs. Read the header of that file before touching it -- particularly the off
switch and the rebuild footgun.

## Ports

`8000` is bound to localhost only; the browser reaches the API through Vite's `/api`
proxy on `80`, so only one port is exposed. Note that the AWS security group on this
host **drops** 3000, 3001, 5000 and 5173 -- a dev server on those ports is unreachable no
matter how healthy it is. 80 and 8080 are both open at the SG.

The app answers on **80**, as `http://bop.ir-bioprocess-poc.awscloud.abbvienet.com/`. Binding a
privileged port is granted to `aria-web` alone via `AmbientCapabilities=CAP_NET_BIND_SERVICE`
-- there is no nginx, iptables or socat on this host, and raising
`net.ipv4.ip_unprivileged_port_start` would have applied to every user of a shared machine.

A new hostname must be added to `server.allowedHosts` in `vite.config.ts` or Vite answers
`403 Blocked request`. IP addresses are allowed implicitly, which is why the raw IP works and
a fresh DNS record does not.

## Known gaps

- The frontend is a Vite **dev** server. Fine for a pilot, not for a wide rollout;
  replace with `vite build` behind nginx.
- `JWT_SECRET` is unset, so sessions are signed with the repo's well-known dev constant
  (`settings.py`). Set a real one -- it logs everyone out, so do it off-hours.
- `DA_ENV=local` is the only reason cookies work over plain http. Changing it sets
  `cookie_secure=True` and every login breaks until the site is served over https.
- `queue.mark_failed` nulls `claimed_by`, erasing which host failed a run. Keeping it
  would have turned a multi-hour investigation into a one-query answer.
