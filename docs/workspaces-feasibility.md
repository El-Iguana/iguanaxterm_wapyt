# AWS WorkSpaces embedding: feasibility report

**Result: FAIL for embedded sessions.** The official WorkSpaces web client
cannot be loaded inside an IguanaXterm tab or GridStack tile. Options that
get a WorkSpace desktop into a pane by other means are assessed below.

Date: 2026-11-10. Repository at `main` (v2.4.0, `f32ce2c`). Plan under test:
`../AWS_WORKSPACES_INTEGRATION.md` (Phase B, "Web embedding proof of
concept").

## 1. What was tested

The plan's gate asks whether the official WorkSpaces web client can load in
a same-page iframe under the IguanaXterm origin, without stripping or
rewriting any AWS security header. That question is answered by the
client's own response headers, before any login, so it was answered with a
header probe rather than a full browser session. No WorkSpace, registration
code or credential was involved.

Probed from Linux with `curl 8.x`, with and without a Chrome 142 User-Agent
and `Sec-Fetch-Dest: iframe`:

```bash
curl -sS -o /dev/null -D - https://us-east-1.webclient.amazonworkspaces.com/main
```

| URL | Response | Framing header |
|---|---|---|
| `https://clients.amazonworkspaces.com/` | 200 | `x-frame-options: DENY` |
| `https://clients.amazonworkspaces.com/webclient` | 301 → `webclient.amazonworkspaces.com/main` | |
| `https://webclient.amazonworkspaces.com/main` | 302 → `us-east-1.webclient.amazonworkspaces.com/main` | |
| `https://us-east-1.webclient.amazonworkspaces.com/main` | 200 | `x-frame-options: DENY` |
| `https://us-east-1.webclient.amazonworkspaces.com/index.html` | 200 | `x-frame-options: DENY` |

The regional client sends no `frame-ancestors` directive; `X-Frame-Options:
DENY` alone is enough. Every current browser refuses to render a response
carrying it inside any frame, from any origin, before the page's scripts
run. The header is identical for a browser User-Agent, so it is not a
client sniff that a real browser would avoid. The download page's own CSP
(`connect-src` limited to AWS broker and client-discovery hosts, cookies
scoped to `amazonworkspaces.com`) shows the shape of what a proxy would have
to rewrite; see option 1.

The client page contains no registration-code URL parameter (searched the
served HTML for `registrationCode` / `regCode`), so an external launch
cannot pre-fill the code.

Not tested, because the gate failed before it could be reached: login,
SAML/MFA, input, clipboard, audio, resize, reconnect, layout switching.

## 2. Our own CSP would also have blocked it

pytincture's service CSP (`pytincture/backend/app.py`,
`_service_content_security_policy`) emits no `frame-src` unless
`browser_frame_origins` or `browser_allow_blob_frames` is set, so frames fall
under `default-src 'self'`. Any future embed of a third-party origin needs
`PytinctureConfig(browser_frame_origins=("https://…",))` in `service.py`
(or `PYTINCTURE_BROWSER_FRAME_ORIGINS`). This is independent of the AWS
header and is noted so nobody debugs it twice.

## 3. Why nothing in the page can work around it

`X-Frame-Options` is enforced by the browser on the *framed* response. A
wapyt widget, Pyodide code, a different iframe attribute, `sandbox`, or a
`blob:` wrapper never changes it. Only three parties can alter the outcome:
whoever serves the response (a proxy), the browser (an extension), or the
host application (a native shell instead of a browser tab). The options
below are those three, plus two that avoid the web client altogether.

## 4. Options

### Option 1. Header-stripping proxy: rejected

IguanaXterm's server fetches the AWS client and rewrites responses, dropping
`X-Frame-Options` and serving the client same-origin.

- **Why it fails technically.** The client's CSP restricts `connect-src` to
  AWS broker and client-discovery hosts plus `'self'`; its cookies are scoped
  to `amazonworkspaces.com`; it opens WebSockets to regional brokers;
  sign-in redirects through an IdP that has its own framing rules. Every
  absolute URL, cookie domain and redirect would need rewriting, and it
  breaks on each new client build.
