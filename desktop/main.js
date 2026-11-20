// IguanaXterm as a desktop app.
//
// Two modes, picked at start:
//
//   window   An IguanaXterm server is already answering (by default the
//            container on http://127.0.0.1:8765): open it in our own window.
//            Its sessions, users and RDP (guacd) are the ones used everywhere
//            else, and quitting the window leaves the server running.
//   local    Nothing answers: start service.py from this checkout on a free
//            loopback port, the way LizardKing does, with the native
//            launcher's data folder. Quitting the window stops that server.
//
// Which server: `--url=URL`, else IGUANAXTERM_URL, else "url" in
// <userData>/config.json, else http://127.0.0.1:8765/iguanaxterm. `--local`
// (or the URL "local") skips the probe and always starts a local server.
//
// Why a window at all, when the browser works: Chromium's File System Access
// dialogs refuse anything under /var, which is all of /home on Fedora Atomic;
// Electron lets us allow those paths (see allowHomePaths). And with no browser
// around the terminal, Ctrl+W/T/N/L reach the shell.
"use strict";

const { app, BrowserWindow, Menu, dialog, session, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const APP_PATH = "/iguanaxterm";
const DEFAULT_URL = `http://127.0.0.1:8765${APP_PATH}`;
// 127.0.0.1, not localhost (pytincture answers localhost with 400). The
// launcher's 127.0.0.3 is only about sharing a browser's cookie jar with
// other pytincture apps; this window has a cookie jar of its own.
const LOCAL_HOST = "127.0.0.1";
const PROBE_TIMEOUT_MS = 2_000;
const START_TIMEOUT_MS = 120_000;  // the first start builds pytincture's browser assets

const args = process.argv.slice(app.isPackaged ? 1 : 2);
const forceLocal = args.includes("--local");
const urlArg = (args.find((arg) => arg.startsWith("--url=")) || "").slice("--url=".length);

let server = null;
let serverLog = "";
let win = null;
let quitting = false;

// ── which server ─────────────────────────────────────────────────────────────

function configuredUrl() {
  if (urlArg) return urlArg;
  if (process.env.IGUANAXTERM_URL) return process.env.IGUANAXTERM_URL.trim();
  try {
    const config = JSON.parse(fs.readFileSync(path.join(app.getPath("userData"), "config.json"), "utf8"));
    if (config && typeof config.url === "string" && config.url.trim()) return config.url.trim();
  } catch { /* no config: the default */ }
  return DEFAULT_URL;
}

// GET <origin>/healthz: 200 means an IguanaXterm is up there.
function healthy(origin, timeout = PROBE_TIMEOUT_MS) {
  return new Promise((resolve) => {
    let target;
    try { target = new URL("/healthz", origin); } catch { resolve(false); return; }
    const client = target.protocol === "https:" ? https : http;
    const request = client.get(target, { timeout }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on("error", () => resolve(false));
    request.on("timeout", () => { request.destroy(); resolve(false); });
  });
}

// ── local server (the fallback) ──────────────────────────────────────────────

// Same folders as tools/windows/launcher.py, so a native install and this
// window share one set of sessions.
function dataDir() {
  if (process.env.GANXTERM_DATA_DIR) return process.env.GANXTERM_DATA_DIR;
  const base = process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
  return path.join(base, "iguanaxterm");
}

function downloadsDir() {
  return process.env.GANXTERM_DOWNLOAD_DIR || path.join(app.getPath("downloads"), "IguanaXterm");
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on("error", reject);
    probe.listen(0, LOCAL_HOST, () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

// The project's own venv first: signals reach Python directly. A desktop
// launcher has a short PATH, so look for uv where brew and the installer put it.
function pythonCommand() {
  const venv = path.join(ROOT, ".venv", "bin", "python");
  if (fs.existsSync(venv)) return [venv, []];
  const candidates = [
    ...(process.env.PATH || "").split(path.delimiter).map((dir) => path.join(dir, "uv")),
    "/home/linuxbrew/.linuxbrew/bin/uv",
    path.join(os.homedir(), ".local", "bin", "uv"),
    path.join(os.homedir(), ".cargo", "bin", "uv"),
  ];
  const uv = candidates.find((candidate) => fs.existsSync(candidate));
  if (!uv) throw new Error("Neither .venv/bin/python nor uv was found. Run `uv sync` in " + ROOT);
  return [uv, ["run", "python"]];
}

function startServer(port, data) {
  const [command, prefix] = pythonCommand();
  const env = {
    ...process.env,
    GANXTERM_DATA_DIR: data,
    GANXTERM_BIND: LOCAL_HOST,
    PORT: String(port),
    GANXTERM_CANONICAL_ORIGIN: `http://${LOCAL_HOST}:${port}`,
    GANXTERM_ALLOWED_HOSTS: LOCAL_HOST,
    GANXTERM_DOWNLOAD_DIR: downloadsDir(),
    PYTHONUNBUFFERED: "1",
  };
  // As the launcher: the first account is admin / change_me, not one left in
  // this environment by something else. Set to values rather than deleted:
  // service.py fills unset variables from a checkout's .env (a developer's
  // has its own admin), and an empty GANXTERM_ADMIN_PASS means the default.
  // RDP stays as configured: set GANXTERM_GUACD to a guacd this machine can
  // reach to have it.
  env.GANXTERM_ADMIN_USER = "admin";
  env.GANXTERM_ADMIN_PASS = "";
  delete env.GANXTERM_DOWNLOAD_HOST_DIR;
  fs.mkdirSync(data, { recursive: true });
  server = spawn(command, [...prefix, "service.py"], {
    cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"],
  });
  const keep = (chunk) => { serverLog = (serverLog + chunk).slice(-8000); };
  server.stdout.on("data", keep);
  server.stderr.on("data", keep);
  server.on("exit", (code, signal) => {
    server = null;
    if (!quitting) {
      dialog.showErrorBox("IguanaXterm stopped",
        `The local IguanaXterm server exited (${signal || `code ${code}`}).\n\n${serverLog.slice(-3000)}`);
      app.quit();
    }
  });
}

async function waitForServer(origin) {
  const deadline = Date.now() + START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!server) throw new Error("The server exited while starting.");
    if (await healthy(origin, 1000)) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`The server did not answer within ${START_TIMEOUT_MS / 1000} s.`);
}

function stopServer() {
  if (!server) return;
  const child = server;
  child.kill("SIGTERM");  // uvicorn closes the live SSH/RDP connections
  setTimeout(() => { if (child.exitCode === null) child.kill("SIGKILL"); }, 10_000).unref();
}

async function startLocal() {
  const data = dataDir();
  const firstRun = !fs.existsSync(path.join(data, "iguanaxterm.db"));
  const port = await freePort();
  const origin = `http://${LOCAL_HOST}:${port}`;
  startServer(port, data);
  await waitForServer(origin);
  return { url: `${origin}${APP_PATH}`, firstRun };
}

// ── the window ───────────────────────────────────────────────────────────────

// Chromium's save and folder dialogs refuse "system" paths, and /var is one:
// on Fedora Atomic every home folder is under /var/home. Allow what is under
// the user's home (either spelling) for our own page; ask about the rest.
function allowHomePaths(origin) {
  const homes = new Set([os.homedir()]);
  try { homes.add(fs.realpathSync(os.homedir())); } catch { /* keep the one */ }
  const inHome = (target) => [...homes].some((home) => target === home || target.startsWith(home + path.sep));
  session.defaultSession.on("file-system-access-restricted", async (_event, details, callback) => {
    if (details.origin !== origin) { callback("deny"); return; }
    if (inHome(details.path)) { callback("allow"); return; }
    const { response } = await dialog.showMessageBox(win, {
      type: "question",
      title: "IguanaXterm",
      message: `Use ${details.path}?`,
      detail: "Your system treats this folder as a system folder.",
      buttons: ["Use it", "Choose another", "Cancel"],
      defaultId: 1,
      cancelId: 2,
    });
    callback(["allow", "tryAgain", "deny"][response]);
  });
}

function createWindow(url, title) {
  const origin = new URL(url).origin;
  allowHomePaths(origin);
  win = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 640,
    minHeight: 420,
    title,
    icon: path.join(__dirname, "icon.png"),
    backgroundColor: "#0f172a",
    show: false,
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  win.once("ready-to-show", () => win.show());
  // The title says which server this window is on.
  win.on("page-title-updated", (event) => event.preventDefault());

  // Links (About, release notes) open in the real browser; the window itself
  // never leaves IguanaXterm.
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:\/\//.test(target) && !target.startsWith(origin)) shell.openExternal(target);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, target) => {
    if (!target.startsWith(origin)) {
      event.preventDefault();
      if (/^https?:\/\//.test(target)) shell.openExternal(target);
    }
  });
  // No menu bar, so every other key reaches the page (and the terminal: Ctrl+R
  // is bash's history search, so reload is F5 only).
  win.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return;
    if (input.key === "F12" || (input.control && input.shift && input.key.toLowerCase() === "i")) {
      win.webContents.toggleDevTools();
      event.preventDefault();
    } else if (input.key === "F5") {
      win.webContents.reload();
      event.preventDefault();
    }
  });
  win.on("closed", () => { win = null; });
  win.loadURL(url);
}

// ── start ────────────────────────────────────────────────────────────────────

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    try {
      const wanted = configuredUrl();
      if (!forceLocal && wanted !== "local" && await healthy(new URL(wanted).origin)) {
        createWindow(wanted, "IguanaXterm");
        return;
      }
      const { url, firstRun } = await startLocal();
      createWindow(url, "IguanaXterm (local)");
      if (firstRun) {
        dialog.showMessageBox(win, {
          type: "info",
          title: "IguanaXterm — first run",
          message: "Sign in with admin / change_me",
          detail: "No IguanaXterm server was running, so this window started its own, "
            + `with its own sessions in ${dataDir()}. IguanaXterm will ask you to `
            + "choose your own password until you do.",
        });
      }
    } catch (error) {
      console.error(`IguanaXterm could not start: ${error.stack || error}\n${serverLog.slice(-3000)}`);
      quitting = true;
      stopServer();
      dialog.showErrorBox("IguanaXterm could not start", `${error.message}\n\n${serverLog.slice(-3000)}`);
      app.quit();
    }
  });

  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", () => { quitting = true; stopServer(); });
}
