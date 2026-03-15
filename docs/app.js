const PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v0.25.1/full/";
const SIZE_WARN   = 512 * 1024 * 1024;

let pyodide = null;
let isReady = false;

// DOM
const statusBar  = document.getElementById("status-bar");
const statusText = document.getElementById("status-text");
const progressWrap = document.getElementById("progress-wrap");
const progressBar  = document.getElementById("progress-bar");
const progressMsg  = document.getElementById("progress-msg");

// Boot
async function boot() {
  setStatus("loading", "Loading Python runtime…");
  pyodide = await loadPyodide({ indexURL: PYODIDE_CDN });

  setStatus("loading", "Installing packages…");
  await pyodide.loadPackage(["numpy", "Pillow"]);
  await pyodide.loadPackage("micropip");
  await pyodide.runPythonAsync(`
import micropip
await micropip.install("qrcode")
  `);

  setStatus("loading", "Initialising…");
  const resp = await fetch("qrext_core.py");
  const src  = await resp.text();
  await pyodide.runPythonAsync(src);

  isReady = true;
  setStatus("ready", "Ready");
  document.querySelectorAll("button[disabled]").forEach(b => b.disabled = false);
  setupDragDrop();
}

// Status
function setStatus(state, text) {
  statusBar.dataset.state = state;
  statusText.textContent  = text;
}

// Tabs
document.getElementById("tab-encode").addEventListener("click", () => switchTab("encode"));
document.getElementById("tab-decode").addEventListener("click", () => switchTab("decode"));

function switchTab(tab) {
  document.getElementById("tab-encode").classList.toggle("active", tab === "encode");
  document.getElementById("tab-decode").classList.toggle("active", tab === "decode");
  document.getElementById("panel-encode").classList.toggle("hidden", tab !== "encode");
  document.getElementById("panel-decode").classList.toggle("hidden", tab !== "decode");
}

// Progress
function showProgress(msg) {
  progressWrap.classList.remove("hidden");
  progressBar.style.width = "0%";
  progressMsg.textContent = msg;
}
function updateProgress(frac, msg) {
  progressBar.style.width = `${Math.round(frac * 100)}%`;
  progressMsg.textContent = msg;
}
function hideProgress() {
  progressWrap.classList.add("hidden");
}

// File picker helper
function setupPicker(btnId, inputId, pathId, cb) {
  const btn   = document.getElementById(btnId);
  const input = document.getElementById(inputId);
  const path  = document.getElementById(pathId);
  btn.addEventListener("click", () => input.click());
  input.addEventListener("change", e => {
    const f = e.target.files[0];
    if (!f) return;
    path.value = f.name;
    readFile(f, cb);
  });
}

function readFile(file, cb) {
  if (file.size > SIZE_WARN) {
    const mb = (file.size / 1024 / 1024).toFixed(0);
    if (!confirm(`This file is ${mb} MB. Large files may be slow or run out of browser memory. Continue?`))
      return;
  }
  const reader = new FileReader();
  reader.onload = e => cb(file, new Uint8Array(e.target.result));
  reader.readAsArrayBuffer(file);
}

// Drag & drop onto the whole window
function setupDragDrop() {
  const win = document.querySelector(".window");
  win.addEventListener("dragover", e => { e.preventDefault(); win.classList.add("drag-over"); });
  win.addEventListener("dragleave", () => win.classList.remove("drag-over"));
  win.addEventListener("drop", e => {
    e.preventDefault();
    win.classList.remove("drag-over");
    if (!isReady) return;
    const f = e.dataTransfer.files[0];
    if (!f) return;
    const activeTab = document.getElementById("tab-encode").classList.contains("active")
      ? "encode" : "decode";
    if (activeTab === "encode") {
      document.getElementById("encode-path").value = f.name;
      readFile(f, (file, bytes) => { encodeFile = { file, bytes }; });
    } else {
      document.getElementById("decode-path").value = f.name;
      readFile(f, (file, bytes) => {
        decodeFile = { file, bytes };
        previewFile(bytes);
      });
    }
  });
}

// ── Encode ────────────────────────────────────────────────────────────────────
let encodeFile = null;

setupPicker("encode-browse", "encode-input", "encode-path", (file, bytes) => {
  encodeFile = { file, bytes };
});