- **Why it is out of bounds.** Every user's WorkSpaces password, MFA and
  session tokens would pass through our server. The plan lists exactly this
  as a non-goal ("Do not proxy AWS login pages to make them same-origin";
  "Do not remove CSP, X-Frame-Options…"), and it is the kind of interference
  AWS's terms do not allow.

**Decision:** not pursued.

### Option 2. Browser extension that removes the header: proof of concept only

A Chrome or Firefox extension can drop `X-Frame-Options` on responses from
`*.webclient.amazonworkspaces.com` with `declarativeNetRequest`
`modifyHeaders`. The real client then loads in a plain iframe, untouched by
our server. Our side needs the `browser_frame_origins` setting from §2.

- **Costs.** Every user must install the extension; it removes clickjacking
  protection AWS chose deliberately; it depends on each browser's extension
  API staying permissive.
- **Unknown.** Whether the client's JavaScript refuses to run when
  `window.top !== window`. Only a test with a real, authorized WorkSpace
  answers that, plus the rest of the plan's Phase B checklist (login,
  SAML/MFA, input, clipboard, audio, resize, reconnect).

**Decision:** acceptable as a throwaway experiment to learn whether the
client works framed at all. Not a product path.

### Option 3. Native shell with a webview per pane: the only honest embed

Run IguanaXterm inside Electron or Tauri. A WorkSpaces pane gets its own
`<webview>` / `BrowserView` positioned over the pane's rectangle. A webview is
a top-level browsing context, so `X-Frame-Options` does not apply, and the
official client runs exactly as in a normal tab: login, SAML/MFA, audio,
clipboard and keyboard all behave.

- **Fits the project's direction.** Native Windows (`tools/windows/`) and
  planned macOS installs already exist; today they launch the system
  browser. A shell is a plausible next form of those.
- **Costs.** A second UI host alongside the browser one; per-platform
  packaging and signing; layout switching and GridStack drag must move a
  native view, not a DOM node, and `_toggle_maximize`'s overlay trick does
  not cover a native view. The plain-browser deployment (compose, Caddy)
  would still not embed WorkSpaces.
- **Still true.** Closing the pane closes the client, not the cloud
  WorkSpace; label it "Close pane".

**Decision:** the only route that embeds the real client without touching
AWS's controls. Large enough to be its own project; scope separately if the
WorkSpaces *client* is the requirement.

### Option 4. RDP to the WorkSpace through the existing RDP type: test first

