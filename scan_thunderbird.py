"""Scan Thunderbird's downloaded mail and open Email Footprint with the results.

Thunderbird stores each mail folder as an mbox file inside your profile, so no
export is needed. This reads only the message headers (From, Subject, Date, To),
skips Sent/Drafts/Trash/Junk, and writes email-footprint-results.html next to
this script with the headers embedded. Nothing is sent anywhere.

By default it then runs a small server on this PC only (127.0.0.1) so the page's
Unsubscribe buttons work: for senders that support one-click unsubscribe
(RFC 8058), the server sends the unsubscribe request for you. Press Ctrl+C to stop.

Usage:  py scan_thunderbird.py [--no-serve] [--no-open]
"""
import configparser
import json
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "email-footprint.html"
OUT = HERE / "email-footprint-results.html"
SKIP = {"sent", "sent items", "sent mail", "drafts", "outbox", "deleted", "deleted items",
        "trash", "junk", "spam", "templates", "unsent messages", "conflicts",
        "local failures", "server failures"}
WANTED = ("from", "subject", "date", "to", "delivered-to", "x-mozilla-status",
          "list-unsubscribe", "list-unsubscribe-post")


def find_profile():
    root = Path(os.environ["APPDATA"]) / "Thunderbird"
    cfg = configparser.ConfigParser()
    cfg.read(root / "profiles.ini", encoding="utf-8")
    for section in cfg.sections():  # the [Install...] section names the profile Thunderbird actually uses
        if section.startswith("Install") and cfg.has_option(section, "Default"):
            return root / cfg.get(section, "Default")
    for section in cfg.sections():
        if cfg.get(section, "Default", fallback="") == "1":
            return root / cfg.get(section, "Path")
    sys.exit("No Thunderbird profile found. Open Thunderbird and add your email account first.")


def mail_folders(profile):
    for base in ("ImapMail", "Mail"):
        for f in sorted((profile / base).rglob("*")):
            if f.is_file() and not f.suffix and f.stat().st_size > 0 and f.name.lower() not in SKIP \
                    and f.name != "msgFilterRules":
                yield f


def read_headers(path):
    """Yield one list of header values per message in an mbox file."""
    headers, last, in_headers, prev_blank = {}, None, True, True

    def emit():
        if headers.get("from"):
            return [headers.get(k, "") for k in WANTED]

    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.rstrip(b"\r\n")
            is_sep = line.startswith(b"From - ") or (prev_blank and line.startswith(b"From "))
            prev_blank = not line
            if is_sep:
                msg = emit()
                if msg:
                    yield msg
                headers, last, in_headers = {}, None, True
                continue
            if not in_headers:
                continue
            if not line:
                in_headers = not headers
                continue
            text = line.decode("utf-8", "replace")
            if text[0] in " \t" and last:
                headers[last] += " " + text.strip()
                continue
            key, sep, value = text.partition(":")
            key = key.lower()
            if sep and key in WANTED and key not in headers:
                headers[key], last = value.strip(), key
            else:
                last = None
    msg = emit()
    if msg:
        yield msg


def main():
    profile = find_profile()
    messages, folders = [], []
    for folder in mail_folders(profile):
        start = len(messages)
        messages.extend(read_headers(folder))
        folders.append(folder.name)
        print(f"  {folder.name}: {len(messages) - start:,} emails")
    if not messages:
        sys.exit("No downloaded mail found. Let Thunderbird finish downloading, then run this again.")

    preload = {"id": f"tb-{int(time.time())}", "folders": folders, "messages": messages}
    data = json.dumps(preload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = APP.read_text(encoding="utf-8")
    html = html.replace("<script>", f"<script>window.PRELOAD={data};</script>\n<script>", 1)
    OUT.write_text(html, encoding="utf-8")
    print(f"Scanned {len(messages):,} emails. Results: {OUT}")
    if "--no-serve" in sys.argv:
        if "--no-open" not in sys.argv:
            webbrowser.open(OUT.as_uri())
        return
    serve(html, unsubscribe_urls(messages))


def unsubscribe_urls(messages):
    """The one-click unsubscribe links found in your mail; the server sends requests to these only."""
    urls = set()
    i_list, i_post = WANTED.index("list-unsubscribe"), WANTED.index("list-unsubscribe-post")
    for m in messages:
        if "one-click" in m[i_post].lower():
            for part in m[i_list].split(","):
                link = part.strip().strip("<>").strip()
                if link.lower().startswith("https://"):
                    urls.add(link)
    return urls


def one_click_unsubscribe(url):
    req = urllib.request.Request(url, data=b"List-Unsubscribe=One-Click", method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "EmailFootprint/1.0 (one-click unsubscribe, RFC 8058)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return {"ok": 200 <= resp.status < 400, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code}
    except Exception as e:  # network errors, timeouts, bad certificates
        return {"ok": False, "status": 0, "error": str(e)[:200]}


def serve(html, allowed, port=8790):
    page = html.encode("utf-8")
    origin = f"http://127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self.send(200, page, "text/html; charset=utf-8")
            elif self.path == "/api/ping":
                self.send(200, b'{"ok":true}')
            else:
                self.send(404, b"{}")

        def do_POST(self):
            # Only this page may call the API, and only for links that came from your own mail.
            if self.path != "/api/unsubscribe" or self.headers.get("Host") != f"127.0.0.1:{port}" \
                    or self.headers.get("Origin") != origin:
                return self.send(403, b'{"ok":false,"error":"Not allowed"}')
            try:
                url = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))["url"]
            except Exception:
                return self.send(400, b'{"ok":false,"error":"Bad request"}')
            if url not in allowed:
                return self.send(403, b'{"ok":false,"error":"This link was not found in your mail"}')
            result = one_click_unsubscribe(url)
            print(f"  Unsubscribe {'OK' if result['ok'] else 'FAILED'} ({result['status']}): {url[:80]}")
            self.send(200, json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Open {origin}/ to review and unsubscribe. Press Ctrl+C to stop.")
    if "--no-open" not in sys.argv:
        webbrowser.open(origin + "/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
