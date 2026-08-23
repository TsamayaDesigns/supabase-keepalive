# supabase-keepalive

**Stop Supabase from pausing your free-tier projects.**

A single dependency-free Python script that pings every Supabase project you own on a schedule, and automatically un-pauses any that already went to sleep. Give it one token; it handles all your projects.

```
$ python keepalive.py --restore
OK    my-portfolio     abcdefghijklmnopqrst  [ACTIVE_HEALTHY]  -> db says 2026-08-23T10:31:07.912Z
OK    side-project     bcdefghijklmnopqrstu  [ACTIVE_HEALTHY]  -> db says 2026-08-23T10:31:08.455Z
OK    old-hackathon    cdefghijklmnopqrstuv  [INACTIVE]        -> restore requested (takes a few minutes; ping again later)
```

---

## The problem

On the free plan, Supabase **pauses a project after 7 days with no database activity**. When that happens your app's API calls start failing until you log into the dashboard and click *Restore*. Worse, a project that gets paused repeatedly can hit a cap on how many times it can be restored.

Two things people get wrong:

- **Opening your website does not count as activity.** Only actual requests that reach the database (queries, auth calls, etc.) reset the 7-day timer.
- **A cron hitting the project URL with no key does nothing.** The API gateway rejects it before the database ever sees it.

So the fix is: run a real (tiny) query against each project at least once a week, automatically.

## How this tool works

