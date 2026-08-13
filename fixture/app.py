"""A deliberately legacy back-office app, used as the automation target.

This is a fixture, not a deliverable. It exists because the interesting problems
only appear on a surface that fights back, and because the exceptional states
have to be summonable on demand -- you cannot ask a public demo site to expire
your session.

What makes it hostile, and why each one matters:

- **Framesets.** The whole app is a frameset with a nav frame and a content
  frame. Anything that assumes one document per page breaks here, and real
  core-banking screens from the 2000s are built exactly this way.
- **Table-based layout.** Forms are laid out in nested tables with no <label>
  elements; a field's caption is a <td> sitting to its left. So "the input
  labelled Member ID" has to be derived from geometry and reading order rather
  than read off an attribute.
- **No test IDs, no stable classes.** Ids are server-generated and change per
  render (`ctl00_r3_c1`), which is what real ASP.NET-era apps emit. Anything
  that pins to them will pass once and fail tomorrow.
- **Non-semantic controls.** The primary action is an <a> styled as a button,
  not a <button>.

Failure modes are switchable with query parameters so a replay can be driven
into each branch deterministically:

    ?fail=notfound     the member does not exist  (a business outcome)
    ?fail=validation   the form rejects the input (a business outcome)
    ?fail=denied       permission denied          (a hard failure)
    ?fail=timeout      session expired, re-login  (recoverable)
    ?fail=slow         a 3 second stall           (recoverable)
    ?fail=dialog       an interstitial appears    (recoverable)
    ?fail=error        an unhandled app error     (a hard failure)

Run it with:  python -m fixture.app  [--port 8899] [--variant westfield]
"""

from __future__ import annotations

import argparse
import http.server
import socketserver
import time
import urllib.parse

# Two "tenants" running the same vendor product, configured differently. Used to
# show one artifact applying to both with per-variant overrides rather than a
# re-record. Note the second renames the field caption and moves the route.
VARIANTS = {
    "default": {
        "brand": "Meridian Core",
        "member_label": "Member ID",
        "search_path": "/members/search",
        "accent": "#1f4e79",
    },
    "westfield": {
        "brand": "Westfield CU Servicing",
        "member_label": "Account Number",
        "search_path": "/servicing/member-lookup",
        "accent": "#7a1f1f",
    },
}

MEMBERS = {
    "12345": {"name": "Dolores Abernathy", "savings": "4812.55", "checking": "1203.10", "status": "Active"},
    "22887": {"name": "Bernard Lowe", "savings": "119.02", "checking": "88.40", "status": "Active"},
    "30021": {"name": "Maeve Millay", "savings": "76230.18", "checking": "9902.77", "status": "Dormant"},
}

CFG = VARIANTS["default"]


def _page(body: str, title: str) -> bytes:
    # No viewport tag, no semantic landmarks, a table wrapper. On purpose.
    return f"""<html><head><title>{title}</title></head>
<body bgcolor="#f4f4f4">
<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td bgcolor="{CFG['accent']}" height="34">&nbsp;<font face="Verdana" size="2" color="#ffffff">
<b>{CFG['brand']}</b></font></td></tr></table>
{body}
</body></html>""".encode()


def _frameset() -> bytes:
    return f"""<html><head><title>{CFG['brand']}</title></head>
<frameset cols="180,*" border="1">
  <frame name="nav" src="/nav">
  <frame name="content" src="{CFG['search_path']}">
</frameset>
</html>""".encode()


def _nav() -> bytes:
    return _page(
        f"""<table cellpadding="4" cellspacing="0" border="0"><tr><td>
<font face="Verdana" size="1">
<a href="{CFG['search_path']}" target="content">Member Search</a><br><br>
<a href="/reports" target="content">Reports</a><br><br>
<a href="/admin" target="content">Administration</a>
</font></td></tr></table>""", "nav")


def _search_form(message: str = "") -> bytes:
    # The caption is a sibling <td>. There is no <label for=...>, and the id is
    # server-generated noise, so the field can only be found by its caption.
    msg = f'<tr><td colspan="2"><font face="Verdana" size="2" color="#a00000">{message}</font></td></tr>' if message else ""
    return _page(f"""
<table cellpadding="6" cellspacing="0" border="0" width="100%"><tr><td>
<font face="Verdana" size="2"><b>Member Search</b></font><br><br>
<form action="{CFG['search_path']}" method="get">
<table cellpadding="3" cellspacing="0" border="0">
  {msg}
  <tr>
    <td align="right"><font face="Verdana" size="2">{CFG['member_label']}</font></td>
    <td><input type="text" name="member_id" id="ctl00_r3_c1" size="18"></td>
  </tr>
  <tr><td></td><td>
    <a href="#" id="ctl00_r7_btn" onclick="this.closest('form').submit();return false;"
       style="border:1px solid #888;padding:3px 10px;background:#e8e8e8;text-decoration:none">
       <font face="Verdana" size="2" color="#000000">Search</font></a>
  </td></tr>
</table>
</form>
</td></tr></table>""", "Member Search")