A Windows WorkSpace is a Windows machine with a network interface in the
customer VPC. If its security group allows TCP 3389 from a bastion and
Remote Desktop is enabled in Windows, the existing `rdp` session type with
an SSH tunnel (`via_session_id`, PR #60) opens it in a pane today, with
guacd doing RDP and the certificate pinned as for any other RDP site. No
new code.

- **Conditions.** Network reach into the VPC (bastion, VPN or Direct
  Connect); 3389 opened in the WorkSpace's security group; RDP enabled on
  the WorkSpace; directory credentials. guacd must be configured
  (`GANXTERM_GUACD`), so not the native Windows build.
- **Caveats.** AWS does not support RDP as the end-user path, so it is the
  administrator's door, not the user's. An RDP login can disconnect an
  active WorkSpaces client session on the same desktop. Linux WorkSpaces
  have no RDP (VNC via the existing `vnc` type would need a VNC server
  installed on the WorkSpace). None of the WorkSpaces client features
  (printer redirection, smart cards, WSP/DCV adaptive streaming) apply.

**Decision:** a troubleshooting path, and AWS positions it that way (its
own guidance describes RDP to a WorkSpace as something you open the
security group for when diagnosing a problem). It is **not** offered as the
standard WorkSpaces workflow and gets no new UI. It is worth one test with
an existing WorkSpace so the wiki can describe it honestly under
*Remote-Desktops*, with the caveats above, for administrators who already
have a bastion.

### Option 5. AWS products that embed on purpose

If the requirement is "an AWS-hosted desktop in a tile" rather than
WorkSpaces Personal specifically, two AWS products allow embedding by
design. Neither is WorkSpaces Personal's brokered session; the plan's §6
already warns against conflating the two.

**AppStream 2.0 embedded streaming.** AWS documents iframe embedding with a
domain allowlist. A different product (streamed applications or a desktop
view) with its own fleet and pricing. Not investigated further.

**Self-managed Amazon DCV** (on EC2, where the server needs no licence, or
on-premises with one). Verified against the DCV documentation on
2026-11-10; there are two distinct ways in, and they are not the same
thing:

- **5a. Frame the DCV server's own web client.** The server ships a web
  page, and its administrator can allow framing with two settings in the
  `[connectivity]` section of `dcv.conf` (Windows: the same names under the
  GSettings registry key):

  ```ini
  [connectivity]
  web-x-frame-options="ALLOW-FROM https://iguanaxterm.example"
  web-extra-http-headers=[("Content-Security-Policy", "frame-ancestors https://iguanaxterm.example")]
  ```

  Behind a DCV Connection Gateway the same headers go in the gateway's
  `[web-resources]` `local-resources-http-headers` instead. Two notes: the
  value must name the *IguanaXterm* origin, not the DCV server's own as the
  AWS example does; and `ALLOW-FROM` is an obsolete `X-Frame-Options` value
  that current browsers ignore, so the `frame-ancestors` header is the one
  doing the work. On our side only `browser_frame_origins` (§2) is needed.
  This gives a frame, with the same limits a frame always has: no shared
  clipboard surface, no resize contract, reparenting may reload it, and the
  user signs in inside the frame.

- **5b. The DCV Web Client SDK as a real pane.** The SDK is `dcv.js` plus
  auxiliary files, distributed as a GPG-signed zip from
  `download.nice-dcv.com` (no npm, no CDN), to be served from our own web
  server with its folder structure intact. It draws into a plain `<div>`,
  so **no iframe is involved and the framing question disappears**. The
  browser calls `dcv.authenticate(serverUrl, …)`, answers the server's
  credential challenge, receives `sessionId` + `authToken`, then
  `dcv.connect({url, sessionId, authToken, divId})`. That is the shape of a
  genuine `dcv` session type next to `vnc` and `rdp`, mounted by
  `_mount_desktop`, and it extends IguanaXterm past what guacd speaks.

  What 5b has to solve before it is a plan:

  1. **Licence.** The SDK is proprietary, under the `EULA.txt` in the
     archive, not open source. The repo is MIT and vendors only
     open-source assets (xterm, noVNC, guacamole-common-js, GridStack).
     Read the EULA; if redistribution is not granted, fetch and
     GPG-verify the zip at image build time in the `Containerfile` (as
     wapyt is cloned there) and in `tools/windows/build.py`, rather than
     committing it. `third-party-licenses.txt` must be served alongside.
  2. **Direct or relayed.** The SDK talks HTTPS and WebSocket straight to
     the DCV server. Direct means: our CSP `connect-src` must list every
     DCV server origin, and `browser_connect_origins` is fixed at startup,
     so adding a site means a restart or a `https: wss:` wildcard; the
     browser must trust the server's certificate, and DCV's default is
     self-signed, with no pinning possible from JavaScript; and the user
     types the DCV password in the browser, outside the Fernet store and
     outside `access.resolve_site`. A relay (`/ws/dcv/<id>` plus the
     handful of HTTP endpoints the SDK calls, pointed at our own origin)
     keeps the existing rules: credentials stay server-side, the
     certificate is pinned in `host_key` like RDP's, `resolve_site` and
     `live.recheck` apply, and the SSH tunnel feature carries over. That
     is also what AWS's own DCV Connection Gateway is. The relay is the
     recommended shape; its cost is reverse-proxying the SDK's HTTP calls
     as well as its WebSocket, which needs the SDK's traffic mapped first.
     QUIC transport is out either way; the web client uses WebSocket.
  3. **Lifecycle.** The SDK has a `connection` object with disconnect,
     resize and input-focus calls, so `IxRDP`'s RFB-shaped surface can be
     matched (`disconnect`, `focus`, `clipboardPasteFrom`, events) and the
     VNC/RDP handlers shared, as the RDP pane does. To be confirmed
     against the SDK reference, not assumed.
  4. **A DCV server to test against.** An EC2 instance with DCV, or an
     on-premises evaluation licence. None exists on this workstation.

**Decision:** 5b is the real technical opportunity in this whole exercise,
and it is a new connection type, not a WorkSpaces workaround. 5a is a
cheap stepping stone for a demo only.

## 5. Recommendation: two tracks, no embedded WorkSpaces

1. Mark the plan's Phase B **failed** and take iframe-based WorkSpaces off
   the roadmap. Do not add an `aws-workspaces` session type that implies
   an embedded session. Option 1 is closed; option 2 is an experiment at
   most; option 3 is its own project if the WorkSpaces *client* inside the
   app is ever a hard requirement.

2. **Track A, AWS WorkSpaces management** (the plan's Phase E, unchanged):
   server-side `boto3`, paginated `DescribeWorkspaces`, state, start/stop/
   reboot with confirmation, least-privilege IAM, credentials never in the
   browser, and an external launch to the regional web client with the
   registration code shown for copying (no URL parameter exists for it).
   Honest wording throughout: this opens the official client in a new tab;
   nothing is embedded. Whether this belongs in a terminal manager is a
   product call.

3. **Track B, native Amazon DCV session type** (option 5b): investigate
   the SDK's EULA and traffic, decide relay versus direct, then build a
   `dcv` type with a renderer that works in tabs and GridStack. This
   extends IguanaXterm's embedded protocols beyond Guacamole and is the
   track to prioritise if embedded capability is the goal. It needs a DCV
   server to test against before any code.

4. **RDP to a WorkSpace** (option 4) stays a documented troubleshooting
   path for administrators, not a product feature.

## 6. What a future re-test needs

The WorkSpaces gate re-opens only if AWS stops sending `X-Frame-Options:
DENY` (or replaces it with a `frame-ancestors` that names customer
origins). Re-run the probe in §1; if the header is gone, continue with
Phase B's full checklist against a real WorkSpace, with
`browser_frame_origins` set (§2).

## 7. References

- [Embed the Amazon DCV web browser client inside an iFrame](https://docs.aws.amazon.com/dcv/latest/adminguide/embed-in-iframe.html)
  (`web-x-frame-options`, `web-extra-http-headers`, Connection Gateway note)
- [Getting started with the Amazon DCV Web Client SDK](https://docs.aws.amazon.com/dcv/latest/websdkguide/getting-started.html)
  (signed zip distribution, folder structure, EULA and third-party licence files)
- [Connect to a Amazon DCV server and get the first frame](https://docs.aws.amazon.com/dcv/latest/websdkguide/establish-connection.html)
  (`dcv.authenticate`, `dcv.connect`, `divId`)
- [Step 2: License the Amazon DCV Server](https://docs.aws.amazon.com/dcv/latest/adminguide/setting-up-license.html)
  (no licence on EC2; clients need none)
- [AWS blog: solving common WorkSpaces connection issues](https://aws.amazon.com/blogs/desktop-and-application-streaming/solving-common-amazon-workspaces-connection-issues-to-on-premises-setups)
  and [re:Post: connect to a WorkSpace with RDP](https://www.repost.aws/knowledge-center/connect-workspace-rdp)
  (RDP as a troubleshooting path that needs the security group opened)
- `../AWS_WORKSPACES_INTEGRATION.md`, the plan this report answers.
