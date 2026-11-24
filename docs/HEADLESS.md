# Headless server

Python 3.11+, on a low-privilege OS account. Qt and MoleditPy are not needed.

```sh
python -m pip install moleditpy-job-server
moleditpy-job-server init --tls --admin admin
moleditpy-job-server serve
```

`init` writes the whole first configuration, so the first `serve` needs no restart.
With `--tls` it uses this PC's private address when there is only one, creates the
local CA and prints its SHA-256 fingerprint for the client PCs. `--admin` asks for the
password; it is never passed on the command line. Without `--tls` or `--lan-http` the
server is reachable from this PC only.

| Command | Purpose |
|---|---|
| `init` | Write a configuration, with this PC's size as the budget |
| `create-admin` | Create the first administrator, if `init --admin` did not |
| `serve` | Run the server in the foreground |
| `start` / `stop` / `restart` | Run the server in the background, stop it, or both; running jobs continue |
| `status` | Whether the server runs, its addresses, and (signed in) its queue; `--json` for scripts |
| `logs [daemon\|startup\|gui]` | List the server's logs, or print one (`-n LINES`, `-f` to follow) |
| `drain` | Pause the queue, wait until no job runs, then stop: for updating the server safely |
| `gui` | Open the desktop window without MoleditPy (needs the `gui` extra) |
| `reset-password <username>` | Recover a forgotten password on the server PC |

| `init` option | Purpose |
|---|---|
| `--tls` | Serve the LAN address over local HTTPS and create the CA now |
| `--lan-http` | Also serve the LAN address over plain HTTP (unencrypted) |
| `--lan-address IP` | The private address other PCs use; needed when this PC has several |
| `--extra-address IP` | Another private address to listen on (repeatable) |
| `--hostname NAME.local` | This PC's `.local` name |
| `--port`, `--tls-port` | HTTP port (default 8766) and HTTPS port (default 8767) |
| `--cpus`, `--memory-mb` | Resource budget (default: this PC's size) |
| `--data-dir PATH` | Data folder (default `~/.moleditpy/job_server/`) |
| `--description TEXT` | A note about this PC, shown with the server status |
| `--admin USERNAME` | Also create the first administrator |

## Administering from the command line

Everything the window's Queue and Account menus do also works from a terminal on the
server PC. Sign in once; the command line registers as a device of its own and stays
signed in until `logout`:

```sh
moleditpy-job-server login admin          # asks for the password
moleditpy-job-server queue                # running and waiting jobs
moleditpy-job-server jobs --all           # every user's jobs
moleditpy-job-server jobs cancel 3f2a     # a job ID, or its first characters
moleditpy-job-server drain && moleditpy-job-server start
```

| Command | Purpose |
|---|---|
| `login [USERNAME]` / `logout` / `whoami` | Register this command line and sign in; revoke it and sign out; show the account |
| `queue [show\|pause\|resume]` | Show the queue, or keep waiting jobs from starting and let them start again |
| `jobs [list] [--all] [--state S]` | Your jobs, or everyone's (administrator) |
| `jobs show\|log\|cancel\|hold\|release JOB` | One job's details or log (`-f` to follow), cancel it, hold or release it |
| `jobs priority\|resources\|owner\|force-run\|mark-lost JOB ...` | The administrator's job controls, as in the window |
| `jobs move-finished` | Queue finished jobs for the finished-jobs folder |
| `users [list\|add\|disable\|enable\|delete\|role\|password]` | Manage accounts (administrator); passwords are asked for |
| `devices [list\|revoke ID]` | Registered devices, and revoking one (administrator) |
| `events [-n N]` | The newest entries of the activity log (administrator) |

Actions that cannot be undone ask first; `-y` answers for scripts. List commands take
`--json`. Text other users chose — job names, logs — is printed with terminal control
characters replaced. `start`, `stop`, `restart`, `status` and `logs` need no sign-in, with
the same authority over this PC's server as `reset-password`; signed in, `stop` shuts down
through the API and the activity log records who did. On Windows, stopping without
signing in needs `--force`, because ending the process skips the clean shutdown.
`drain` leaves the queue paused; resume it with `queue resume` after the next `start`.
A server run by systemd ([deploy/job-server.service](../deploy/job-server.service)) is
better started and stopped with `systemctl --user`.

Every command accepts `--config /path/to/settings.json`. Data lives in
`~/.moleditpy/job_server/`. Install `moleditpy-job-server[gui]` for the desktop window.
The window also opens without MoleditPy — `python -m job_server gui`, or
`launch_job_server.bat` on Windows — and a second launch brings the running
window to the front instead of opening another.

With MoleditPy 5.3 or later, the plugin installed through the Plugin Installer
runs the same commands from its plugin folder, with no separate install:
`moleditpy -r job_server gui` opens the window, `moleditpy -r job_server serve`
runs the server, and so on. Hints the commands print then name that form too.

Status answers (`status`, the web monitor, the API) name the PC they came
from: its hostname and a fixed line — "<PC>'s job server running on <OS>".
The name is answered before sign-in, so a client can tell which PC it
reached. The owner's own description of the PC, set in the window's
Settings tab or with `init --description`, rides in `/health` too, so a
person or an AI agent meeting the server for the first time knows what
the PC is for; the web monitor shows it on the sign-in screen. `/health`
is answered before sign-in, so anyone who can reach the server can read
the note — write it as a note, not a secret. The other status answers do
not carry it.
