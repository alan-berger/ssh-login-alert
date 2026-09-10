# ssh-login-alert

Email alerts on successful SSH authentication.

Two collectors are provided. They solve the same problem in environments with
very different constraints, and they are **not** equivalent — pick
deliberately.

| | `profile` | `journald` |
|---|---|---|
| Interactive shell login | yes | yes |
| `scp` / `sftp` | no | yes |
| Remote command (`ssh host cmd`) | no | yes |
| Port-forward only (`ssh -N`) | no | yes |
| Accounts with `nologin` shell | no | yes |
| Auth method reported | partial | yes |
| Public key fingerprint | no | yes |
| Multi-factor chain visible | no | yes |
| Retries failed sends | no | yes |
| Requires root | no | yes |
| Requires systemd + journald | no | yes |
| Python | 3.6+ | 3.8+ |

**If you have root and systemd, use `journald`.** It alerts on
authentication itself, so it catches every successful SSH auth regardless of
what the session does afterwards.

**Use `profile` only where you cannot install a service** — shared hosting,
cPanel, or any account without privileged access. Understand that it reports
*session start*, not authentication, and misses everything in the "no" column
above.

---

## journald collector

### How it works

sshd logs one `Accepted <method> for <user> from <ip> port <n> ssh2` line per
successful authentication, at default `LogLevel INFO`. A systemd service
follows the journal, matches those lines, and emails an alert.

Because it reads the journal rather than hooking the login path, a broken or
stopped alerter can never delay or block a login. This is the main reason to
prefer it over a `pam_exec` hook in `/etc/pam.d/sshd`, which runs
synchronously inside authentication and cannot see the auth method or key
fingerprint anyway.

With `AuthenticationMethods publickey,keyboard-interactive` and
`LogLevel VERBOSE`, sshd emits a `Partial publickey ...` line carrying the key
fingerprint before the final `Accepted` line. Both share the same sshd PID, so
the collector correlates them and reports the full chain:

```
Method chain: publickey -> keyboard-interactive/pam
Key         : ED25519 SHA256:KFXk4gPQVEOJ8e3K...
Known as    : laptop ed25519
```

State is a **journal cursor**, not a line count. Cursors survive log rotation,
journal vacuuming and service restarts, giving at-least-once delivery with no
gaps and no duplicates.

Sends that fail are spooled to disk as JSON and retried every 60 seconds, so a
mail outage delays alerts rather than losing them.

### Requirements

- Linux with systemd and a persistent journal
- Python 3.8+ (standard library only, no pip packages)
- root, to install the service
- An SMTP account, or local `sendmail`

### Install

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sshalert
sudo install -m 0755 collectors/journald/ssh_login_alert.py /usr/local/bin/
sudo install -m 0644 collectors/journald/ssh-login-alert.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo mkdir -p -m 0700 /etc/ssh-login-alert
sudo install -m 0600 -o sshalert -g sshalert \
    collectors/journald/config.ini.example /etc/ssh-login-alert/config.ini
sudo nano /etc/ssh-login-alert/config.ini

sudo systemctl enable --now ssh-login-alert
journalctl -u ssh-login-alert -f
```

The service must log `self-test OK: reading journal for ...` on startup. If it
logs `SELF-TEST FAILED`, fix that before going further — see Troubleshooting.

Verify the `sshalert` user can read the journal:

```bash
id sshalert                                    # expect systemd-journal
sudo -u sshalert journalctl -t sshd -n 5       # expect entries
```

### Recommended sshd settings

```
LogLevel VERBOSE
```

Not required. Without it you still get an alert per successful
authentication, but you lose the `Partial` line, and with it the key
fingerprint and the visible multi-factor chain. `LogLevel VERBOSE` is also
what fail2ban's own documentation recommends.

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

## profile collector

For unprivileged environments. A one-liner in `~/.bash_profile` appends a
record to `~/.ssh_logins` on login; a Python script compares the line count
against a state file and emails any new entries.

See [`collectors/profile/README.md`](collectors/profile/README.md) for setup.
Released as [v1.0.0](../../releases/tag/v1.0.0) if you want the original
flat layout.

---

## Configuration

Both collectors share the same config format.

```ini
[alert]
to          = you@example.com
from        = ssh-alerts@example.com
method      = smtp          ; or "sendmail"
resolve_ptr = true          ; reverse-DNS the source address

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

`ssl` and `starttls` are mutually exclusive and select the TLS model, not
whether TLS is used. Port 465 is implicit TLS (`ssl = true`); port 587 is
STARTTLS (`starttls = true`). Setting `ssl = true` against a STARTTLS port
hangs until timeout.

`[keys]` maps public key fingerprints to friendly names, so alerts identify
which key was used. Get a fingerprint with `ssh-keygen -lf ~/.ssh/id_ed25519.pub`.
Fingerprints are case sensitive.

`[filter]` suppresses alerts you have decided are noise. Leaving it empty —
alerting on everything — is the safe default.

**The config file contains a password in plaintext.** Keep it `0600` and owned
by the service user. Prefer a dedicated submission account with no mailbox
access to anything else, and use a separate account per host so a compromise
of one does not hand over credentials usable from the others.

The config is read **once at startup**. After editing it, restart the service.

---

## Troubleshooting

### Service runs, self-test passes, but no alerts

Confirm sshd is actually logging authentications:

```bash
journalctl -t sshd -t sshd-session --since "-30 min" | grep -E '(Partial|Accepted)'
```

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

The collector follows `sshd`, `sshd-session` and `sshd-auth` by default, which
covers every version. If your build uses something else, set it explicitly:

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

Check the mail server logs for the reason — the 535 response usually carries
none. Confirm the account is a real mailbox rather than an alias; aliases have
no password and cannot authenticate.

Never put a password inline in a shell command. Use `getpass`, or read the
config as above, so nothing lands in shell history.

### Alerts stop arriving after a mail outage

They should not — check the spool:

```bash
sudo ls -l /var/lib/ssh-login-alert/spool/
```

Queued alerts are retried every 60 seconds and delivered on the next
successful send.

### Duplicate or missing alerts

If both collectors are deployed on the same host, remove the `~/.bash_profile`
lines. Otherwise interactive logins alert twice.

---

## Security considerations

This is **detection, not prevention**. An attacker with root on the monitored
host can stop the service, read the SMTP credential from the config, or send
you a reassuring email themselves. Nothing in a host-resident monitor survives
compromise of that host.

Two things reduce the blast radius:

**Per-host credentials.** A dedicated submission account per monitored host
means revoking one does not silence the others, and the `sasl_username` in
your mail logs identifies which host is authenticating.

**A heartbeat.** A monitor that has died is indistinguishable from a monitor
with nothing to report. Silence should be alarming, not reassuring. Point the
service at a dead-man's-switch (healthchecks.io, Uptime Kuma, or similar) so
you find out when it stops.

Be aware of the circular case: alerting about logins to your *mail server*,
via that same mail server, fails on both sides simultaneously. Use a second,
independent channel there.

---

## Licence

MIT. See [LICENSE](LICENSE).
