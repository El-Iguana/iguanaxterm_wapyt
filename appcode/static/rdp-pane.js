// An RDP desktop in a pane (#58), on Apache guacamole-common-js 1.6.
//
// IxRDP deliberately answers the same calls and events as noVNC's RFB, so the
// app's pane code drives a VNC or an RDP desktop the same way:
//
//   calls:  disconnect(), focus(), sendCtrlAltDel(), sendKey(keysym),
//           clipboardPasteFrom(text)
//   events: "connect", "disconnect" {clean}, "securityfailure" {reason},
//           "clipboard" {text}
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

  class IxRDP extends EventTarget {
    constructor(container, url) {
      super();
      this.container = container;
      this._closedByUs = false;
      this._reported = false;
      this._refusal = "";

      const tunnel = new Guacamole.WebSocketTunnel(url);
      const client = new Guacamole.Client(tunnel);
      this.client = client;

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

      const rect = container.getBoundingClientRect();
      const width = Math.max(320, Math.round(rect.width || 1024));
      const height = Math.max(240, Math.round(rect.height || 768));
      const dpi = Math.round(96 * (window.devicePixelRatio || 1));
      this._lastSize = [width, height];
      client.connect(`w=${width}&h=${height}&dpi=${dpi}`);
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
      const width = Math.round(box.width);
      const height = Math.round(box.height);
      if (width < 100 || height < 100) return;          // hidden, or mid-drag
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

    fullscreen() {
      const target = this.container.closest(".ix-pane") || this.container;
      if (document.fullscreenElement) document.exitFullscreen();
      else if (target.requestFullscreen) target.requestFullscreen();
    }
  }

  window.IxRDP = IxRDP;
})();
