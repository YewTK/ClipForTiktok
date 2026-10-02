// TikTok Smooth patcher — rewrites MP4/MOV container metadata only; video and audio data are never re-encoded.
// The patch (Smooth V1):
//   1. mvhd becomes version 1 with duration = 0xFFFFFFFFFFFFFFFF
//   2. the video trak gets a udta/uuid marker box
//   3. moov is placed before the media data (fast start) and chunk offsets are shifted to match
(function (root) {
  'use strict';

  const MARKER_UUID = [0x7f, 0x14, 0x3a, 0xb9, 0xd2, 0x52, 0x4d, 0x63, 0x89, 0x4f, 0x69, 0xd8, 0xc2, 0xa7, 0x0e, 0x15];
  const MARKER_PAYLOAD = [0x01];

  const u16 = (b, o) => (b[o] << 8) | b[o + 1];
  const u32 = (b, o) => ((b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]) >>> 0;
  const u64 = (b, o) => u32(b, o) * 4294967296 + u32(b, o + 4);
  const fourcc = (b, o) => String.fromCharCode(b[o], b[o + 1], b[o + 2], b[o + 3]);
  const setU32 = (b, o, v) => { b[o] = v >>> 24; b[o + 1] = (v >>> 16) & 255; b[o + 2] = (v >>> 8) & 255; b[o + 3] = v & 255; };
  const setU64 = (b, o, v) => { setU32(b, o, Math.floor(v / 4294967296)); setU32(b, o + 4, v % 4294967296); };

  function concat(parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }

  function makeBox(type, parts) {
    const body = concat(parts);
    const out = new Uint8Array(8 + body.length);
    setU32(out, 0, out.length);
    for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
    out.set(body, 8);
    return out;
  }

  // Child boxes of b[start, end). Boxes inside moov always use 32-bit sizes in practice.
  function boxes(b, start, end) {
    const out = [];
    let p = start;
    while (p + 8 <= end) {
      let size = u32(b, p);
      if (size === 0) size = end - p;
      if (size < 8 || p + size > end) throw new Error('โครงสร้างไฟล์เสียหาย (box ขนาดผิดปกติ)');
      out.push({ type: fourcc(b, p + 4), start: p, body: p + 8, end: p + size });
      p += size;
    }
    return out;
  }

  function find(b, parent, path) {
    let cur = parent;
    for (const type of path) {
      cur = boxes(b, cur.body, cur.end).find((x) => x.type === type);
      if (!cur) return null;
    }
    return cur;
  }

  // Locate top-level boxes without reading the media data. read(pos, len) -> Promise<Uint8Array>
  async function scanTopLevel(read, fileSize) {
    const found = [];
    let p = 0;
    while (p + 8 <= fileSize) {
      const h = await read(p, Math.min(16, fileSize - p));
      let size = u32(h, 0);
      const type = fourcc(h, 4);
      if (size === 1) size = u64(h, 8);
      else if (size === 0) size = fileSize - p;
      if (size < 8) throw new Error('ไฟล์นี้ไม่ใช่ MP4/MOV ที่ถูกต้อง');
      found.push({ type, start: p, end: Math.min(p + size, fileSize) });
      p += size;
    }
    if (!found.length || !found.some((x) => x.type === 'ftyp')) throw new Error('ไฟล์นี้ไม่ใช่ MP4/MOV');
    if (found.some((x) => x.type === 'moof')) throw new Error('ไม่รองรับไฟล์แบบ fragmented MP4');
    const moov = found.find((x) => x.type === 'moov');
    if (!moov) throw new Error('ไม่พบข้อมูล moov ในไฟล์ (ไฟล์อาจบันทึกไม่สมบูรณ์)');
    const mdat = found.find((x) => x.type === 'mdat');
    // moov already ahead of the media data stays where it is; otherwise it moves in front of mdat
    const insertAt = mdat && mdat.start < moov.start ? mdat.start : moov.start;
    return { moov, insertAt };
  }

  function trackInfo(b, trak) {
    const hdlr = find(b, trak, ['mdia', 'hdlr']);
    const mdhd = find(b, trak, ['mdia', 'mdhd']);
    const stbl = find(b, trak, ['mdia', 'minf', 'stbl']);
    if (!hdlr || !mdhd || !stbl) return null;
    const info = { handler: fourcc(b, hdlr.body + 8) };
    const v1 = b[mdhd.body] === 1;
    const timescale = u32(b, mdhd.body + (v1 ? 20 : 12));
    const duration = v1 ? u64(b, mdhd.body + 24) : u32(b, mdhd.body + 16);
    info.seconds = timescale ? duration / timescale : 0;

    const stsd = find(b, { body: stbl.body, end: stbl.end }, ['stsd']);
    if (stsd) {
      const e = stsd.body + 8;
      info.codec = fourcc(b, e + 4);
      if (info.handler === 'vide') { info.width = u16(b, e + 32); info.height = u16(b, e + 34); }
      if (info.handler === 'soun') { info.channels = u16(b, e + 24); info.sampleRate = u16(b, e + 32); }
    }
    const stsz = find(b, { body: stbl.body, end: stbl.end }, ['stsz']);
    if (stsz) {
      const uniform = u32(b, stsz.body + 4);
      info.samples = u32(b, stsz.body + 8);
      let bytes = uniform * info.samples;
      if (!uniform) for (let i = 0; i < info.samples; i++) bytes += u32(b, stsz.body + 12 + i * 4);
      info.bytes = bytes;
      if (info.seconds) {
        info.bitrate = (bytes * 8) / info.seconds;
        info.fps = info.samples / info.seconds;
      }
    }
    return info;
  }

  function patchMvhd(b, box) {
    if (b[box.body] === 1) {
      const out = b.slice(box.start, box.end);
      out.fill(0xff, 8 + 24, 8 + 32);
      return out;
    }
    // v0 body: verflags(4) ctime(4) mtime(4) timescale(4) duration(4) rest...
    const head = new Uint8Array(4 + 8 + 8 + 4 + 8);
    head[0] = 1;
    head.set(b.subarray(box.body + 1, box.body + 4), 1);
    head.set(b.subarray(box.body + 4, box.body + 8), 8);
    head.set(b.subarray(box.body + 8, box.body + 12), 16);
    head.set(b.subarray(box.body + 12, box.body + 16), 20);
    head.fill(0xff, 24, 32);
    return makeBox('mvhd', [head, b.subarray(box.body + 20, box.end)]);
  }

  function hasMarker(b, udta) {
    return boxes(b, udta.body, udta.end).some(
      (x) => x.type === 'uuid' && MARKER_UUID.every((v, i) => b[x.body + i] === v)
    );
  }

  function patchVideoTrak(b, trak) {
    const marker = makeBox('uuid', [Uint8Array.from(MARKER_UUID), Uint8Array.from(MARKER_PAYLOAD)]);
    const children = boxes(b, trak.body, trak.end);
    const udta = children.find((x) => x.type === 'udta');
    if (udta && hasMarker(b, udta)) return b.subarray(trak.start, trak.end);
    const parts = children.map((x) =>
      x === udta ? makeBox('udta', [b.subarray(x.body, x.end), marker]) : b.subarray(x.start, x.end)
    );
    if (!udta) parts.push(makeBox('udta', [marker]));
    return makeBox('trak', parts);
  }

  // Media data after the insertion point moves forward by the new moov; data after the old moov moves back by its size.
  function shiftChunkOffsets(b, insertAt, oldMoov) {
    const shift = (v) => (v >= insertAt ? b.length : 0) - (v >= oldMoov.end ? oldMoov.end - oldMoov.start : 0);
    const moov = { body: 8, end: b.length };
    for (const trak of boxes(b, moov.body, moov.end).filter((x) => x.type === 'trak')) {
      const stbl = find(b, trak, ['mdia', 'minf', 'stbl']);
      if (!stbl) continue;
      for (const box of boxes(b, stbl.body, stbl.end)) {
        if (box.type !== 'stco' && box.type !== 'co64') continue;
        const n = u32(b, box.body + 4);
        for (let i = 0; i < n; i++) {
          if (box.type === 'stco') {
            const o = box.body + 8 + i * 4;
            const v = u32(b, o);
            if (v + shift(v) > 0xffffffff) throw new Error('ไฟล์ใหญ่เกิน 4 GB สำหรับตาราง offset แบบ 32 บิต');
            setU32(b, o, v + shift(v));
          } else {
            const o = box.body + 8 + i * 8;
            const v = u64(b, o);
            setU64(b, o, v + shift(v));
          }
        }
      }
    }
  }

  // moov: bytes of the whole moov box. pos: its {start, end} in the file. insertAt: where the new moov will go.
  function patchMoov(moov, pos, insertAt) {
    if (u32(moov, 0) !== moov.length) throw new Error('ไม่รองรับ moov ที่ใช้ขนาดแบบ 64 บิต');
    const children = boxes(moov, 8, moov.length);
    const tracks = [];
    let videoDone = false;
    const parts = children.map((box) => {
      if (box.type === 'mvhd') return patchMvhd(moov, box);
      if (box.type === 'trak') {
        const info = trackInfo(moov, box);
        if (info) tracks.push(info);
        if (info && info.handler === 'vide' && !videoDone) {
          videoDone = true;
          return patchVideoTrak(moov, box);
        }
      }
      return moov.subarray(box.start, box.end);
    });
    if (!videoDone) throw new Error('ไม่พบแทร็กวิดีโอในไฟล์');
    const out = makeBox('moov', parts);
    shiftChunkOffsets(out, insertAt, pos);
    return {
      moov: out,
      video: tracks.find((t) => t.handler === 'vide'),
      audio: tracks.find((t) => t.handler === 'soun') || null,
    };
  }

  async function patchFile(file) {
    const read = async (pos, len) => new Uint8Array(await file.slice(pos, pos + len).arrayBuffer());
    const { moov, insertAt } = await scanTopLevel(read, file.size);
    const result = patchMoov(await read(moov.start, moov.end - moov.start), moov, insertAt);
    // file slices are lazy, so the media data is never loaded into memory
    const pieces = [file.slice(0, insertAt), result.moov, file.slice(insertAt, moov.start), file.slice(moov.end)];
    return {
      blob: new Blob(pieces, { type: file.type || 'video/mp4' }),
      video: result.video,
      audio: result.audio,
    };
  }

  const api = { scanTopLevel, patchMoov, patchFile };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TikTokSmooth = api;
})(typeof self !== 'undefined' ? self : this);
