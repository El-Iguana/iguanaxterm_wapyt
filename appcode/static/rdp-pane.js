// An RDP desktop in a pane (#58), on Apache guacamole-common-js 1.6.
//
// IxRDP deliberately answers the same calls and events as noVNC's RFB, so the
// app's pane code drives a VNC or an RDP desktop the same way:
//
//   calls:  disconnect(), focus(), sendCtrlAltDel(), sendKey(keysym),
//           clipboardPasteFrom(text); RDP only: setMuted(bool)
//   events: "connect", "disconnect" {clean}, "securityfailure" {reason},
//           "clipboard" {text}; RDP only: "printed" {filename, bytes},
//           "printfailed" {reason}
//
// The relay (/ws/rdp/<id>, rdp_ws.py) does the Guacamole handshake with guacd
// itself, so this page never sees the login: it only draws and sends input.
//
// Loaded on first use, after guacamole-common.min.js, by the app.
(function () {
  "use strict";

  const KEY = { CTRL: 0xffe3, ALT: 0xffe9, DEL: 0xffff, SUPER: 0xffeb, V: 0x0076 };
  const RESIZE_DEBOUNCE_MS = 120;   // same idea as the terminal's fit debounce
  const STATE_CONNECTED = 3;
  const STATE_DISCONNECTED = 5;
  const MAX_DENSITY = 3;            // beyond this the pixels cost more than they show
  const PDF_MAX_BYTES = 100 * 1024 * 1024;   // a print job held in the tab (#77)
  const ACK_OK = 0x0000;
  const ACK_FORBIDDEN = 0x0303;     // Guacamole.Status.Code.CLIENT_FORBIDDEN
  const ACK_TOO_LARGE = 0x030D;     // CLIENT_TOO_LARGE

  // A print job's name as a safe download name: no path, ends in .pdf.
  function pdfName(name) {
    let base = String(name || "").split(/[\\/]/).pop().replace(/[\x00-\x1f<>:"|?*]/g, "_").trim();
    if (!base || base === ".pdf") base = "document.pdf";
    return /\.pdf$/i.test(base) ? base : base + ".pdf";
  }

  // Pixels per CSS pixel to ask the desktop for (#74). Off (the default), the
  // desktop is CSS-sized and the browser stretches it on a high-DPI screen:
  // normal size, a little soft. On, a 2x screen gets twice the pixels: crisp,
  // but Windows draws at half the size, because guacd 1.6 never passes a
  // scale factor to Windows (its DPI only resizes the desktop). The handshake
  // always says 96 DPI: anything higher and guacd scales the size back down
  // (guac_rdp_suggest_resolution), undoing this.
  function density(hidpi) {
    if (!hidpi) return 1;
    return Math.min(MAX_DENSITY, Math.max(1, window.devicePixelRatio || 1));
  }

  class IxRDP extends EventTarget {
    // options: {muted, hidpi, printing}
    constructor(container, url, options) {
      super();
      options = options || {};
      this.container = container;
      this.muted = !!options.muted;
      this.hidpi = !!options.hidpi;
      this.printing = !!options.printing;
      this._closedByUs = false;
      this._reported = false;
      this._refusal = "";

      const tunnel = new Guacamole.WebSocketTunnel(url);
      const client = new Guacamole.Client(tunnel);
      this.client = client;
      this.tunnel = tunnel;             // for tests and the console

      const display = client.getDisplay();
      const element = display.getElement();
      element.classList.add("ix-rdp-display");
      container.appendChild(element);
      container.ixRdp = this;          // for tests and the console

      // guacamole-common-js 1.6.0 decodes streamed images with ImageDecoder and
      // never closes the VideoFrames it gets back, which Chrome reports as an
      // error on every frame (and warns can stall). Without ImageDecoder it
      // takes its older path, Image over a data URI, which keeps the drawing
      // order. Hidden only for the length of this call.
      const drawStream = display.drawStream;
      display.drawStream = function () {
        const saved = window.ImageDecoder;
        window.ImageDecoder = undefined;
        try {
          return drawStream.apply(this, arguments);
        } finally {
          window.ImageDecoder = saved;
        }
      };
      this.display = display;

      // Mouse (and touch) on the display; positions are unscaled by guacamole.
      const mouse = new Guacamole.Mouse(element);
      mouse.onEach(["mousedown", "mouseup", "mousemove"], (event) => {
        client.sendMouseState(event.state, true);
      });
      try {
        const touch = new Guacamole.Mouse.Touchscreen(element);
        touch.onEach(["mousedown", "mouseup", "mousemove"], (event) => {
          client.sendMouseState(event.state, true);
        });
      } catch (_e) { /* no touch support in this browser */ }
      this.mouse = mouse;

      // The keyboard listens on the pane's own element, so keys reach the
      // desktop only while it has focus: typing in another pane never does.
      const keyboard = new Guacamole.Keyboard(container);
      keyboard.onkeydown = (keysym) => {
        client.sendKeyEvent(1, keysym);
        return false;
      };
      keyboard.onkeyup = (keysym) => client.sendKeyEvent(0, keysym);
      this.keyboard = keyboard;
      container.addEventListener("mousedown", () => this.focus());

      // Sound (#73). guacamole's own RawAudioPlayer plays guacd's PCM; muting
      // drops the packets rather than pausing, so unmuting is live, not a
      // backlog. Browsers keep an AudioContext suspended until the page gets
      // a click or a key, so the pane's first one wakes it.
      // ``audio`` counts what arrived and what played, for tests and the console.
      this.audio = { streams: 0, received: 0, played: 0 };
      client.onaudio = (stream, mimetype) => {
        const player = Guacamole.AudioPlayer.getInstance(stream, mimetype);
        if (!player) return null;
        this.audio.streams++;
        const play = stream.onblob;
        stream.onblob = (data) => {
          this.audio.received++;
          if (this.muted) return;
          this.audio.played++;
          play(data);
        };
        return player;
      };
      const wake = () => {
        const context = Guacamole.AudioContextFactory.getAudioContext();
        if (context && context.state === "suspended") context.resume().catch(() => {});
      };
      container.addEventListener("mousedown", wake);
      container.addEventListener("keydown", wake);

      display.onresize = () => this._scale();
      this._observer = new ResizeObserver(() => {
        this._scale();
        clearTimeout(this._resizeTimer);
        this._resizeTimer = setTimeout(() => this._sendSize(), RESIZE_DEBOUNCE_MS);
      });
      this._observer.observe(container);

      client.onstatechange = (state) => {
        if (state === STATE_CONNECTED) {
          this._scale();
          this.dispatchEvent(new CustomEvent("connect"));
        } else if (state === STATE_DISCONNECTED) {
          this._finish();
        }
      };
      // The relay sends guacd's errors already put in words (guac.status_message).
      const refuse = (status) => {
        this._refusal = (status && status.message) || "The remote desktop refused the connection.";
        this.dispatchEvent(new CustomEvent("securityfailure", { detail: { reason: this._refusal } }));
      };
      client.onerror = refuse;
      tunnel.onerror = refuse;

      client.onclipboard = (stream, mimetype) => {
        if (!/^text\//.test(mimetype)) { stream.sendAck("Unsupported", 0x0100); return; }
        const reader = new Guacamole.StringReader(stream);
        let text = "";
        reader.ontext = (chunk) => { text += chunk; };
        reader.onend = () => {
          this.dispatchEvent(new CustomEvent("clipboard", { detail: { text } }));
        };
      };

      // Printing (#77): guacd turns a Windows print job into a PDF and sends
      // it as a file stream, waiting for an ack after the announcement and
      // after every chunk. Nothing else may arrive as a file: the drive's own
      // channel is shut, so anything that isn't a PDF from a printing site is
      // refused.
      client.onfile = (stream, mimetype, filename) => this._receivePrint(stream, mimetype, filename);

      const rect = container.getBoundingClientRect();
      const scale = density(this.hidpi);
      const width = Math.max(320, Math.round((rect.width || 1024) * scale));
      const height = Math.max(240, Math.round((rect.height || 768) * scale));
      this._lastSize = [width, height];
      client.connect(`w=${width}&h=${height}&dpi=96`);
    }

    _receivePrint(stream, mimetype, filename) {
      if (!this.printing || mimetype !== "application/pdf") {
        stream.sendAck("Not accepted", ACK_FORBIDDEN);
        return;
      }
      const name = pdfName(filename);
      const chunks = [];
      let bytes = 0;
      stream.onblob = (data) => {
        const raw = atob(data);
        bytes += raw.length;
        if (bytes > PDF_MAX_BYTES) {
          stream.onblob = null;
          stream.onend = null;
          stream.sendAck("Too large", ACK_TOO_LARGE);
          this.dispatchEvent(new CustomEvent("printfailed", { detail: {
            reason: `${name} is larger than ${PDF_MAX_BYTES / (1024 * 1024)} MiB, so it wasn't downloaded.` } }));
          return;
        }
        const chunk = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) chunk[i] = raw.charCodeAt(i);
        chunks.push(chunk);
        stream.sendAck("OK", ACK_OK);
      };
      stream.onend = () => {
        const url = URL.createObjectURL(new Blob(chunks, { type: "application/pdf" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = name;
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        this.dispatchEvent(new CustomEvent("printed", { detail: { filename: name, bytes } }));
      };
      stream.sendAck("Ready", ACK_OK);
    }

    _finish() {
      if (this._reported) return;
      this._reported = true;
      this._observer.disconnect();
      clearTimeout(this._resizeTimer);
      this.keyboard.onkeydown = null;
      this.keyboard.onkeyup = null;
      const clean = this._closedByUs;
      this.dispatchEvent(new CustomEvent("disconnect", { detail: { clean } }));
    }

    // Fit the remote screen inside the pane, keeping its aspect ratio.
    _scale() {
      const width = this.display.getWidth();
      const height = this.display.getHeight();
      const box = this.container.getBoundingClientRect();
      if (!width || !height || !box.width || !box.height) return;
      const scale = Math.min(box.width / width, box.height / height);
      this.display.scale(scale);
      const element = this.display.getElement();
      element.style.left = `${Math.max(0, (box.width - width * scale) / 2)}px`;
      element.style.top = `${Math.max(0, (box.height - height * scale) / 2)}px`;
    }

    // The remote desktop follows the tile (resize-method display-update).
    _sendSize() {
      const box = this.container.getBoundingClientRect();
      if (box.width < 100 || box.height < 100) return;  // hidden, or mid-drag
      const scale = density(this.hidpi);
      const width = Math.round(box.width * scale);
      const height = Math.round(box.height * scale);
      if (this._lastSize[0] === width && this._lastSize[1] === height) return;
      this._lastSize = [width, height];
      this.client.sendSize(width, height);
    }

    disconnect() {
      this._closedByUs = true;
      try { this.client.disconnect(); } catch (_e) { /* already gone */ }
      this._finish();
    }

    focus() {
      this.container.focus({ preventScroll: true });
    }

    sendKey(keysym) {
      this.client.sendKeyEvent(1, keysym);
      this.client.sendKeyEvent(0, keysym);
    }

    // Keys the browser or the OS keeps for itself, sent as a chord.
    sendChord(keysyms) {
      for (const keysym of keysyms) this.client.sendKeyEvent(1, keysym);
      for (const keysym of [...keysyms].reverse()) this.client.sendKeyEvent(0, keysym);
    }

    sendCtrlAltDel() {
      this.sendChord([KEY.CTRL, KEY.ALT, KEY.DEL]);
    }

    // The app's Ctrl+V: it has already put this computer's clipboard on the
    // desktop; now the keystroke. Sent as a whole chord, because the page
    // swallowed the V and guacamole may not have passed a held Ctrl on.
    pasteShortcut() {
      this.sendChord([KEY.CTRL, KEY.V]);
    }

    sendWindowsKey() {
      this.sendKey(KEY.SUPER);
    }

    clipboardPasteFrom(text) {
      const stream = this.client.createClipboardStream("text/plain");
      const writer = new Guacamole.StringWriter(stream);
      writer.sendText(text);
      writer.sendEnd();
    }

    setMuted(muted) {
      this.muted = !!muted;
    }

    fullscreen() {
      const target = this.container.closest(".ix-pane") || this.container;
      if (document.fullscreenElement) document.exitFullscreen();
      else if (target.requestFullscreen) target.requestFullscreen();
    }
  }

  window.IxRDP = IxRDP;
})();