document.getElementById("encode-btn").addEventListener("click", async () => {
  if (!isReady || !encodeFile) {
    if (!encodeFile) showResult("encode-result", "No file selected.", true);
    return;
  }

  const autorun  = document.getElementById("encode-autorun").checked;
  const outname  = document.getElementById("encode-outname").value.trim();
  const filename = encodeFile.file.name;

  showProgress("Encoding…");
  setStatus("busy", `Encoding ${filename}…`);

  try {
    pyodide.globals.set("_input_bytes", encodeFile.bytes);
    pyodide.globals.set("_filename",    filename);
    pyodide.globals.set("_autorun",     autorun);

    const result = await pyodide.runPythonAsync(`
import js
def _cb(frac, msg): js.updateProgress(frac, msg)
encode(bytes(_input_bytes.to_py()), _filename, allow_autorun=_autorun, progress_cb=_cb)
    `);

    const pngBytes = result.toJs({ create_proxies: false });
    const blob     = new Blob([pngBytes], { type: "image/png" });
    const base     = outname || filename.replace(/\.[^.]+$/, "");
    const dlName   = `${base}.qrplus.png`;
    triggerDownload(URL.createObjectURL(blob), dlName);

    setStatus("ready", "Ready");
    showResult("encode-result", `✓ Encoded — ${dlName} (${formatSize(blob.size)})`);
  } catch (err) {
    setStatus("ready", "Ready");
    showResult("encode-result", `✗ ${err.message}`, true);
  }
  hideProgress();
});

// ── Decode ────────────────────────────────────────────────────────────────────
let decodeFile = null;

setupPicker("decode-browse", "decode-input", "decode-path", (file, bytes) => {
  decodeFile = { file, bytes };
  previewFile(bytes);
});

async function previewFile(bytes) {
  document.getElementById("decode-preview").classList.add("hidden");
  document.getElementById("decode-multipart-warn").classList.add("hidden");
  document.getElementById("decode-btn").disabled = false;
  try {
    pyodide.globals.set("_png_bytes", bytes);
    const meta = await pyodide.runPythonAsync(
      `peek_metadata(bytes(_png_bytes.to_py()))`
    );
    const m = meta.toJs({ create_proxies: false });
    document.getElementById("preview-name").textContent  = m.get("filename") || "Unknown";
    document.getElementById("preview-size").textContent  = formatSize(m.get("original_size") || 0);
    const parts = m.get("total_parts") || 1;
    document.getElementById("preview-parts").textContent =
      parts > 1 ? `Part ${(m.get("part_num")||0)+1} of ${parts}` : "Single file";
    document.getElementById("decode-preview").classList.remove("hidden");
    if (parts > 1) {
      document.getElementById("decode-multipart-warn").classList.remove("hidden");
      document.getElementById("decode-btn").disabled = true;
    }
  } catch (e) { /* silent — preview is optional */ }
}

document.getElementById("decode-btn").addEventListener("click", async () => {
  if (!isReady || !decodeFile) {
    if (!decodeFile) showResult("decode-result", "No file selected.", true);
    return;
  }

  const skip = document.getElementById("decode-skip-checksum").checked;

  showProgress("Scanning…");
  setStatus("busy", `Scanning ${decodeFile.file.name}…`);

  try {
    pyodide.globals.set("_png_bytes",     decodeFile.bytes);
    pyodide.globals.set("_skip_checksum", skip);

    const result = await pyodide.runPythonAsync(`
import js
def _cb(frac, msg): js.updateProgress(frac, msg)
filename, file_bytes = decode(bytes(_png_bytes.to_py()), skip_checksum=_skip_checksum, progress_cb=_cb)
(filename, file_bytes)
    `);

    const [filename, fileBytes] = result.toJs({ create_proxies: false });
    const blob = new Blob([fileBytes]);
    triggerDownload(URL.createObjectURL(blob), filename);

    setStatus("ready", "Ready");
    showResult("decode-result", `✓ Decoded — ${filename} (${formatSize(blob.size)})`);
  } catch (err) {
    setStatus("ready", "Ready");
    showResult("decode-result", `✗ ${err.message}`, true);
  }
  hideProgress();
});

// ── Credits ───────────────────────────────────────────────────────────────────
document.getElementById("credits-link").addEventListener("click", e => {
  e.preventDefault();
  document.getElementById("credits-overlay").classList.remove("hidden");
});
document.getElementById("credits-close").addEventListener("click", () => {
  document.getElementById("credits-overlay").classList.add("hidden");
});
document.getElementById("credits-overlay").addEventListener("click", e => {
  if (e.target === e.currentTarget)
    document.getElementById("credits-overlay").classList.add("hidden");
});

// ── Helpers ───────────────────────────────────────────────────────────────────
function triggerDownload(url, filename) {
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function formatSize(bytes) {
  if (bytes < 1024)           return `${bytes} B`;
  if (bytes < 1048576)        return `${(bytes/1024).toFixed(1)} KB`;
  if (bytes < 1073741824)     return `${(bytes/1048576).toFixed(1)} MB`;
  return `${(bytes/1073741824).toFixed(2)} GB`;
}

function showResult(id, msg, isError=false) {
  const el = document.getElementById(id);
  el.className = "result-box" + (isError ? " error" : "");
  el.textContent = msg;
  el.classList.remove("hidden");
}

// Boot
boot().catch(err => setStatus("error", `Failed to load: ${err.message}`));
