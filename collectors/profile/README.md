# ssh-login-alert

![Python](https://img.shields.io/badge/python-3.6%2B-blue?logo=python&logoColor=white)
![Shell](https://img.shields.io/badge/shell-bash-green?logo=gnu-bash&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20cPanel-lightgrey)
![License](https://img.shields.io/badge/license-MIT-blue)
![Maintenance](https://img.shields.io/badge/maintained-yes-brightgreen)

Real-time SSH login alerting via email, triggered directly from `~/.bash_profile`. Sends an immediate email on every SSH login, with the connecting IP address, username, authentication method, and timestamp.

No daemons, no dependencies beyond the Python standard library, no root access required. Works on shared hosting.

---

## How it works

Two components work together:

**1. A one-liner in `~/.bash_profile`** writes a record to `~/.ssh_logins` on every login, then immediately calls the monitor script:

```bash
_ssh_auth="unknown"; [ -n "$SSH_USER_AUTH" ] && _ssh_auth=$(awk 'NR==1{print $1}' "$SSH_USER_AUTH" 2>/dev/null); [ -z "$_ssh_auth" ] && _ssh_auth="unknown"
echo "$(date '+%Y-%m-%d %H:%M:%S %z') ${SSH_CLIENT%% *} $USER $_ssh_auth" >> ~/.ssh_logins
/usr/bin/python3 ~/bin/ssh_login_monitor.py
```

- `$SSH_CLIENT` is set by the SSH daemon and contains the client IP, source port, and destination port. `${SSH_CLIENT%% *}` strips everything after the first space, leaving just the IP.
- `$USER` is the authenticated username.
- `$SSH_USER_AUTH` is a path to a temporary file set by OpenSSH when `ExposeAuthInfo yes` is configured in `sshd_config`. It contains the authentication method (e.g. `publickey`). On systems where this is unavailable it will fall back to `unknown`.

**2. `ssh_login_monitor.py`** compares the current line count of `~/.ssh_logins` against a stored state file. Any new lines trigger an individual email alert.

Because the script is called directly by the login event, alerts are sent immediately rather than waiting for a cron poll.

### Example log entry

```
2026-04-13 16:46:59 +0100 1.2.3.4 alice publickey
```

### Example alert email

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
- A mail sending method: either local `sendmail` (available on most Linux/cPanel hosts) or an SMTP server
- SSH access to your server with a shell that sources `~/.bash_profile`
- OpenSSH 7.8+ with `ExposeAuthInfo yes` in `sshd_config` for authentication method detection (older versions or systems without this setting will log `unknown`)

---

## Installation

### Step 1, Enable auth info exposure (servers where you have root)

To populate the authentication method field, add the following to `/etc/ssh/sshd_config`:

```
ExposeAuthInfo yes
```

Then restart sshd:

```bash
systemctl restart sshd
```

> **Note:** This step requires root access and is not possible on shared hosting. On shared hosting, or any system where `ExposeAuthInfo` cannot be set, auth method will always show `unknown`. Everything else functions normally.

### Step 2, Login logger

Add these lines to `~/.bash_profile`:

```bash
_ssh_auth=$(awk 'NR==1{print $1}' "$SSH_USER_AUTH" 2>/dev/null); [ -z "$_ssh_auth" ] && _ssh_auth="unknown"
echo "$(date '+%Y-%m-%d %H:%M:%S %z') ${SSH_CLIENT%% *} $USER $_ssh_auth" >> ~/.ssh_logins
/usr/bin/python3 ~/bin/ssh_login_monitor.py
```

Log out and back in, then verify the log file is being written:

```bash
cat ~/.ssh_logins
```

You should see a line like:

```
2026-04-13 16:46:59 +0100 1.2.3.4 alice publickey
```

### Step 3, Config file

Create `~/.ssh_login_monitor.conf`. Choose either `sendmail` or `smtp` as your delivery method.

**Using local sendmail (recommended for shared hosting):**

```ini
[alert]
to     = you@example.com
from   = alerts@yourdomain.com
method = sendmail
```

**Using SMTP with STARTTLS (port 587):**

```ini
[alert]
to     = you@example.com
from   = alerts@yourdomain.com
method = smtp

[smtp]
host     = mail.yourdomain.com
port     = 587
user     = alerts@yourdomain.com
password = YOUR_PASSWORD_HERE
starttls = yes
ssl      = no
```

**Using SMTP with implicit SSL (port 465):**

```ini
[alert]
to     = you@example.com
from   = alerts@yourdomain.com
method = smtp

[smtp]
host     = mail.yourdomain.com
port     = 465
user     = alerts@yourdomain.com
password = YOUR_PASSWORD_HERE
ssl      = yes
starttls = no
```

Secure the config file:

```bash
chmod 600 ~/.ssh_login_monitor.conf
```

### Step 4, Deploy the script

```bash
mkdir -p ~/bin ~/logs
cp ssh_login_monitor.py ~/bin/
chmod 700 ~/bin/ssh_login_monitor.py
```

Check the correct path to Python on your system:

```bash
which python3
```

Update the path in `~/.bash_profile` if it differs from `/usr/bin/python3`.

### Step 5, Optional cron fallback

Add a cron job as a safety net in case the script fails silently during a login:

```
* * * * * /usr/bin/python3 ~/bin/ssh_login_monitor.py
```

The script is idempotent — if no new logins are detected it exits silently.

---

## First run behaviour

On first run the script seeds `~/.ssh_login_monitor.state` with the current line count of `~/.ssh_logins` and exits without sending any alerts. This prevents a flood of alerts for existing login history. The next login after deployment will trigger the first real alert.

Check the log to confirm seeding worked:

```bash
cat ~/logs/ssh_login_monitor.log
```

Expected output:

```
[2026-04-13 16:35:27] First run, seeding state with 3 existing entries
```

---

## Files

| File | Purpose |
|---|---|
| `~/.ssh_logins` | Append-only login log written by `~/.bash_profile` |
| `~/.ssh_login_monitor.conf` | Configuration (delivery method, addresses, SMTP credentials) |
| `~/.ssh_login_monitor.state` | Single integer, line count at last successful alert |
| `~/logs/ssh_login_monitor.log` | Operational log for debugging |

---

## Script logic walkthrough

### Imports and paths

```python
HOME        = Path.home()
CONFIG_FILE = HOME / ".ssh_login_monitor.conf"
STATE_FILE  = HOME / ".ssh_login_monitor.state"
LOGINLOG    = HOME / ".ssh_logins"
LOG_FILE    = HOME / "logs" / "ssh_login_monitor.log"
```

All file paths are anchored to the home directory via `Path.home()`, so no username is hardcoded. The `/` operator on `Path` objects is a clean way to build paths without string concatenation.

---

### log()

```python
def log(msg: str):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")
```

Simple append-only logger. Creates `~/logs/` if it doesn't exist yet, stamps every message with a timestamp, and appends it to the log file. Used throughout the script for both successful actions and errors.

---

### load_config()

```python
def load_config() -> configparser.ConfigParser:
    if not CONFIG_FILE.exists():
        log(f"ERROR: Config file not found: {CONFIG_FILE}")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg
```

Reads the `.conf` file into a `ConfigParser` object, which allows values to be accessed with `cfg["section"]["key"]` syntax. Exits immediately with a logged error if the config is missing, rather than crashing with an unhelpful traceback later.

---

### send_email()

```python
def send_email(cfg, subject, body):
```

Builds a proper MIME email message with correctly formatted headers including `Date` and `Message-ID` (derived from the configured `from` address), then branches on the `method` setting in the config:

- **sendmail**: pipes the formatted message directly to `/usr/sbin/sendmail` via stdin. The local mail system handles delivery without authentication. Ideal for shared hosting or any server with a local MTA.
- **smtp with STARTTLS**: opens a plain connection to the SMTP server then upgrades to TLS via `STARTTLS`. Use with port 587.
- **smtp with implicit SSL**: opens a TLS-wrapped connection immediately using `smtplib.SMTP_SSL`. Use with port 465.

---

### get_stored_count() / store_count()

```python
def get_stored_count() -> int:
    try:
        return int(STATE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return -1

def store_count(count: int):
    STATE_FILE.write_text(str(count))
```

The state file contains a single integer, the number of lines in `~/.ssh_logins` at the time of the last successfully sent alert. `-1` is the sentinel value for "state file does not exist yet" (first run). This number is what allows the script to know which lines are new on each execution.

---

### parse_login_line()

```python
def parse_login_line(line: str) -> dict:
    parts = line.strip().split()
    return {
        "timestamp":   f"{parts[0]} {parts[1]} {parts[2]}",
        "ip":          parts[3],
        "username":    parts[4] if len(parts) > 4 else "unknown",
        "auth_method": parts[5] if len(parts) > 5 else "unknown",
    }
```

Splits a line like `2026-04-13 16:46:59 +0100 1.2.3.4 alice publickey` into its components by position. Both `username` and `auth_method` default to `unknown` defensively if the fields are absent — for example in log entries written before this feature was added, or on systems where `ExposeAuthInfo` is unavailable.

---

### main(), the core logic

```python
lines         = [l.strip() for l in LOGINLOG.read_text().splitlines() if l.strip()]
current_count = len(lines)
stored_count  = get_stored_count()
```

Reads the entire `~/.ssh_logins` file and counts the non-empty lines. Compares against the stored count to determine if anything is new.

```python
if stored_count == -1:
    store_count(current_count)
    log(f"First run, seeding state with {current_count} existing entries")
    return
```

First run only — writes the current line count to the state file and exits without sending any alerts. This prevents alerting on historical logins that predate deployment.

```python
if current_count <= stored_count:
    return
```

Nothing new, exit silently. This is the path taken on the vast majority of executions.

```python
new_lines = lines[stored_count:]
```

Slices the list from the last known position to the end. If the stored count was 5 and there are now 7 lines, `new_lines` contains lines at index 5 and 6 (the 6th and 7th entries). This correctly handles multiple simultaneous logins.

```python
for line in new_lines:
    try:
        send_email(cfg, subject, body)
        log(...)
    except Exception as e:
        log(...)
        errors.append(line)
```

Iterates over every new line and sends an individual email for each one, collecting any failures.

```python
if not errors:
    store_count(current_count)
else:
    failed_index = lines.index(errors[0])
    store_count(failed_index)
```

State only advances as far as the last successfully delivered alert. If sending fails partway through a batch, the next execution retries from the failure point rather than silently skipping undelivered alerts.

---

## License

MIT
