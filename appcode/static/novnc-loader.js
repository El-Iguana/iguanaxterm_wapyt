// Hands noVNC's RFB class to the Pyodide side. An ES module cannot be imported
// from Python directly, and innerHTML never runs a script, so the app loads this
// file with a <script type="module"> tag the first time a desktop opens.
import RFB from "/novnc/core/rfb.js";

window.IxRFB = RFB;
