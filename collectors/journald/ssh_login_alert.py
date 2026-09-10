#!/usr/bin/env python3
"""
ssh_login_alert.py

Alerts by email on every successful SSH authentication, using the systemd
journal as the source of truth rather than ~/.bash_profile.

Why the journal:
  - sshd logs "Accepted <method> for <user> from <ip> port <n> ssh2[: <detail>]"
    once per successful authentication, at default LogLevel INFO, regardless of
    what the connection does afterwards. That covers interactive shells, scp,
    sftp, remote command execution, and connections that never open a session
    channel at all (ssh -N port forwarding, nologin tunnel accounts).
  - The Accepted/Partial lines carry the auth method and the public key
    fingerprint, which PAM does not expose to pam_exec.
  - It runs out of band. A broken alerter can never block or slow a login.

State is a journal cursor, not a line count. Cursors survive rotation,
vacuuming and service restarts, and give exact at-least-once semantics.

Sends that fail are spooled to disk and retried by a background thread, so a
mail outage delays alerts rather than losing them.
"""

import configparser
import ipaddress
import json
import logging
import os
import re
import signal
import smtplib
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

# --------------------------------------------------------------------------
# Paths. systemd sets STATE_DIRECTORY and CONFIGURATION_DIRECTORY for us when
# StateDirectory=/ConfigurationDirectory= are in the unit; fall back for
# manual runs.
# --------------------------------------------------------------------------

STATE_DIR   = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/ssh-login-alert").split(":")[0])
CONF_DIR    = Path(os.environ.get("CONFIGURATION_DIRECTORY", "/etc/ssh-login-alert").split(":")[0])
CONFIG_FILE = Path(os.environ.get("SSH_ALERT_CONFIG", str(CONF_DIR / "config.ini")))
CURSOR_FILE = STATE_DIR / "cursor"
SPOOL_DIR   = STATE_DIR / "spool"

# OpenSSH 9.8 split the daemon: the listener stays "sshd", but each connection
# is handled by "sshd-session" (and, from 9.9, privsep auth by "sshd-auth"),
# each logging under its own syslog identifier. Authentication events appear
# under sshd-session on those versions and under sshd on older ones, so follow
# all of them. Override with journal_identifiers in [alert] if needed.
DEFAULT_IDENTIFIERS = ["sshd", "sshd-session", "sshd-auth"]
PARTIAL_TTL        = 300     # seconds to remember a Partial line per sshd PID
CURSOR_FLUSH_SECS  = 5       # rate-limit cursor writes for non-matching lines
RETRY_INTERVAL     = 60      # spool drain interval

# sshd auth.c emits "Accepted", "Partial" (method succeeded, more required) or
# "Failed". We alert on Accepted and use Partial only to enrich the alert.
ACCEPTED_RE = re.compile(
    r"^Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<ip>\S+) "
    r"port (?P<port>\d+) ssh2(?::\s*(?P<detail>.*))?$"
)
PARTIAL_RE = re.compile(
    r"^Partial (?P<method>\S+) for (?P<user>\S+) from (?P<ip>\S+) "
    r"port (?P<port>\d+) ssh2(?::\s*(?P<detail>.*))?$"
)

log = logging.getLogger("ssh-login-alert")

_stopping = threading.Event()
_send_lock = threading.Lock()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config() -> configparser.ConfigParser:
    if not CONFIG_FILE.exists():
        log.error("config file not found: %s", CONFIG_FILE)
        sys.exit(1)
    cfg = configparser.ConfigParser()
    # Public key fingerprints are case-sensitive base64; do not lowercase keys.
    cfg.optionxform = str
    cfg.read(CONFIG_FILE)
    if "alert" not in cfg:
        log.error("config file %s has no [alert] section", CONFIG_FILE)
        sys.exit(1)
    return cfg


def parse_networks(raw: str):
    nets = []
    for token in raw.replace(",", " ").split():
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            log.warning("ignoring unparseable network in config: %s", token)
    return nets


def is_ignored(cfg, user: str, ip: str) -> bool:
    if "filter" not in cfg:
        return False
    users = cfg["filter"].get("ignore_users", "").replace(",", " ").split()
    if user in users:
        return True
    nets = parse_networks(cfg["filter"].get("ignore_ips", ""))
    if nets:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in nets)
    return False


def key_label(cfg, detail: str) -> str:
    """Map 'ED25519 SHA256:xxxx' to a friendly name from the [keys] section."""
    if not detail or "keys" not in cfg:
        return ""
    for token in detail.split():
        if token.startswith("SHA256:") and token in cfg["keys"]:
            return cfg["keys"][token]
    return ""


