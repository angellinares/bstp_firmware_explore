// The patching itself. index.html uses it; tests/test_site.py runs the same file in Node.
// A .led file is one line of hex: everything here works on the bytes that hex stands for.

function fromHex(text) {
  const hex = text.trim();
  if (hex.length % 2 || /[^0-9a-fA-F]/.test(hex)) throw new Error("not a .led file: it is not one line of hex");
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}

function toHex(bytes) {
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out.toUpperCase();
}

async function sha256(bytes) {
  const subtle = (globalThis.crypto ?? require("crypto").webcrypto).subtle;
  return toHex(new Uint8Array(await subtle.digest("SHA-256", bytes))).toLowerCase();
}

// edits: [[offset, bytes to drop, new bytes as hex], ...] in rising order (tools/gen_site.py).
function applyEdits(bytes, edits) {
  const parts = [];
  let pos = 0;
  for (const [at, drop, hex] of edits) {
    parts.push(bytes.subarray(pos, at), fromHex(hex));
    pos = at + drop;
  }
  parts.push(bytes.subarray(pos));
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let n = 0;
  for (const p of parts) { out.set(p, n); n += p.length; }
  return out;
}

// -> "bstp-fw trig 1.0.0" if a build of ours made this file, else null.
function readStamp(bytes, patches) {
  const start = bytes.length - patches.stamp_from_end;
  if (start < 0) return null;
  let text = "";
  for (const b of bytes.subarray(start, start + patches.stamp_length)) {
    if (b === 0) break;
    text += String.fromCharCode(b);
  }
  return text.startsWith("bstp-fw ") ? text : null;
}

// -> the patched bytes, or throws: nothing leaves here unless it is the tested build.
async function patch(bytes, patches, mod) {
  if (await sha256(bytes) !== patches.stock_sha256) {
    const stamp = readStamp(bytes, patches);
    throw new Error(stamp
      ? `this file is already patched (${stamp}); use the original file from Arturia`
      : `this is not Arturia's ${patches.firmware} update file`);
  }
  const out = applyEdits(bytes, mod.edits);
  if (await sha256(out) !== mod.sha256) throw new Error("the result is not the tested build; nothing to download");
  return out;
}

if (typeof module !== "undefined") module.exports = { fromHex, toHex, sha256, applyEdits, readStamp, patch };
