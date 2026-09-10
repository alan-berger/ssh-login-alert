# ssh-login-alert — journald collector

[![Python](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![systemd](https://img.shields.io/badge/requires-systemd-important?logo=systemd&logoColor=white)](https://systemd.io/)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey?logo=linux&logoColor=white)](#)
[![Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)](#)
[![License](https://img.shields.io/badge/license-MIT-blue)](../../LICENSE)

Alerts by email on **every successful SSH authentication**, using the systemd
journal as the source of truth. Covers interactive logins, `scp`, `sftp`,
remote command execution, port-forward-only sessions and accounts with a
`nologin` shell.

Requires root and systemd. If you do not have those, use the
[`profile` collector](../profile/) instead — see the
[comparison table](../../README.md).

---

## How it works

sshd logs one line per successful authentication, at default `LogLevel INFO`:

```
Accepted keyboard-interactive/pam for alice from 203.0.113.10 port 43710 ssh2
```

A systemd service follows the journal, matches those lines, and emails an
alert. Because it reads the journal rather than hooking the login path, a
broken or stopped alerter can never delay or block a login. That is the main
reason to prefer this over a `pam_exec` hook in `/etc/pam.d/sshd`, which runs
synchronously inside authentication, misses connections that never open a
session channel, and cannot see the auth method or key fingerprint anyway.

### Multi-factor chains

With `AuthenticationMethods publickey,keyboard-interactive` and
`LogLevel VERBOSE`, sshd emits a `Partial` line carrying the key fingerprint
before the final `Accepted` line. Both share the same sshd PID, so the
collector correlates them and reports the full chain.

### Example alert

```
Successful SSH authentication on web01.

User        : alice
Source      : 203.0.113.10 port 43710 (host10.example.net)
Method chain: publickey -> keyboard-interactive/pam
Key         : ED25519 SHA256:852En2PPftuS5swhvqPZz2ShvRvgkZjv7OleCjdiVFw
Known as    : laptop ed25519
Time        : 2026-09-10 04:38:11 +0100
sshd PID    : 1208849
```

### State and delivery

State is a **journal cursor**, not a line count. Cursors survive log rotation,
journal vacuuming and service restarts, giving at-least-once delivery with no
gaps and no duplicates. On first run with no cursor the collector starts from
"now", so existing history does not trigger a flood.

Sends that fail are spooled to disk as JSON and retried every 60 seconds, so a
mail outage delays alerts rather than losing them.

---

## Requirements

- Linux with systemd and a persistent journal
- Python 3.8+ (standard library only, no pip packages)
- root, to install the service
- An SMTP account, or local `sendmail`

---

## Installation

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sshalert
sudo install -m 0755 ssh_login_alert.py /usr/local/bin/
sudo install -m 0644 ssh-login-alert.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo mkdir -p -m 0700 /etc/ssh-login-alert
sudo install -m 0600 -o sshalert -g sshalert \
    config.ini.example /etc/ssh-login-alert/config.ini
sudo nano /etc/ssh-login-alert/config.ini

sudo systemctl enable --now ssh-login-alert
journalctl -u ssh-login-alert -f
```

The service **must** log this on startup:

```
INFO self-test OK: reading journal for sshd, sshd-session, sshd-auth
```

If it logs `SELF-TEST FAILED`, fix that before going further — see
[Troubleshooting](#troubleshooting). Confirm the service user can read the
journal:

```bash
id sshalert                                # expect systemd-journal
sudo -u sshalert journalctl -t sshd -n 5   # expect entries
```

### Recommended sshd setting

```
LogLevel VERBOSE
```

Not required. Without it you still get an alert per successful
authentication, but you lose the `Partial` line, and with it the key
fingerprint and the visible multi-factor chain. `LogLevel VERBOSE` is also
what fail2ban's documentation recommends.

### Testing coverage

The point of this collector is the cases the `profile` one misses. Test them:

```bash
scp somefile user@host:/tmp/            # expect an alert
ssh -N -L 9999:localhost:80 user@host   # expect an alert
ssh user@host true                      # expect an alert
```

Also confirm a *failed* authentication produces **no** alert — this tool
reports success only, by design.

---

## Configuration

`/etc/ssh-login-alert/config.ini`, mode `0600`, owned by the service user.

```ini
[alert]
to          = you@example.com
from        = ssh-alerts@example.com
method      = smtp          ; or "sendmail"
resolve_ptr = true          ; reverse-DNS the source address

; Syslog identifiers to follow. Leave unset for the default of
; sshd, sshd-session, sshd-auth.
; journal_identifiers = sshd, sshd-session, sshd-auth

[smtp]
host     = mail.example.com
port     = 587
user     = ssh-alerts@example.com
password = CHANGEME
ssl      = false            ; true for implicit TLS (port 465)
starttls = true             ; true for STARTTLS (port 587)

[filter]
ignore_users =
ignore_ips   = 127.0.0.1/32, ::1/128

[keys]
SHA256:YOUR_FINGERPRINT_HERE = laptop ed25519
```

`ssl` and `starttls` select the TLS *model*, not whether TLS is used, and are
mutually exclusive. Port 465 is implicit TLS (`ssl = true`); port 587 is
STARTTLS (`starttls = true`). Setting `ssl = true` against a STARTTLS port
hangs until timeout.

`[keys]` maps public key fingerprints to friendly names so alerts identify
which key was used. Get one with `ssh-keygen -lf ~/.ssh/id_ed25519.pub`.
Fingerprints are case sensitive.

`[filter]` suppresses alerts you have decided are noise. Leaving it empty —
alerting on everything — is the safe default.

**The config is read once at startup.** After editing it, restart the service.

---

## Files

| Path | Purpose |
|---|---|
| `/usr/local/bin/ssh_login_alert.py` | The collector |
| `/etc/systemd/system/ssh-login-alert.service` | Unit file |
| `/etc/ssh-login-alert/config.ini` | Configuration, including SMTP password |
| `/var/lib/ssh-login-alert/cursor` | Journal position |
| `/var/lib/ssh-login-alert/spool/` | Alerts awaiting retry after a failed send |

Operational logging goes to the journal: `journalctl -u ssh-login-alert`.

---

## Troubleshooting

### `SELF-TEST FAILED: no journal entries for identifiers ...`

Two causes.

**The service user cannot read the journal.** `SupplementaryGroups=` in the
unit sometimes does not apply if the user already existed:

```bash
sudo usermod -aG systemd-journal sshalert
sudo systemctl restart ssh-login-alert
id sshalert
```

**sshd logs under a different identifier.** OpenSSH 9.8 split the daemon: the
listener remains `sshd`, but each connection is handled by `sshd-session`,
which logs under its own syslog identifier. OpenSSH 9.9+ adds `sshd-auth`.
Any current Debian, Ubuntu or Fedora release is affected.

This collector follows `sshd`, `sshd-session` and `sshd-auth` by default,
covering every version. If your build uses something else, set it explicitly:

```ini
[alert]
journal_identifiers = sshd, sshd-session, sshd-auth, your-identifier
```

Check what your host uses:

```bash
ssh -V
journalctl -t sshd-session -n 10
```

This failure is worth understanding because of how it presents: the service
starts cleanly, reports `active (running)`, and silently never alerts. The
startup self-test exists specifically to make it visible.

### Self-test passes but no alerts arrive

Confirm sshd is logging authentications at all:

```bash
journalctl -t sshd -t sshd-session --since "-30 min" | grep -E '(Partial|Accepted)'
```

Then check whether the alert was built but not delivered:

```bash
sudo ls -l /var/lib/ssh-login-alert/spool/
journalctl -u ssh-login-alert --since "-30 min"
```

Files in the spool mean detection works and delivery is broken.

### `SMTPAuthenticationError: (535, ... authentication failed)`

Credentials. Test exactly what the daemon reads:

```bash
sudo -u sshalert python3 -c "
import configparser, smtplib
c = configparser.ConfigParser(); c.optionxform = str
c.read('/etc/ssh-login-alert/config.ini')
s = smtplib.SMTP(c['smtp']['host'], int(c['smtp']['port']), timeout=20)
s.starttls(); s.login(c['smtp']['user'], c['smtp']['password'])
print(s.noop()); s.quit()"
```

The 535 response usually carries no reason — check your mail server's own
logs. Confirm the account is a real mailbox rather than an alias; aliases have
no password and cannot authenticate.

Never put a password inline in a shell command, or it lands in your shell
history.

### Alerts stop after a mail outage

They should not. Queued alerts are retried every 60 seconds and delivered on
the next successful send. Check the spool and the service journal as above.

### No fingerprint or partial chain in alerts

`LogLevel VERBOSE` is not in effect. Note that a file in
`/etc/ssh/sshd_config.d/` can override the main `sshd_config`. Confirm with a
fresh login:

```bash
journalctl -t sshd -t sshd-session --since "-2 min" | grep Partial
```

---

## Licence

MIT. See [LICENSE](../../LICENSE).