# --------------------------------------------------------------------------
# Mail
# --------------------------------------------------------------------------

def send_email(cfg, subject: str, body: str):
    to_addr   = cfg["alert"]["to"]
    from_addr = cfg["alert"]["from"]
    method    = cfg["alert"].get("method", "smtp").lower()

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"]    = subject
    msg["From"]       = from_addr
    msg["To"]         = to_addr
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    msg["Auto-Submitted"] = "auto-generated"

    if method == "smtp":
        host     = cfg["smtp"]["host"]
        port     = int(cfg["smtp"].get("port", 587))
        user     = cfg["smtp"].get("user", "")
        password = cfg["smtp"].get("password", "")
        use_ssl  = cfg["smtp"].getboolean("ssl", False)
        use_tls  = cfg["smtp"].getboolean("starttls", True)

        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server as s:
            if not use_ssl and use_tls:
                s.starttls()
            if user:
                s.login(user, password)
            s.sendmail(from_addr, [to_addr], msg.as_string())
    else:
        proc = subprocess.run(
            ["/usr/sbin/sendmail", "-t", "-oi"],
            input=msg.as_string(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=20,
        )
        if proc.returncode != 0:
            raise RuntimeError("sendmail exited %d: %s" % (proc.returncode, proc.stderr))


def deliver(cfg, subject: str, body: str) -> bool:
    """Try to send; on failure spool to disk for the retry thread."""
    try:
        with _send_lock:
            send_email(cfg, subject, body)
        log.info("alert sent: %s", subject)
        return True
    except Exception as exc:
        log.error("send failed (%s) — spooling: %s", exc, subject)
        spool(subject, body)
        return False


def spool(subject: str, body: str):
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    path = SPOOL_DIR / ("%d.json" % time.time_ns())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"subject": subject, "body": body}))
    tmp.rename(path)


def drain_spool(cfg):
    if not SPOOL_DIR.is_dir():
        return
    for path in sorted(SPOOL_DIR.glob("*.json")):
        try:
            item = json.loads(path.read_text())
        except Exception:
            log.warning("discarding unreadable spool file %s", path)
            path.unlink(missing_ok=True)
            continue
        try:
            with _send_lock:
                send_email(cfg, item["subject"], item["body"])
        except Exception as exc:
            log.warning("spool drain stalled (%s) — will retry", exc)
            return          # keep ordering; try again next interval
        path.unlink(missing_ok=True)
        log.info("spooled alert delivered: %s", item["subject"])


def retry_loop(cfg):
    while not _stopping.wait(RETRY_INTERVAL):
        drain_spool(cfg)


# --------------------------------------------------------------------------
# Cursor
# --------------------------------------------------------------------------

def read_cursor():
    try:
        value = CURSOR_FILE.read_text().strip()
        return value or None
    except FileNotFoundError:
        return None


