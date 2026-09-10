# ssh-login-alert — profile collector

[![Python](https://img.shields.io/badge/python-3.6%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Shell](https://img.shields.io/badge/shell-bash-green?logo=gnu-bash&logoColor=white)](#)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20cPanel-lightgrey)](#)
[![Root](https://img.shields.io/badge/root-not%20required-brightgreen)](#)
[![License](https://img.shields.io/badge/license-MIT-blue)](../../LICENSE)

Real-time SSH login alerting by email, triggered from `~/.bash_profile`. No
daemons, no dependencies beyond the Python standard library, no root access
required. Works on shared hosting.

> **If you have root and systemd, use the
> [journald collector](../journald/) instead.** This one reports *session
> start* rather than authentication, so it does not detect `scp`, `sftp`,
> remote command execution, port-forward-only sessions (`ssh -N`), or
> authentications by accounts with a `nologin` shell. See the
> [comparison table](../../README.md) before choosing.

---

## How it works

**1. A one-liner in `~/.bash_profile`** writes a record to `~/.ssh_logins` on
every login, then calls the monitor script:

```bash
_ssh_auth="unknown"; [ -n "$SSH_USER_AUTH" ] && _ssh_auth=$(awk 'NR==1{print $1}' "$SSH_USER_AUTH" 2>/dev/null); [ -z "$_ssh_auth" ] && _ssh_auth="unknown"
echo "$(date '+%Y-%m-%d %H:%M:%S %z') ${SSH_CLIENT%% *} $USER $_ssh_auth" >> ~/.ssh_logins
/usr/bin/python3 ~/bin/ssh_login_monitor.py
```

- `$SSH_CLIENT` is set by sshd and holds the client IP, source port and
  destination port. `${SSH_CLIENT%% *}` strips everything after the first
  space, leaving the IP.
- `$USER` is the authenticated username.
- `$SSH_USER_AUTH` is a path to a temporary file set by OpenSSH when
  `ExposeAuthInfo yes` is configured. It holds the authentication method.
  Where unavailable, the field falls back to `unknown`.

> **The `[ -n "$SSH_USER_AUTH" ]` guard is required.** Passing an empty value
> to `awk` makes it read from stdin, which hangs the login session.

**2. `ssh_login_monitor.py`** compares the current line count of
`~/.ssh_logins` against a stored state file. Any new lines trigger an
individual email alert. Because the script is called by the login event
itself, alerts are immediate rather than waiting for a cron poll.

### Example log entry

```
2026-04-13 16:46:59 +0100 1.2.3.4 alice publickey
```

### Example alert

```
New SSH login detected on <HOST>.

IP address  : 1.2.3.4
Username    : alice
Auth method : publickey
Login time  : 2026-04-13 16:46:59 +0100

If this was not you, take action immediately.
```

---

## Requirements

- Python 3.6+
- Local `sendmail` (present on most Linux and cPanel hosts) or an SMTP account
- A login shell that sources `~/.bash_profile`
- OpenSSH 7.8+ with `ExposeAuthInfo yes` for auth method detection; without
  it the field reports `unknown` and everything else works normally

---

## Installation

### Step 1 — Enable auth info exposure (optional, needs root)

Add to `/etc/ssh/sshd_config`, then restart sshd:

```
ExposeAuthInfo yes
```

Not possible on shared hosting. Skip it there; auth method will show
`unknown`.

### Step 2 — Deploy the script

```bash
mkdir -p ~/bin ~/logs
cp ssh_login_monitor.py ~/bin/
chmod 700 ~/bin/ssh_login_monitor.py
which python3          # adjust the path below if not /usr/bin/python3
```

### Step 3 — Config file

Copy `config.ini.example` to `~/.ssh_login_monitor.conf` and edit it:

```bash
cp config.ini.example ~/.ssh_login_monitor.conf
chmod 600 ~/.ssh_login_monitor.conf
```

On shared hosting, `method = sendmail` is usually simplest — the local MTA
handles delivery with no credentials. Use `method = smtp` if you need control
over the envelope sender, for example to keep SPF aligned. See
[Configuration](#configuration) below.

### Step 4 — Login logger

Add to `~/.bash_profile`:

```bash
_ssh_auth="unknown"; [ -n "$SSH_USER_AUTH" ] && _ssh_auth=$(awk 'NR==1{print $1}' "$SSH_USER_AUTH" 2>/dev/null); [ -z "$_ssh_auth" ] && _ssh_auth="unknown"
echo "$(date '+%Y-%m-%d %H:%M:%S %z') ${SSH_CLIENT%% *} $USER $_ssh_auth" >> ~/.ssh_logins
/usr/bin/python3 ~/bin/ssh_login_monitor.py
```

Log out and back in, then confirm the log is being written:

```bash
cat ~/.ssh_logins
```

### Step 5 — Optional cron fallback

A safety net in case the script fails silently during a login:

```
* * * * * /usr/bin/python3 ~/bin/ssh_login_monitor.py
```

The script is idempotent — with no new logins it exits silently.

---

## First run behaviour

On first run the script seeds `~/.ssh_login_monitor.state` with the current
line count and exits without alerting, so existing history does not trigger a
flood. The next login produces the first real alert.

```bash
cat ~/logs/ssh_login_monitor.log
```

```
[2026-04-13 16:35:27] First run, seeding state with 3 existing entries
```

---

## Configuration

`~/.ssh_login_monitor.conf`, mode `0600`.

**Local sendmail** (recommended on shared hosting):

```ini
[alert]
to     = you@example.com
from   = alerts@yourdomain.com
method = sendmail
```

**SMTP with STARTTLS (port 587):**

```ini
[alert]
to     = you@example.com
from   = alerts@yourdomain.com
method = smtp

[smtp]
host     = mail.yourdomain.com
port     = 587
user     = alerts@yourdomain.com
password = CHANGEME
ssl      = false
starttls = true
```

**SMTP with implicit TLS (port 465):** as above, with `port = 465`,
`ssl = true`, `starttls = false`.

`ssl` and `starttls` select the TLS *model*, not whether TLS is used, and are
mutually exclusive. Setting `ssl = true` against a STARTTLS port hangs until
timeout.

The `[filter]` and `[keys]` sections used by the journald collector do not
apply here.

---

## Files

| File | Purpose |
|---|---|
| `~/.ssh_logins` | Append-only login log written by `~/.bash_profile` |
| `~/bin/ssh_login_monitor.py` | The monitor script |
| `~/.ssh_login_monitor.conf` | Configuration, including SMTP password |
| `~/.ssh_login_monitor.state` | Single integer — line count at last successful alert |
| `~/logs/ssh_login_monitor.log` | Operational log |

---

## Known limitations

Coverage is limited to logins that source `~/.bash_profile`. It reports
session start rather than authentication, so an attacker who authenticates
without requesting a shell is not alerted on.

Anyone with write access to the account can disable it by editing
`~/.bash_profile`.

A failed send rewinds the state file so the next run retries, which can
resend alerts that were already delivered.

`~/.ssh_logins` grows without bound. Rotate it periodically; the script
detects truncation and reseeds rather than re-alerting.

---

## Licence

MIT. See [LICENSE](../../LICENSE).
