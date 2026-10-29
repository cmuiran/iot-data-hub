// QR Code encoder (ISO/IEC 18004): byte mode, error correction level M, versions 1-10
// (up to 213 bytes: a claim link fits in version 4-6). After the reference algorithm by
// Project Nayuki (MIT). Output: an SVG element, or the module matrix for tests.

const ECC_PER_BLOCK = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26];    // level M
const BLOCKS = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5];
const MAX_VERSION = 10;

function rawDataModules(ver) {
  let result = (16 * ver + 128) * ver + 64;
  if (ver >= 2) {
    const align = Math.floor(ver / 7) + 2;
    result -= (25 * align - 10) * align - 55;
    if (ver >= 7) result -= 36;
  }
  return result;
}

const dataCodewords = (ver) => Math.floor(rawDataModules(ver) / 8) - ECC_PER_BLOCK[ver] * BLOCKS[ver];

function gfMul(x, y) {
  let z = 0;
  for (let i = 7; i >= 0; i -= 1) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z & 0xff;
}

function rsDivisor(degree) {
  const result = new Array(degree).fill(0);
  result[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i += 1) {
    for (let j = 0; j < result.length; j += 1) {
      result[j] = gfMul(result[j], root);
      if (j + 1 < result.length) result[j] ^= result[j + 1];
    }
    root = gfMul(root, 0x02);
  }
  return result;
}

function rsRemainder(data, divisor) {
  const result = divisor.map(() => 0);
  for (const b of data) {
    const factor = b ^ result.shift();
    result.push(0);
    divisor.forEach((coef, i) => { result[i] ^= gfMul(coef, factor); });
  }
  return result;
}

function alignmentPositions(ver, size) {
  if (ver === 1) return [];
  const count = Math.floor(ver / 7) + 2;
  const step = Math.floor((ver * 8 + count * 3 + 5) / (count * 4 - 4)) * 2;
  const result = [6];
  for (let pos = size - 7; result.length < count; pos -= step) result.splice(1, 0, pos);
  return result;
}