It uses the [Supabase Management API](https://supabase.com/docs/reference/api/introduction), which is authenticated by a single **personal access token** for your whole account:

| Step | Endpoint | Purpose |
|---|---|---|
| 1 | `GET /v1/projects` | List every project + its status (`ACTIVE_HEALTHY`, `INACTIVE`, ...) |
| 2 | `POST /v1/projects/{ref}/database/query` with `select now()` | A genuine read on the project's Postgres — this is what resets the pause timer |
| 3 | `POST /v1/projects/{ref}/restore` | Only with `--restore`, and only for projects already `INACTIVE` |

Because it goes through the Management API, you never have to collect per-project URLs or anon keys — adding a new project to your account means it's covered on the next run.

> The query endpoint is marked **beta** by Supabase. If the tool ever starts failing across all projects at once, that endpoint's shape is the first thing to check.

A second, token-less mode exists for projects you *don't* own (see below).

## Quick start

### 1. Create a personal access token

Dashboard → **Account → Access Tokens** → https://supabase.com/dashboard/account/tokens → *Generate new token*. Copy it (`sbp_...`). Treat it like a password — it can do anything your account can.

### 2. Run it once locally

```powershell
# PowerShell
$env:SUPABASE_ACCESS_TOKEN = "sbp_..."
python keepalive.py --list        # see every project and its status, no side effects
python keepalive.py               # ping each healthy project
python keepalive.py --restore     # ...and un-pause any INACTIVE ones
```

```bash
# bash / zsh
export SUPABASE_ACCESS_TOKEN="sbp_..."
python keepalive.py --restore
```

Requires Python 3.10+. No `pip install` — it's standard library only.

### 3. Schedule it

Pick whichever fits. The important thing is that it fires **at least once every 7 days**. The defaults below run twice a week so a single missed run is harmless.

#### Option A — GitHub Actions (recommended: runs even when your computer is off)

1. Fork this repo (or push your own copy).
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - `SUPABASE_ACCESS_TOKEN` = your token
   - `SUPABASE_TARGETS` = only if you use target mode (see below)
3. **Actions → supabase-keepalive → Run workflow** once to confirm it's green.

The workflow in [`.github/workflows/keepalive.yml`](.github/workflows/keepalive.yml) runs every **Monday and Thursday at 03:17 UTC**. Change the `cron:` line for a different cadence.

Two GitHub gotchas:
- Scheduled workflows are **disabled automatically in repos with no activity for 60 days**. GitHub emails you when that happens; any commit or a manual *Run workflow* re-enables it.
- Scheduled runs can be delayed by minutes (occasionally an hour) under load — irrelevant for a 7-day window.

#### Option B — Windows Task Scheduler (only runs while the PC is on)

```powershell
setx SUPABASE_ACCESS_TOKEN "sbp_..."     # user-scoped env var; open a NEW terminal afterwards
schtasks /Create /TN "supabase-keepalive" /SC DAILY /ST 10:00 /TR "python C:\path\to\keepalive.py --restore" /F
```

Laptop that's often closed? Prefer Option A.

#### Option C — cron (Linux / macOS / a VPS)

```
17 3 * * 1,4  SUPABASE_ACCESS_TOKEN=sbp_... /usr/bin/python3 /path/to/keepalive.py --restore >> /var/log/supabase-keepalive.log 2>&1
```

## Usage reference

```
python keepalive.py [--list] [--restore] [--project X ...] [--target URL,KEY[,TABLE] ...]
```

| Flag | What it does |
|---|---|
| *(none)* | Ping every project the token can see |
| `--list` | Print projects + status and exit. No pings, no restores. |
| `--restore` | For projects in `INACTIVE` (paused) state, request a restore instead of failing |
| `--project X` | Limit to one project. `X` can be the **dashboard link** (`https://supabase.com/dashboard/project/<ref>/...`), the **API URL** (`https://<ref>.supabase.co`), or the bare 20-letter **ref**. Repeatable. |
| `--target URL,KEY[,TABLE]` | Token-less mode, see below. Repeatable, or set `SUPABASE_TARGETS` with `;`-separated entries. |

Exit code is **0** only if every project was pinged (or restore-requested) successfully, so schedulers and CI can alert on failure.

Project statuses you'll see:

| Status | Meaning | What the tool does |
|---|---|---|
| `ACTIVE_HEALTHY` | Running normally | Runs the query |
| `INACTIVE` | **Paused** for inactivity | Fails, or restores with `--restore` |
| `RESTORING`, `COMING_UP`, `PAUSING`, ... | Transitional | Skips it this run (reported as FAIL so you notice; fine on the next run) |

### Target mode (no token / someone else's project)

If you can't use a token — e.g. keeping a friend's project alive, or you only have the project's public keys — give it the API URL and the anon (or service) key directly:

```bash
python keepalive.py --target "https://abcdefghijklmnopqrst.supabase.co,eyJhbGciOi...,profiles"
```

The third field is a **table name**. Include it: a `select ... limit 1` on a real table is a genuine database read. Without a table the tool only hits the PostgREST root, which is served from a schema cache and is weaker evidence of activity. RLS is fine — an empty result still counts; what matters is the request reached Postgres.

Multiple targets: repeat `--target`, or `SUPABASE_TARGETS="url,key,table;url2,key2,table2"`.

Token mode and target mode can be combined in one run.

## Security notes

- The access token has full power over your account. Store it **only** in GitHub Actions secrets / an environment variable, never in the repo. `.env` is git-ignored; `.env.example` is the template.
- The query the tool runs is `select now()` with `read_only: true`. It never writes to your database.
- In target mode, prefer the **anon** key over the service-role key — anon is designed to be public; service-role bypasses RLS.
- A public repo is safe **as long as the token is in secrets** — the workflow reads `${{ secrets.SUPABASE_ACCESS_TOKEN }}`, which GitHub masks in logs.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ERROR: nothing to do` | Neither `SUPABASE_ACCESS_TOKEN` nor `--target` was given. |
| `could not list projects (HTTP 401)` | Token is wrong, revoked, or from a different account. Regenerate it. |
| `HTTP 403` on the query | The token lacks database permission (fine-grained tokens need `database_read`). Create an all-scopes token. |
| `HTTP 429` | Rate-limited. Twice a week will never hit this; if you run it in a tight loop, stop. |
| `paused - rerun with --restore` | The project is already asleep. Run with `--restore`, wait a few minutes, run again. |
| Workflow stopped running | GitHub's 60-day inactivity rule. Trigger it manually once. |
| Target mode `401 Invalid API key` | The key doesn't belong to that project URL. Copy both from the same project's *Settings → API*. |
| Target mode `404` | Table doesn't exist (or isn't in the exposed schema). Use a table you can see in the Table Editor. |

## Why not `pg_cron` / an Edge Function inside the project?

Anything *inside* the project stops the moment the project is paused — it can't keep itself awake. The ping has to come from outside. GitHub Actions is the cheapest always-on "outside" most people already have.

## Files

```
keepalive.py                      the whole tool
.github/workflows/keepalive.yml   Mon/Thu schedule + manual trigger
.env.example                      env var template
```

## License

MIT
