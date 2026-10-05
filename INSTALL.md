# Installing IguanaXterm

IguanaXterm runs as a container. You need **Docker** or **Podman** — on
Windows, macOS or Linux — and nothing else: the image builds from this
repository and fetches everything it needs (Python packages, pytincture and
the wapyt widgetset) from the internet while it builds.

- **Disk:** about 1.5 GB for the images and build cache.
- **Network during the build:** github.com, pypi.org and deb.debian.org.
- **Time:** the first build takes roughly 3–10 minutes; later ones reuse most of it.

**Windows without Docker?** There is also a native installer, with no
container engine and no HTTPS: see
[section 12](#12-windows-without-docker-native-installer).

Every command below is shown for Docker. **For Podman, replace `docker` with
`podman`** — the compose files, flags and paths are the same, except where a
section says otherwise. Where Windows PowerShell differs from macOS/Linux
shells, both are given.

**Contents**

1. [Install a container engine](#1-install-a-container-engine)
2. [Get IguanaXterm](#2-get-iguanaxterm)
3. [Configure](#3-configure)
4. [Start it](#4-start-it)
5. [Connect to your servers](#5-connect-to-your-servers)
6. [Folder downloads](#6-folder-downloads)
7. [Everyday tasks](#7-everyday-tasks): stop, logs, update, back up, reset a password, uninstall
8. [Without compose](#8-without-compose)
9. [Reaching IguanaXterm from other machines](#9-reaching-iguanaxterm-from-other-machines)
10. [Troubleshooting](#10-troubleshooting)
11. [What was actually tested](#11-what-was-actually-tested)
12. [Windows without Docker (native installer)](#12-windows-without-docker-native-installer)

---

## 1. Install a container engine

Pick **one** engine. You also need its **compose** tool, which starts
IguanaXterm from `compose.yaml` with one command.

### Windows 10 (22H2 or later) and Windows 11

**Docker Desktop**

1. Install Docker Desktop from <https://www.docker.com/products/docker-desktop/>,
   or in PowerShell: `winget install Docker.DockerDesktop`.
2. It uses WSL 2. If the installer asks, let it enable WSL, or run
   `wsl --install` in an administrator PowerShell and restart.
3. Start Docker Desktop and wait until it says it is running.
4. Check, in a new PowerShell window:
   ```powershell
   docker version
   docker compose version
   ```

**Podman**

1. Install Podman Desktop from <https://podman-desktop.io/>, or in PowerShell:
   `winget install RedHat.Podman-Desktop` (the command-line only package is
   `RedHat.Podman`).
2. Create and start the Podman machine (a small Linux VM, using WSL 2):
   ```powershell
   podman machine init
   podman machine start
   ```
   Podman Desktop offers to do this on first launch.
3. Install a compose provider. Podman Desktop's **Compose** extension installs
   one; on the command line: `winget install Docker.DockerCompose`.
4. Check:
   ```powershell
   podman version
   podman compose version
   ```

**Git** (to clone): `winget install Git.Git`, or skip it and download the ZIP
in step 2.

### macOS 13 or later (Apple Silicon or Intel)

**Docker Desktop** — install it from <https://www.docker.com/products/docker-desktop/>,
then start it and check `docker version` and `docker compose version`.
Docker-compatible alternatives such as OrbStack or Colima work the same way,
since they provide the `docker` command.

**Podman**

```sh
brew install podman docker-compose      # docker-compose is podman compose's provider
podman machine init
podman machine start
podman compose version
```

Podman Desktop (<https://podman-desktop.io/>) is the graphical alternative.

On Apple Silicon the image builds as native `arm64`; nothing to configure.

### Linux

**Docker Engine** — follow <https://docs.docker.com/engine/install/> for your
distribution and install the Compose plugin (`docker-compose-plugin`). To use
`docker` without `sudo`, add yourself to the `docker` group and log in again:

```sh
sudo usermod -aG docker "$USER"
```

**Podman** (rootless by default; nothing needs `sudo` once it is installed):

```sh
sudo apt install podman                 # Debian, Ubuntu
sudo dnf install podman                 # Fedora, RHEL, CentOS Stream
```

Then a compose provider — either one:

```sh
sudo apt install podman-compose         # or: pipx install podman-compose
sudo dnf install podman-compose
# or Docker's compose plugin, which podman compose also uses
```

Check with `podman compose version`.

---

## 2. Get IguanaXterm

```sh
git clone https://github.com/El-Iguana/iguanaxterm_wapyt.git
cd iguanaxterm_wapyt
```

Without Git: on <https://github.com/El-Iguana/iguanaxterm_wapyt> choose
**Code → Download ZIP**, unzip it, and open a terminal in the folder.

> **Windows:** the repository forces Unix line endings (`.gitattributes`), so
> Git's usual CRLF conversion does not touch it. Keep it that way — a `\r` at
> the end of a setting in `.env` becomes part of the value.

---

## 3. Configure

Copy the example settings:

```sh
cp .env.example .env                    # macOS, Linux
```
```powershell
Copy-Item .env.example .env             # Windows PowerShell
```

Open `.env` in any text editor and set, at least:

| Setting | What it does |
|---|---|
| `GANXTERM_ADMIN_PASS` | Password of the first account, `admin`. **Used only on the very first start**, when there are no accounts yet. Left empty, it is `change_me`. Either way, IguanaXterm asks for a new password each time it loads until you change it from the app. |
| `GANXTERM_ADMIN_USER` | Its name, if not `admin`. |
| `GANXTERM_PORT` | The port on this computer, default `8765`. Change it if 8765 is taken. |

> **Windows:** Notepad may save the file as `.env.txt` when file extensions are
> hidden. In File Explorer turn on **View → Show → File name extensions** and
> check that the file is called exactly `.env`.

Everything else in `.env.example` is optional and explained there; the full
list is in [README.md](README.md#configuration).

---

## 4. Start it

```sh
docker compose up -d --build
```

This builds the image and starts the container — named `iguanaxterm`, with
hostname `iguanaxterm` and the label `app=IguanaXterm` — in the background.
Watch it come up:

```sh
docker compose ps           # wait for "(healthy)"
docker compose logs -f      # Ctrl+C stops following, not the app
```

Then open **<http://127.0.0.1:8765/iguanaxterm>** (or your `GANXTERM_PORT`)
and sign in as `admin` with the password from `.env`. Change it from
**Password** in the toolbar. The first page load takes 30–60 seconds while the
browser sets up Python; it is cached afterwards.

> **Use `127.0.0.1`, not `localhost`.** IguanaXterm (through pytincture) only
> serves signed-in sessions over plain HTTP on a *literal* loopback address,
> and it only listens on this computer. `localhost` is refused with
> `Invalid host header`. To use it from other machines, see
> [section 9](#9-reaching-iguanaxterm-from-other-machines).

---

## 5. Connect to your servers

**New** in the toolbar saves a session: SSH, SFTP, FTP(S), Telnet or VNC, with
a host and port. IguanaXterm connects **from inside its container**, so:

| Where the server is | Host |
|---|---|
| Another machine on your network, a VPS, a NAS | its name or IP address, as from any computer |
| This computer — **Docker Desktop** (Windows, macOS) | `host.docker.internal` |
| This computer — **Podman** (Windows, macOS) | `host.containers.internal` (`host.docker.internal` works too) |
| This computer — **Linux** | see [below](#servers-on-the-same-computer-linux) |
| Another container | its container name, after `docker network connect iguanaxterm_default <container>` |
| A VNC desktop that only listens on its own machine | a VNC session with **Connect → Through SSH session**, host as seen from there (usually `localhost`) |

**Check before you guess.** This asks the container itself whether it can
reach a host and port:

```sh
docker compose exec iguanaxterm python manage.py probe host.docker.internal 22
```

It answers *reachable*, *does not resolve*, or *not reachable* with the reason.

### Servers on the same computer, Linux

On Linux, `host.docker.internal` / `host.containers.internal` reach this
computer's **network addresses**, not its `127.0.0.1`. A server listening on
all addresses — the usual `sshd` — is fine. One listening only on `127.0.0.1`
— a VM's forwarded port, a local VNC server — answers *connection refused*
from the container. Then either:

1. **Use host networking**, where `127.0.0.1` inside the container *is* this
   computer (IguanaXterm still listens on 127.0.0.1 only):
   ```sh
   docker compose down
   docker compose -f compose.host-network.yaml up -d --build
   ```
   Use the same `-f compose.host-network.yaml` on later `ps`, `logs`, `down`
   and `exec` commands. (Linux only: on Windows and macOS, host networking
   means the engine's VM.)
2. **Tunnel through SSH**, for VNC: a VNC session can connect through a saved
   SSH session to the same computer, and reach `localhost` from there.

---

## 6. Folder downloads

Downloading a **folder** from the file browser needs the browser's folder
picker. Chrome and Edge have it (on `127.0.0.1` or over HTTPS); Firefox and
Brave (by default) do not. There, IguanaXterm copies the folder to the
**server** instead and says so: by default into its data volume, where
**Saved files** in the toolbar lists the folders, downloads their files one by
one, and deletes them. That works the same on every system and needs nothing
set up.

### Saving folders to this computer

When IguanaXterm runs on your own computer, the folders can land in a real
folder instead — default `~/Downloads/IguanaXterm`, one subfolder per user;
set `GANXTERM_DOWNLOAD_HOST_DIR` in `.env` for another. **Use a dedicated
folder**, not `~/Downloads` itself. How depends on the engine:

**Docker Desktop (Windows, macOS)** — add the override file:

```sh
docker compose -f compose.yaml -f compose.downloads.yaml up -d
```

**Docker Engine (Linux)** — the same, but the container's user (uid 10001)
must be able to write the folder first:

```sh
mkdir -p ~/Downloads/IguanaXterm
sudo chown 10001 ~/Downloads/IguanaXterm      # or: chmod 777 it, if you prefer
docker compose -f compose.yaml -f compose.downloads.yaml up -d
```

Saved files are then owned by uid 10001 on the host.

**Podman (Linux)** — use `podman run` instead of compose. `--userns=keep-id`
makes the container's user *be* you, so saved files are yours; `:U` re-owns
the data volume to match and `:z` sets the folder's SELinux label. (`podman
compose` through the Docker Compose provider drops the `:U` option, and
without it a volume created under the other user mapping stays read-only, so
compose cannot do this reliably.)

```sh
podman compose down                                  # if it was running
mkdir -p ~/Downloads/IguanaXterm
podman build -f Containerfile -t localhost/iguanaxterm:latest .
podman run -d --name iguanaxterm --hostname iguanaxterm --label app=IguanaXterm \
  --restart unless-stopped \
  -p 127.0.0.1:8765:8765 \
  --env-file .env \
  --userns=keep-id:uid=10001,gid=999 \
  -e GANXTERM_DOWNLOAD_DIR=/downloads \
  -e GANXTERM_DOWNLOAD_HOST_DIR='~/Downloads/IguanaXterm' \
  -v iguanaxterm-data:/data:U \
  -v ~/Downloads/IguanaXterm:/downloads:z \
  localhost/iguanaxterm:latest
```

**Podman: switching back to compose** after running this way, re-own the data
volume once, or the app fails with *attempt to write a readonly database*:

```sh
podman rm -f iguanaxterm
podman unshare chown -R 10001:999 "$(podman volume inspect iguanaxterm-data --format '{{.Mountpoint}}')"
podman compose up -d
```

---

## 7. Everyday tasks

All of these run in the `iguanaxterm_wapyt` folder. With host networking, add
`-f compose.host-network.yaml` after `compose`.

**Stop, start, restart, logs**

```sh
docker compose stop
docker compose start
docker compose restart
docker compose logs -f
```

The container restarts with the engine unless you stopped it. (Podman on Linux
does not start containers at boot by itself; a Quadlet or
`podman generate systemd` does, see Podman's documentation.)

**Update to a newer version**

From 2.1.0, IguanaXterm says when a newer release is out: an **Update x.y.z**
badge in the toolbar, and a link in **About**, which also shows the version
you are running (`GANXTERM_UPDATE_CHECK=off` stops the check). Before 2.1.0,
watch the repository's releases on GitHub (**Watch → Custom → Releases**).

```sh
git pull
docker compose up -d --build
```

Use the same compose files you started with: `-f compose.host-network.yaml`,
or `-f compose.yaml -f compose.downloads.yaml`, after `compose`.

- **Installed from a ZIP:** unzip the new version into a new folder, copy
  your `.env` into it, and run the same command there. The compose project,
  container and volume have fixed names, so it takes over the same data.
- **Running with the `podman run` command** of section 6: `git pull`, then
  `podman rm -f iguanaxterm` and the `podman build` and `podman run` commands
  from section 6 again.
- **The Windows installer:** see [section 12](#12-windows-without-docker-native-installer);
  run the new `setup.exe` over the old one.

Your accounts, saved sessions, pinned host keys and layouts live in the
`iguanaxterm-data` volume and survive updates and rebuilds. The database is
updated on the first start and only ever gains columns or tables; to go back
to an older version, restore a backup taken before updating (below).

**Back up the data volume** — the SQLite database, `secret.key` (which
encrypts every stored password and private key) and folders saved on the
server. *Without `secret.key` the stored credentials cannot be read.*

**Podman** (any OS) exports and imports volumes itself:

```sh
podman volume export iguanaxterm-data --output iguanaxterm-data.tar
# restore: stop IguanaXterm, then import into the (existing, empty) volume
podman volume import iguanaxterm-data iguanaxterm-data.tar
```

**Docker** has no export, so a throwaway container copies the volume into a
dedicated `backups` folder:

```sh
# macOS, Linux
mkdir -p backups
docker run --rm -v iguanaxterm-data:/data -v "$PWD/backups:/backup:z" \
  docker.io/library/alpine tar czf /backup/iguanaxterm-data.tgz -C /data .
```
```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force backups | Out-Null
docker run --rm -v iguanaxterm-data:/data -v "${PWD}\backups:/backup" `
  docker.io/library/alpine tar czf /backup/iguanaxterm-data.tgz -C /data .
```

To restore, stop IguanaXterm, run the same command with
`tar xzf /backup/iguanaxterm-data.tgz -C /data`, and start it again.

> `:z` lets the container write to the folder on Linux with SELinux (Fedora,
> RHEL), where it is otherwise *Permission denied*; elsewhere it does nothing.
> It relabels the folder, so only ever give it a dedicated one like `backups`.

**Forgot the admin password**

```sh
docker compose exec -it iguanaxterm python manage.py reset-password admin
```

It asks for the new password twice. `manage.py users` lists the accounts;
`reset-password NAME --create` makes a new administrator.

**Uninstall**

```sh
docker compose down                 # removes the container, keeps your data
docker compose down -v              # …and deletes the data volume — irreversible
docker image rm localhost/iguanaxterm:latest
```

---

## 8. Without compose

The same thing with plain commands (Podman: replace `docker` with `podman`).

```sh
docker build -f Containerfile -t localhost/iguanaxterm:latest .
docker volume create iguanaxterm-data
docker run -d --name iguanaxterm --hostname iguanaxterm --label app=IguanaXterm \
  --restart unless-stopped \
  -p 127.0.0.1:8765:8765 \
  --env-file .env \
  --add-host host.docker.internal:host-gateway \
  -v iguanaxterm-data:/data \
  localhost/iguanaxterm:latest
```
```powershell
docker build -f Containerfile -t localhost/iguanaxterm:latest .
docker volume create iguanaxterm-data
docker run -d --name iguanaxterm --hostname iguanaxterm --label app=IguanaXterm `
  --restart unless-stopped `
  -p 127.0.0.1:8765:8765 `
  --env-file .env `
  --add-host host.docker.internal:host-gateway `
  -v iguanaxterm-data:/data `
  localhost/iguanaxterm:latest
```

- `-f Containerfile` is required with Docker, which otherwise looks for a file
  named `Dockerfile`.
- `--add-host …:host-gateway` gives Docker Engine on Linux the
  `host.docker.internal` name; it is harmless elsewhere.
- **Another host port** (say 9000): `-p 127.0.0.1:9000:8765` *and*
  `-e GANXTERM_CANONICAL_ORIGIN=http://127.0.0.1:9000`, since the app must know
  the address the browser uses.
- **Host networking on Linux**: replace the `-p` and `--add-host` lines with
  `--network host -e GANXTERM_BIND=127.0.0.1`.

---

## 9. Reaching IguanaXterm from other machines

IguanaXterm holds your SSH credentials, so it refuses to serve sign-ins over
plain HTTP anywhere but `127.0.0.1`. To use it from other machines, put an
HTTPS reverse proxy in front and tell IguanaXterm its public address. With
[Caddy](https://caddyserver.com/) on the same machine, which obtains the
certificate itself:

1. In `.env`:
   ```
   GANXTERM_CANONICAL_ORIGIN=https://terminal.example.com
   GANXTERM_ALLOWED_HOSTS=terminal.example.com
   ```
   `GANXTERM_ALLOWED_HOSTS` takes host names, not `host:port`.
2. A `Caddyfile`:
   ```
   terminal.example.com {
       reverse_proxy 127.0.0.1:8765
   }
   ```
3. `docker compose up -d` (to apply the new settings) and start Caddy.

Keep the `127.0.0.1:` in the published port: only the proxy should reach
IguanaXterm. `terminal.example.com` must resolve to the machine, and ports 80
and 443 must reach Caddy for it to get a certificate.

Any HTTPS reverse proxy works, with three requirements. Caddy meets all of
them by default; **nginx needs them spelled out**:

- **WebSockets** — terminals and remote desktops run over them:
  `proxy_http_version 1.1;`, `proxy_set_header Upgrade $http_upgrade;`,
  `proxy_set_header Connection "upgrade";`.
- **`X-Forwarded-Proto: https`** — `proxy_set_header X-Forwarded-Proto $scheme;`.
- **Large uploads** — uploads stream straight through to the remote host, but
  nginx refuses any body over **1 MB** by default, which fails every photo:
  `client_max_body_size 0;` (or a real cap) and `proxy_request_buffering off;`,
  and raise `proxy_read_timeout` for slow links.

HTTPS is also what gives browsers on other machines the folder picker
([section 6](#6-folder-downloads)).

---

## 10. Troubleshooting

**The page says `Invalid host header`.** You opened `localhost` (or another
name). Use `http://127.0.0.1:8765/iguanaxterm` exactly (with your
`GANXTERM_PORT` if you changed it), or set up
[section 9](#9-reaching-iguanaxterm-from-other-machines).

**`port is already allocated` / `address already in use`.** Something else
uses 8765. Set `GANXTERM_PORT=8777` (any free port) in `.env` and run
`docker compose up -d` again.

**`permission denied … docker.sock` (Linux).** Add yourself to the `docker`
group (section 1) and log in again, or use `sudo docker`.

**`Cannot connect to Podman` / `unable to connect to Podman socket`
(Windows, macOS).** The Podman machine is not running: `podman machine start`.

**`podman compose` says no compose provider was found.** Install
`docker-compose` or `podman-compose` (section 1).

**The build fails downloading something.** The build needs github.com,
pypi.org and deb.debian.org. Behind a proxy, configure it for your engine
(Docker Desktop: *Settings → Resources → Proxies*; Podman: `HTTP_PROXY` /
`HTTPS_PROXY` in the Podman machine or your environment) and build again.

**A session times out or is refused.** Run the `manage.py probe` command from
[section 5](#5-connect-to-your-servers). *Refused* on Linux usually means the
server listens on 127.0.0.1 only — see
[Servers on the same computer, Linux](#servers-on-the-same-computer-linux).

**`attempt to write a readonly database` (Podman).** The data volume is owned
for a different user mapping — you switched between the `podman run` command
of section 6 and compose. Re-own it as shown at the end of section 6.

**A terminal opens and closes at once behind a proxy.** The proxy is not
passing WebSockets; see the nginx lines in section 9.

**`docker compose ps` shows `unhealthy`.** `docker compose logs iguanaxterm`
shows why; the last lines usually name the problem.

**Stored passwords stopped working after moving IguanaXterm.** The data
volume's `secret.key` encrypts them; restoring the database without it loses
them. Move the whole volume (section 7), or re-enter the passwords.

---

## 11. What was actually tested

On Linux x86_64 with rootless Podman 5.8 and Docker Compose v5 as its compose
provider, starting from a clean copy of the repository and an empty volume:

- `compose.yaml` (bridge network), with an SSH server in a container
  addressed by name: a live terminal, typing into it, SFTP browsing, two
  panes on one host.
- `compose.host-network.yaml` on another port, against an SSH server on the
  host's 127.0.0.1: the same checks.
- Section 9 with Caddy (its internal CA, on port 8443): the same checks
  through the proxy — terminals over WebSockets included — with the
  healthcheck healthy in that mode and plain HTTP to the container refused.
- The `podman run` command of section 6: saved files owned by the user and
  deletable without `sudo`; then the `podman unshare` fix to return to
  compose.
- Measured along the way: `podman compose` through the Docker Compose provider
  dropped the `:U` mount option, so the data volume (created by an earlier run
  under the other user mapping) came out read-only — which is why section 6
  uses `podman run` on Podman; and without `keep-id` the container could not
  write a host folder at all.
- The image **builds for `linux/arm64`** (Apple Silicon, ARM Linux) using
  prebuilt ARM wheels, and boots and signs in — checked
  under emulation.
- Every compose file and combination validates with Docker Compose
  (`docker compose config`).

On **Windows with Docker Desktop**, IguanaXterm was installed and run by hand
(2026-09-29) and works.

Not tested here: Docker Engine itself (its daemon needed root on the test
machine), `compose.downloads.yaml` with Docker, and macOS. The steps
for those follow Docker's and Podman's documented behaviour. If something
differs on your system, `manage.py probe` will show where, and an issue on
GitHub is welcome.

---

## 12. Windows without Docker (native installer)

For a computer without Docker: a normal Windows installer that runs
IguanaXterm directly, for **you, on this computer only**. It needs no
administrator rights, no container engine and no certificate.

**Get it:** `IguanaXterm-<version>-setup.exe`, attached to each
[release](https://github.com/El-Iguana/iguanaxterm_wapyt/releases) (built by
the `windows` GitHub Actions workflow). Windows 10 or 11, 64-bit.

**SmartScreen:** the installer is not code-signed yet, so Windows says
*"Windows protected your PC"*. Choose **More info → Run anyway**.

**What it does:**

- Installs to `%LOCALAPPDATA%\Programs\IguanaXterm`, with its own Python. It
  does not touch any Python you have.
- Adds **IguanaXterm**, **Reset IguanaXterm password** and **Uninstall
  IguanaXterm** to the Start menu. It can also add a desktop shortcut and
  start IguanaXterm when you sign in.
- Starting **IguanaXterm** runs the server in the background and opens
  `http://127.0.0.3:8765/iguanaxterm` in your default browser. If 8765 is
  taken, it uses the next free port. A tray icon offers **Open IguanaXterm**,
  **Show downloads folder**, **Show log folder** and **Quit IguanaXterm**.
  Starting it again while it runs just opens the browser.
- **Plain HTTP is safe here** because it listens on 127.0.0.3 only: nothing
  outside your computer can reach it. Use `127.0.0.3` exactly, not
  `localhost`, which answers *400 Invalid host header*. It is `127.0.0.3`
  rather than `127.0.0.1` so that signing in to another pytincture app, such
  as Monguana, does not sign you out of IguanaXterm: they share a cookie name,
  and browsers share cookies across ports. `GANXTERM_HOST` picks another
  address.

**First start:** sign in as **`admin`** with password **`change_me`** (a
message box reminds you). IguanaXterm then asks you to choose your own
password, and keeps asking each time it loads until you do. Forgot it later?
Run **Reset IguanaXterm password** from the Start menu.

**Folder downloads** (section 6) that a browser cannot save through a folder
picker go straight to your own **Downloads\IguanaXterm** folder, since the
"server" is your computer. Remote names Windows does not allow (`a:b`, `CON`,
a trailing dot) are saved with a `_`, and two names that would clash get
` (2)`.

**Your data** (accounts, saved sessions, known host keys, the key that
encrypts their passwords, logs) is in `%LOCALAPPDATA%\IguanaXterm`.
Upgrading keeps it: run the new installer over the old one. To back it up,
quit IguanaXterm from the tray and copy that folder. Keep `secret.key` with
`iguanaxterm.db`; without it, the saved passwords cannot be decrypted.

**Uninstall** from Start menu → Uninstall IguanaXterm, or Settings → Apps. It
asks whether to delete your data too; the default keeps it. Your downloaded
files are never removed.

**If it does not start**, a message box says so. The reason is in
`%LOCALAPPDATA%\IguanaXterm\logs\server.log` (tray → Show log folder).

**Command line** (PowerShell), for support or scripts:

```powershell
$x = "$env:LOCALAPPDATA\Programs\IguanaXterm"
& "$x\python\python.exe" "$x\app\iguanaxterm_launcher.py" --status   # prints the URL if running
& "$x\python\python.exe" "$x\app\iguanaxterm_launcher.py" --stop
& "$x\python\python.exe" "$x\app\iguanaxterm_launcher.py" --check    # start, check the login page, stop
```

**Tested:** every build runs on a GitHub Actions Windows runner, which runs
the unit tests on Windows, installs it silently, starts it, checks the login
page, the script MIME types (xterm, noVNC) and the downloads folder, stops it
and uninstalls it. The v2.0.0 installer was also installed and used by hand
on **Windows 11** (2026-09-29), including the tray icon, the first-run
message box and the Start-menu shortcuts.

