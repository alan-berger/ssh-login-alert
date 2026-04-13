#!/usr/bin/env python3
"""
ssh_login_monitor.py
Monitors a login log file for new entries and sends an email alert per new login.
Designed to be called from ~/.bash_profile on login, and optionally via cron as a fallback.
"""

import sys
import smtplib
import subprocess
import configparser
import socket
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

HOME        = Path.home()
CONFIG_FILE = HOME / ".ssh_login_monitor.conf"
STATE_FILE  = HOME / ".ssh_login_monitor.state"
LOGINLOG    = HOME / ".ssh_logins"
LOG_FILE    = HOME / "logs" / "ssh_login_monitor.log"


def log(msg: str):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def load_config() -> configparser.ConfigParser:
    if not CONFIG_FILE.exists():
        log(f"ERROR: Config file not found: {CONFIG_FILE}")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg


def send_email(cfg: configparser.ConfigParser, subject: str, body: str):
    to_addr   = cfg["alert"]["to"]
    from_addr = cfg["alert"]["from"]
    method    = cfg["alert"].get("method", "sendmail").lower()

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    if method == "smtp":
        host     = cfg["smtp"]["host"]
        port     = int(cfg["smtp"].get("port", 587))
        user     = cfg["smtp"]["user"]
        password = cfg["smtp"]["password"]
        use_tls  = cfg["smtp"].getboolean("starttls", True)

        with smtplib.SMTP(host, port, timeout=15) as s:
            if use_tls:
                s.starttls()
            s.login(user, password)
            s.sendmail(from_addr, [to_addr], msg.as_string())
    else:
        # sendmail — available on most Linux/cPanel hosts, no authentication required
        proc = subprocess.run(
            ["/usr/sbin/sendmail", "-t", "-oi"],
            input=msg.as_string(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=15
        )
        if proc.returncode != 0:
            raise RuntimeError(f"sendmail exited {proc.returncode}: {proc.stderr}")


def get_stored_count() -> int:
    try:
        return int(STATE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return -1


def store_count(count: int):
    STATE_FILE.write_text(str(count))


def parse_login_line(line: str) -> dict:
    """
    Parse a line written by the ~/.bash_profile logger.
    Format: YYYY-MM-DD HH:MM:SS +TZ IP USERNAME AUTHMETHOD
    Example: 2026-04-13 16:46:59 +0100 81.152.11.244 alice publickey
    """
    try:
        parts = line.strip().split()
        return {
            "timestamp":   f"{parts[0]} {parts[1]} {parts[2]}",
            "ip":          parts[3],
            "username":    parts[4],
            "auth_method": parts[5] if len(parts) > 5 else "unknown",
        }
    except (IndexError, ValueError):
        return {
            "timestamp":   "unknown",
            "ip":          "unknown",
            "username":    "unknown",
            "auth_method": "unknown",
        }


def main():
    if not LOGINLOG.exists():
        log("WARNING: ~/.ssh_logins not found — has ~/.bash_profile been updated?")
        return

    lines         = [l.strip() for l in LOGINLOG.read_text().splitlines() if l.strip()]
    current_count = len(lines)
    stored_count  = get_stored_count()

    if stored_count == -1:
        # First run — seed the state file without alerting on existing entries
        store_count(current_count)
        log(f"First run — seeding state with {current_count} existing entries")
        return

    if current_count <= stored_count:
        return  # No new entries

    new_lines = lines[stored_count:]
    cfg       = load_config()
    hostname  = socket.gethostname()
    errors    = []

    for line in new_lines:
        parsed  = parse_login_line(line)
        subject = f"[{hostname}] SSH login from {parsed['ip']}"
        body = (
            f"New SSH login detected on {hostname}.\n\n"
            f"IP address   : {parsed['ip']}\n"
            f"Username     : {parsed['username']}\n"
            f"Auth method  : {parsed['auth_method']}\n"
            f"Login time   : {parsed['timestamp']}\n\n"
            f"If this was not you, take action immediately.\n"
        )
        try:
            send_email(cfg, subject, body)
            log(f"Alert sent — login from {parsed['ip']} as {parsed['username']} "
                f"({parsed['auth_method']}) at {parsed['timestamp']}")
        except Exception as e:
            log(f"ERROR sending alert for {parsed['ip']}: {e}")
            errors.append(line)

    if not errors:
        store_count(current_count)
    else:
        # Partial failure — advance state only to the last successfully alerted line
        # so the next run retries any unsent alerts
        failed_index = lines.index(errors[0])
        store_count(failed_index)
        log(f"Partial failure — state advanced to line {failed_index}, will retry remaining")


if __name__ == "__main__":
    main()
