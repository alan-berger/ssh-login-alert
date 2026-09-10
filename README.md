# ssh-login-alert

[![Python](https://img.shields.io/badge/python-3.6%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20cPanel-lightgrey)](#)
[![Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)](#)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Maintenance](https://img.shields.io/badge/maintained-yes-brightgreen)](#)

Email alerts on successful SSH authentication.

Two collectors are provided. They solve the same problem under very different
constraints, and they are **not** equivalent — pick deliberately.

| | [`profile`](collectors/profile/) | [`journald`](collectors/journald/) |
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
| Survives log rotation | n/a | yes |
| Requires root | no | yes |
| Requires systemd + journald | no | yes |
| Python | 3.6+ | 3.8+ |

## Which one?

**If you have root and systemd, use [`journald`](collectors/journald/).** It
alerts on authentication itself, so it catches every successful SSH auth
regardless of what the session does afterwards, and it runs out of band — a
broken or stopped alerter can never delay or block a login.

**Use [`profile`](collectors/profile/) only where you cannot install a
service** — shared hosting, cPanel, or any account without privileged access.
It reports *session start* rather than authentication, so it misses everything
in the "no" column above. It is the right answer when it is the only answer.

Do not run both on the same host, or interactive logins will alert twice.

---

## Security considerations

These apply to both collectors.

**This is detection, not prevention.** An attacker with root on the monitored
host can stop the service, read the SMTP credential from the config, or send
you a reassuring email themselves. Nothing in a host-resident monitor survives
compromise of that host. The `profile` collector is weaker still: anyone with
write access to the account can disable it by editing `~/.bash_profile`.

**The config file contains a password in plaintext.** Keep it `0600` and owned
by the account that reads it. Use a dedicated submission account with no
access to anything else.

**Use a separate credential per monitored host.** Revoking one then does not
silence the others, and the `sasl_username` in your mail server logs
identifies which host is authenticating.

**Add a heartbeat.** A monitor that has died is indistinguishable from a
monitor with nothing to report. Silence should be alarming, not reassuring.
Point the service at a dead-man's-switch — healthchecks.io, Uptime Kuma, or
similar — so you find out when it stops.

**Beware the circular case.** Alerting about logins to your *mail server*, via
that same mail server, fails on both sides simultaneously. Use a second,
independent channel there.

To report a vulnerability in this project, see [SECURITY.md](SECURITY.md).

---

## Repository layout

```
collectors/
├── journald/          systemd service, journal-sourced (preferred)
│   ├── README.md
│   ├── ssh_login_alert.py
│   ├── ssh-login-alert.service
│   └── config.ini.example
└── profile/           unprivileged, ~/.bash_profile (shared hosting)
    ├── README.md
    ├── ssh_login_monitor.py
    └── config.ini.example
```

The original flat layout, before the `journald` collector was added, is tagged
[v1.0.0](../../releases/tag/v1.0.0).

## Licence

MIT. See [LICENSE](LICENSE).