/** The module matrix (true = dark) of `text` (UTF-8). */
export function qrMatrix(text) {
  const bytes = [...new TextEncoder().encode(text)];
  let ver = 1;
  while (ver <= MAX_VERSION && 4 + (ver < 10 ? 8 : 16) + bytes.length * 8 > dataCodewords(ver) * 8) {
    ver += 1;
  }
  if (ver > MAX_VERSION) throw new Error("text too long for a QR code");
  const size = ver * 4 + 17;
  const modules = Array.from({ length: size }, () => new Array(size).fill(false));
  const fixed = Array.from({ length: size }, () => new Array(size).fill(false));
  const set = (x, y, dark) => { modules[y][x] = dark; fixed[y][x] = true; };

  // function patterns
  for (let i = 0; i < size; i += 1) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
  for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
    for (let dy = -4; dy <= 4; dy += 1) {
      for (let dx = -4; dx <= 4; dx += 1) {
        const x = cx + dx, y = cy + dy;
        const dist = Math.max(Math.abs(dx), Math.abs(dy));
        if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, dist !== 2 && dist !== 4);
      }
    }
  }
  const align = alignmentPositions(ver, size);
  for (let i = 0; i < align.length; i += 1) {
    for (let j = 0; j < align.length; j += 1) {
      if ((i === 0 && j === 0) || (i === 0 && j === align.length - 1) ||
          (i === align.length - 1 && j === 0)) continue;
      for (let dy = -2; dy <= 2; dy += 1) {
        for (let dx = -2; dx <= 2; dx += 1) {
          set(align[i] + dx, align[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
        }
      }
    }
  }
  const formatBits = (mask) => {
    const data = (0 << 3) | mask;                    // level M = 00
    let rem = data;
    for (let i = 0; i < 10; i += 1) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    const bit = (i) => ((bits >>> i) & 1) !== 0;
    for (let i = 0; i <= 5; i += 1) set(8, i, bit(i));
    set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
    for (let i = 9; i < 15; i += 1) set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i += 1) set(size - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i += 1) set(8, size - 15 + i, bit(i));
    set(8, size - 8, true);
  };
  formatBits(0);
  if (ver >= 7) {
    let rem = ver;
    for (let i = 0; i < 12; i += 1) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const bits = (ver << 12) | rem;
    for (let i = 0; i < 18; i += 1) {
      const dark = ((bits >>> i) & 1) !== 0;
      const a = size - 11 + (i % 3), b = Math.floor(i / 3);
      set(a, b, dark); set(b, a, dark);
    }
  }

  // data codewords: mode, count, bytes, terminator, padding
  const bits = [];
  const push = (value, len) => { for (let i = len - 1; i >= 0; i -= 1) bits.push((value >>> i) & 1); };
  push(0b0100, 4);
  push(bytes.length, ver < 10 ? 8 : 16);
  bytes.forEach((b) => push(b, 8));
  const capacity = dataCodewords(ver) * 8;
  push(0, Math.min(4, capacity - bits.length));
  push(0, (8 - (bits.length % 8)) % 8);
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) push(pad, 8);
  const data = [];
  for (let i = 0; i < bits.length; i += 8) data.push(parseInt(bits.slice(i, i + 8).join(""), 2));

  // error correction, interleaved
  const blocks = BLOCKS[ver], eccLen = ECC_PER_BLOCK[ver];
  const raw = Math.floor(rawDataModules(ver) / 8);
  const shortBlocks = blocks - (raw % blocks);
  const shortLen = Math.floor(raw / blocks);
  const divisor = rsDivisor(eccLen);
  const parts = [];
  for (let i = 0, k = 0; i < blocks; i += 1) {
    const dat = data.slice(k, k + shortLen - eccLen + (i < shortBlocks ? 0 : 1));
    k += dat.length;
    const ecc = rsRemainder(dat, divisor);
    if (i < shortBlocks) dat.push(0);
    parts.push(dat.concat(ecc));
  }
  const codewords = [];
  for (let i = 0; i < parts[0].length; i += 1) {
    parts.forEach((block, j) => {
      if (i !== shortLen - eccLen || j >= shortBlocks) codewords.push(block[i]);
    });
  }

  // zigzag placement
  let n = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert += 1) {
      for (let j = 0; j < 2; j += 1) {
        const x = right - j;
        const upward = ((right + 1) & 2) === 0;
        const y = upward ? size - 1 - vert : vert;
        if (!fixed[y][x] && n < codewords.length * 8) {
          modules[y][x] = ((codewords[n >>> 3] >>> (7 - (n & 7))) & 1) !== 0;
          n += 1;
        }
      }
    }
  }

  // the mask with the lowest penalty
  const masks = [
    (x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0,
    (x, y) => (x + y) % 3 === 0, (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
    (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0, (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
    (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0];
  const apply = (mask) => {
    for (let y = 0; y < size; y += 1) {
      for (let x = 0; x < size; x += 1) if (!fixed[y][x] && masks[mask](x, y)) modules[y][x] = !modules[y][x];
    }
  };
  let best = 0, bestScore = Infinity;
  for (let mask = 0; mask < 8; mask += 1) {
    apply(mask);
    formatBits(mask);
    const score = penalty(modules);
    if (score < bestScore) { best = mask; bestScore = score; }
    apply(mask);                                   // undo (xor)
  }
  apply(best);
  formatBits(best);
  return modules;
}

function penalty(m) {
  const size = m.length;
  let score = 0, dark = 0;
  for (let a = 0; a < size; a += 1) {
    for (const line of [(i) => m[a][i], (i) => m[i][a]]) {      // runs of 5+ in rows and columns
      let run = 1;
      for (let i = 1; i <= size; i += 1) {
        if (i < size && line(i) === line(i - 1)) run += 1;
        else { if (run >= 5) score += run - 2; run = 1; }
      }
    }
  }
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      if (m[y][x]) dark += 1;
      if (x < size - 1 && y < size - 1 && m[y][x] === m[y][x + 1] && m[y][x] === m[y + 1][x] &&
          m[y][x] === m[y + 1][x + 1]) score += 3;
    }
  }
  const total = size * size;
  score += Math.floor(Math.abs(dark * 20 - total * 10) / total) * 10;
  return score;
}

/** An <svg> of the QR code with a 4-module quiet zone. */
export function qrSvg(text, px = 120, label = "") {
  const m = qrMatrix(text);
  const size = m.length + 8;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
  svg.setAttribute("width", px);
  svg.setAttribute("height", px);
  svg.setAttribute("shape-rendering", "crispEdges");
  svg.setAttribute("role", "img");
  if (label) svg.setAttribute("aria-label", label);
  const bg = document.createElementNS(ns, "rect");
  bg.setAttribute("width", size); bg.setAttribute("height", size); bg.setAttribute("fill", "#fff");
  const path = document.createElementNS(ns, "path");
  let d = "";
  m.forEach((row, y) => row.forEach((dark, x) => { if (dark) d += `M${x + 4} ${y + 4}h1v1h-1z`; }));
  path.setAttribute("d", d);
  path.setAttribute("fill", "#14161B");
  svg.append(bg, path);
  return svg;
}