def _member_page(m: dict) -> bytes:
    return _page(f"""
<table cellpadding="6" cellspacing="0" border="0" width="100%"><tr><td>
<font face="Verdana" size="2"><b>Member Detail</b></font><br><br>
<table cellpadding="3" cellspacing="0" border="0">
 <tr><td align="right"><font face="Verdana" size="2">Name</font></td>
     <td><font face="Verdana" size="2">{m['name']}</font></td></tr>
 <tr><td align="right"><font face="Verdana" size="2">Status</font></td>
     <td><font face="Verdana" size="2">{m['status']}</font></td></tr>
 <tr><td align="right"><font face="Verdana" size="2">Savings Balance</font></td>
     <td><font face="Verdana" size="2">{m['savings']}</font></td></tr>
 <tr><td align="right"><font face="Verdana" size="2">Checking Balance</font></td>
     <td><font face="Verdana" size="2">{m['checking']}</font></td></tr>
</table>
</td></tr></table>""", "Member Detail")


def _notice(kind: str) -> bytes:
    bodies = {
        "notfound": ("Member Search", "No member found with that identifier."),
        "validation": ("Member Search", "Identifier must be 5 digits."),
        "denied": ("Access Denied", "You do not have permission to view this member."),
        "timeout": ("Session Expired", "Your session has expired. Please sign in again."),
        "error": ("Application Error", "An unexpected error occurred. Reference 0x8007007E."),
    }
    title, text = bodies[kind]
    return _page(f"""
<table cellpadding="6" cellspacing="0" border="0" width="100%"><tr><td>
<font face="Verdana" size="2"><b>{title}</b></font><br><br>
<font face="Verdana" size="2">{text}</font>
</td></tr></table>""", title)


def _dialog(resume_query: str = "") -> bytes:
    # An interstitial the automation is allowed to dismiss and continue past.
    # "Continue" carries the original request forward, minus the fail switch, so
    # that dismissing it resumes the interrupted work rather than dumping the
    # user back at an empty form. Real interstitials behave this way, and a
    # fixture that loses the request would make recovery untestable end to end.
    href = f"{CFG['search_path']}?{resume_query}" if resume_query else CFG["search_path"]
    return _page(f"""
<table cellpadding="6" cellspacing="0" border="0" width="100%"><tr><td>
<font face="Verdana" size="2"><b>Notice</b></font><br><br>
<font face="Verdana" size="2">Scheduled maintenance this weekend.</font><br><br>
<a href="{href}" id="ctl00_ack">
  <font face="Verdana" size="2">Continue</font></a>
</td></tr></table>""", "Notice")


def _resume_query(q: dict[str, list[str]]) -> str:
    """The original query without the fail switch, so a dismissal can resume."""
    keep = {k: v for k, v in q.items() if k != "fail"}
    return urllib.parse.urlencode({k: v[0] for k, v in keep.items() if v})


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        fail = (q.get("fail") or [""])[0]
        path = parsed.path

        if fail == "slow":
            time.sleep(3)

        if path == "/":
            return self._send(_frameset())
        if path == "/nav":
            return self._send(_nav())
        if path == "/dialog":
            return self._send(_dialog(_resume_query(q)))

        if path == CFG["search_path"]:
            member_id = (q.get("member_id") or [""])[0]
            if not member_id:
                return self._send(_search_form())
            if fail == "dialog":
                return self._send(_dialog(_resume_query(q)))
            if fail in ("denied", "timeout", "error"):
                return self._send(_notice(fail))
            if fail == "validation" or (member_id and not member_id.isdigit()):
                return self._send(_search_form("Identifier must be 5 digits."))
            if fail == "notfound" or member_id not in MEMBERS:
                return self._send(_search_form("No member found with that identifier."))
            return self._send(_member_page(MEMBERS[member_id]))

        self._send(_page('<font face="Verdana" size="2">Not implemented.</font>', "…"))

    def _send(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8899, variant: str = "default") -> None:
    global CFG
    CFG = VARIANTS[variant]
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"fixture '{variant}' ({CFG['brand']}) on http://127.0.0.1:{port}/")
        httpd.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--variant", choices=sorted(VARIANTS), default="default")
    a = ap.parse_args()
    serve(a.port, a.variant)