def write_cursor(cursor: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_suffix(".tmp")
    tmp.write_text(cursor)
    tmp.replace(CURSOR_FILE)


# --------------------------------------------------------------------------
# Alert construction
# --------------------------------------------------------------------------

def ptr_lookup(cfg, ip: str) -> str:
    if not cfg["alert"].getboolean("resolve_ptr", True):
        return ""
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


def journal_time(entry) -> str:
    try:
        usec = int(entry.get("__REALTIME_TIMESTAMP", "0"))
        dt = datetime.fromtimestamp(usec / 1_000_000, tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %z")
    except Exception:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def build_alert(cfg, entry, match, chain):
    user   = match.group("user")
    ip     = match.group("ip")
    port   = match.group("port")
    detail = match.group("detail") or ""
    host   = entry.get("_HOSTNAME", socket.gethostname())
    when   = journal_time(entry)
    ptr    = ptr_lookup(cfg, ip)
    label  = key_label(cfg, " ".join(d for _, d in chain) + " " + detail)

    methods = [m for m, _ in chain] + [match.group("method")]
    fingerprints = [d for _, d in chain if d] + ([detail] if detail else [])

    subject = "[%s] SSH auth success: %s from %s" % (host, user, ip)

    lines = [
        "Successful SSH authentication on %s." % host,
        "",
        "User        : %s" % user,
        "Source      : %s port %s%s" % (ip, port, (" (%s)" % ptr) if ptr else ""),
        "Method chain: %s" % " -> ".join(methods),
    ]
    if fingerprints:
        lines.append("Key         : %s" % "; ".join(fingerprints))
    if label:
        lines.append("Known as    : %s" % label)
    lines += [
        "Time        : %s" % when,
        "sshd PID    : %s" % entry.get("_PID", "unknown"),
        "",
        "Note: this alert fires on authentication, not on shell login. It also",
        "covers scp, sftp and port-forward-only sessions.",
        "",
        "If this was not you, take action immediately.",
    ]
    return subject, "\n".join(lines)


# --------------------------------------------------------------------------
# Journal follow
# --------------------------------------------------------------------------

def identifiers(cfg):
    raw = cfg["alert"].get("journal_identifiers", "").replace(",", " ").split()
    return raw or DEFAULT_IDENTIFIERS


def journal_process(cfg, cursor):
    cmd = ["journalctl", "-o", "json", "--follow", "--no-pager", "-n", "0"]
    for ident in identifiers(cfg):
        cmd += ["-t", ident]
    if cursor:
        cmd += ["--after-cursor", cursor]
    else:
        cmd += ["--since", "now"]
    log.info("starting: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            universal_newlines=True, bufsize=1)


def prune(partials):
    now = time.monotonic()
    for pid in [p for p, v in partials.items() if now - v["ts"] > PARTIAL_TTL]:
        partials.pop(pid, None)


def run(cfg):
    partials = {}
    cursor = read_cursor()
    if cursor is None:
        log.info("no cursor found — starting from now, history will not alert")
    last_flush = 0.0

    while not _stopping.is_set():
        proc = journal_process(cfg, cursor)
        try:
            for line in proc.stdout:
                if _stopping.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                cursor = entry.get("__CURSOR", cursor)
                message = entry.get("MESSAGE", "")
                if isinstance(message, list):      # journald byte-array form
                    message = bytes(message).decode("utf-8", "replace")
                pid = entry.get("_PID", "?")

                m = PARTIAL_RE.match(message)
                if m:
                    prune(partials)
                    slot = partials.setdefault(pid, {"chain": [], "ts": time.monotonic()})
                    slot["chain"].append((m.group("method"), m.group("detail") or ""))
                    slot["ts"] = time.monotonic()
                    continue

                m = ACCEPTED_RE.match(message)
                if m:
                    chain = partials.pop(pid, {}).get("chain", [])
                    if is_ignored(cfg, m.group("user"), m.group("ip")):
                        log.info("suppressed by filter: %s from %s",
                                 m.group("user"), m.group("ip"))
                    else:
                        subject, body = build_alert(cfg, entry, m, chain)
                        deliver(cfg, subject, body)
                    write_cursor(cursor)
                    last_flush = time.monotonic()
                    continue

                if time.monotonic() - last_flush > CURSOR_FLUSH_SECS:
                    write_cursor(cursor)
                    last_flush = time.monotonic()
        finally:
            if cursor:
                write_cursor(cursor)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        if not _stopping.is_set():
            log.warning("journalctl exited (rc=%s) — restarting in 5s", proc.returncode)
            _stopping.wait(5)


def startup_check(cfg):
    """Fail loudly if we cannot see sshd in the journal.

    A daemon that cannot read the journal looks exactly like a daemon on a host
    where nobody has logged in. That is the worst possible failure mode for a
    monitoring tool, so shout about it in systemctl status rather than sitting
    there looking healthy.
    """
    idents = identifiers(cfg)
    cmd = ["journalctl", "-o", "cat", "-n", "1"]
    for ident in idents:
        cmd += ["-t", ident]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=20)
    except Exception as exc:
        log.error("SELF-TEST FAILED: could not run journalctl (%s)", exc)
        return
    if proc.returncode != 0:
        log.error("SELF-TEST FAILED: journalctl exited %d: %s",
                  proc.returncode, proc.stderr.strip())
        return
    if not proc.stdout.strip():
        log.error(
            "SELF-TEST FAILED: no journal entries for identifiers %s. "
            "Either this user cannot read the system journal (check it is in "
            "the systemd-journal group) or sshd logs under a different "
            "identifier on this host (OpenSSH 9.8+ uses sshd-session). "
            "NO ALERTS WILL BE SENT.", ", ".join(idents))
        return
    log.info("self-test OK: reading journal for %s", ", ".join(idents))


def handle_signal(signum, _frame):
    log.info("received signal %d — shutting down", signum)
    _stopping.set()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    cfg = load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)

    startup_check(cfg)

    threading.Thread(target=retry_loop, args=(cfg,), daemon=True).start()
    drain_spool(cfg)
    run(cfg)


if __name__ == "__main__":
    main()
