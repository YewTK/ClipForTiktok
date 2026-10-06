"""TikTok Smooth local server — serves the pages and downloads clips with yt-dlp.

Run:  python server.py        then open http://localhost:5173
Env:  PORT=5173                 port to listen on (localhost only)
      COOKIES=path/cookies.txt  Netscape cookies file for clips that need a login (IG, age-gated YouTube)
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadCancelled

ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / 'downloads'
PORT = int(os.environ.get('PORT', 5173))
COOKIES = os.environ.get('COOKIES')

TIKTOK_READY = {'H.264', 'H.265'}
CODECS = (('avc', 'H.264'), ('h264', 'H.264'), ('hvc', 'H.265'), ('hev', 'H.265'), ('h265', 'H.265'),
          ('bytevc1', 'H.265'), ('vp09', 'VP9'), ('vp9', 'VP9'), ('av01', 'AV1'), ('vp8', 'VP8'))
CODEC_RANK = {'H.265': 4, 'H.264': 3, 'AV1': 2, 'VP9': 1}


def find_ffmpeg():
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()

# HEVC encoders in order of preference: GPU first, software as the fallback.
ENCODERS = {
    'hevc_nvenc': {'args': ['-preset', 'p6', '-tune', 'hq', '-rc', 'vbr', '-cq', '19', '-b:v', '0', '-spatial_aq', '1'],
                   'ten_bit': ['-profile:v', 'main10', '-pix_fmt', 'p010le'], 'name': 'NVIDIA NVENC'},
    'hevc_qsv': {'args': ['-preset', 'slower', '-global_quality', '20'],
                 'ten_bit': ['-profile:v', 'main10', '-pix_fmt', 'p010le'], 'name': 'Intel Quick Sync'},
    'hevc_amf': {'args': ['-quality', 'quality', '-rc', 'cqp', '-qp_i', '19', '-qp_p', '21'],
                 'ten_bit': ['-profile:v', 'main10', '-pix_fmt', 'p010le'], 'name': 'AMD AMF'},
    'libx265': {'args': ['-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p'],
                'ten_bit': ['-profile:v', 'main10', '-pix_fmt', 'yuv420p10le'], 'name': 'CPU (x265)'},
}
_encoder = None


def hevc_encoder():
    """First HEVC encoder that actually works on this machine (a listed GPU encoder may have no GPU behind it)."""
    global _encoder
    if _encoder is None and FFMPEG:
        _encoder = ''
        for enc in ENCODERS:
            r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=s=256x256:d=0.2',
                                '-c:v', enc, '-f', 'null', '-'], capture_output=True, creationflags=_NO_WINDOW)
            if r.returncode == 0:
                _encoder = enc
                break
    return _encoder or None


_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def ydl_opts(**extra):
    opts = {
        'quiet': True, 'no_warnings': True, 'noprogress': True, 'noplaylist': True,
        'js_runtimes': {'deno': {}, 'node': {}},  # YouTube needs a JS runtime to unlock its formats
        'concurrent_fragment_downloads': 8, 'retries': 10, 'fragment_retries': 10,
        'windowsfilenames': True,
    }
    if FFMPEG:
        opts['ffmpeg_location'] = FFMPEG
    if COOKIES:
        opts['cookiefile'] = COOKIES
    opts.update(extra)
    return opts


def codec_family(vcodec):
    v = (vcodec or '').lower()
    for prefix, name in CODECS:
        if v.startswith(prefix):
            return name
    return v.split('.')[0].upper() or '?'


def is_ten_bit(f):
    v = (f.get('vcodec') or '').lower()
    return ((f.get('dynamic_range') or 'SDR') != 'SDR' or v.startswith(('vp09.02', 'vp9.2'))
            or bool(re.match(r'av01\.\d\.\d+\w\.10', v)))


def size_of(f, duration):
    return f.get('filesize') or f.get('filesize_approx') or (
        int(f['tbr'] * 125 * duration) if f.get('tbr') and duration else None)


def quality_options(info):
    """One option per (resolution, fps, codec, HDR), keeping the best-bitrate stream of each."""
    duration = info.get('duration') or 0
    formats = info.get('formats') or []
    audio = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')]
    best_audio = max(audio, key=lambda f: f.get('abr') or f.get('tbr') or 0, default=None)
    audio_size = size_of(best_audio, duration) if best_audio else 0

    best = {}
    for f in formats:
        if f.get('vcodec') == 'none' or not f.get('height'):
            continue
        w, h = f.get('width') or 0, f['height']
        codec = codec_family(f.get('vcodec'))
        hdr = (f.get('dynamic_range') or 'SDR') != 'SDR'
        key = (min(w, h) if w else h, round(f.get('fps') or 0), codec, hdr)
        rank = (f.get('protocol') in ('https', 'http'), f.get('tbr') or 0)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, f)

    options = []
    for (side, fps, codec, hdr), (_, f) in best.items():
        video_only = f.get('acodec') in (None, 'none')
        audio_ext = 'm4a' if codec in ('H.264', 'H.265', 'AV1') else 'webm'
        fid = f['format_id']
        selector = f'{fid}+bestaudio[ext={audio_ext}]/{fid}+bestaudio/{fid}' if video_only else fid
        size = size_of(f, duration)
        options.append({
            'id': fid, 'selector': selector, 'side': side, 'fps': fps, 'codec': codec, 'hdr': hdr,
            'width': f.get('width'), 'height': f['height'], 'tenBit': is_ten_bit(f),
            'tiktok': codec in TIKTOK_READY, 'size': size + (audio_size if video_only else 0) if size else None,
            'kbps': round(f['tbr']) if f.get('tbr') else None,
            'label': f'{side}p{fps if fps > 30 else ""}',
        })
    options.sort(key=lambda o: (o['side'], o['fps'], CODEC_RANK.get(o['codec'], 0), not o['hdr']), reverse=True)
    return options


# ---------------------------------------------------------------- jobs

MP3_BITRATES = (320, 256, 192, 128)
UPLOADS = DOWNLOADS / '.uploads'

_lookups = {}  # token -> {'url', 'info', 'options', 'at'}
_jobs = {}
_lock = threading.Lock()


def lookup(url):
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get('_type') == 'playlist':
        entries = [e for e in info.get('entries') or [] if e]
        if not entries:
            raise ValueError('ลิงก์นี้เป็นเพลย์ลิสต์ที่ไม่มีวิดีโอ')
        info = entries[0]
    info = yt_dlp.YoutubeDL.sanitize_info(info)
    options = quality_options(info)
    if not options:
        raise ValueError('ไม่พบไฟล์วิดีโอที่ดาวน์โหลดได้จากลิงก์นี้')
    token = uuid.uuid4().hex
    now = time.time()
    with _lock:
        for k in [k for k, v in _lookups.items() if now - v['at'] > 2 * 3600]:
            del _lookups[k]
        _lookups[token] = {'url': info.get('webpage_url') or url, 'info': info, 'options': options, 'at': now}
    return {
        'token': token, 'title': info.get('title'), 'uploader': info.get('uploader') or info.get('channel'),
        'duration': info.get('duration'), 'thumbnail': info.get('thumbnail'),
        'platform': info.get('extractor_key'), 'url': info.get('webpage_url') or url, 'options': options,
    }


def unique_path(path):
    n = 2
    base = path.stem
    while path.exists():
        path = path.with_name(f'{base} ({n}){path.suffix}')
        n += 1
    return path


def media_duration(path):
    r = subprocess.run([FFMPEG, '-hide_banner', '-i', str(path)], capture_output=True, text=True, encoding='utf-8',
                       errors='replace', creationflags=_NO_WINDOW)
    m = re.search(r'Duration: (\d+):(\d+):(\d+\.?\d*)', r.stderr)
    if not m:
        raise ValueError('ไฟล์นี้ไม่ใช่ไฟล์วิดีโอหรือเสียงที่ ffmpeg อ่านได้')
    if 'Audio:' not in r.stderr:
        raise ValueError('ไฟล์นี้ไม่มีเสียง')
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def mp3_args(src, bitrate, title=None, artist=None):
    meta = [x for k, v in (('title', title), ('artist', artist)) if v for x in ('-metadata', f'{k}={v}')]
    return ['-i', str(src), '-map', '0:a:0', '-vn', '-c:a', 'libmp3lame', '-b:a', f'{bitrate}k',
            '-id3v2_version', '3', *meta]


class Job:
    """State the page polls. status: queued downloading merging converting done error cancelled."""

    def __init__(self, codec):
        self.id = uuid.uuid4().hex[:12]
        self.status = 'queued'
        self.stage = ''
        self.progress = 0.0
        self.speed = self.eta = self.total = None
        self.file = None
        self.codec = codec
        self.error = None
        self.cancelled = False
        self.proc = None

    def public(self):
        return {
            'id': self.id, 'status': self.status, 'stage': self.stage, 'progress': self.progress,
            'speed': self.speed, 'eta': self.eta, 'total': self.total, 'error': self.error, 'codec': self.codec,
            'file': self.file and self.file.name, 'size': self.file and self.file.exists() and self.file.stat().st_size,
        }

    def start(self):
        with _lock:
            _jobs[self.id] = self
        threading.Thread(target=self.run, daemon=True).start()
        return self.public()

    def run(self):
        try:
            self.work()
            self.status, self.stage = 'done', ''
        except Exception as e:  # noqa: BLE001 — every failure is reported to the page
            if self.cancelled:
                self.status, self.error = 'cancelled', 'ยกเลิกแล้ว'
                self.discard()
            else:
                self.status, self.error = 'error', clean_error(e)
        finally:
            self.cleanup()

    def work(self):
        raise NotImplementedError

    def discard(self):
        """Remove partial output after a cancel."""

    def cleanup(self):
        """Remove temporary input once the job ends, whatever the outcome."""

    def ffmpeg(self, args, out, duration, stage):
        """Run ffmpeg into `out`, reporting progress from its -progress stream."""
        self.status, self.stage, self.progress, self.speed, self.eta = 'converting', stage, 0.0, None, None
        cmd = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', *args, '-progress', 'pipe:1', '-nostats', str(out)]
        started = time.time()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding='utf-8', errors='replace', creationflags=_NO_WINDOW)
        for line in self.proc.stdout:
            key, _, value = line.strip().partition('=')
            if key == 'out_time_us' and value.isdigit() and duration:
                self.progress = min(1.0, int(value) / 1e6 / duration)
                elapsed = time.time() - started
                if self.progress > 0.01:
                    self.eta = elapsed / self.progress - elapsed
            elif key == 'speed' and value.endswith('x'):
                self.speed = value
        err = self.proc.stderr.read()
        if self.proc.wait() != 0:
            out.unlink(missing_ok=True)
            if self.cancelled:
                raise DownloadCancelled()
            raise RuntimeError('แปลงไฟล์ไม่สำเร็จ: ' + (err.strip().splitlines() or ['ffmpeg error'])[-1])
        self.progress = 1.0

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class DownloadJob(Job):
    def __init__(self, source, option, convert):
        super().__init__(option['codec'])
        self.source, self.option, self.convert = source, option, convert
        self._streams = []

    def on_progress(self, d):
        if self.cancelled:
            raise DownloadCancelled('ยกเลิกแล้ว')
        if d['status'] != 'downloading':
            return
        name = d.get('filename')
        if name not in self._streams:
            self._streams.append(name)
        is_audio = self.option.get('audio') or (d.get('info_dict') or {}).get('vcodec') == 'none'
        self.status = 'downloading'
        self.stage = 'เสียง' if is_audio else 'วิดีโอ'
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        if total:
            self.progress = min(1.0, d.get('downloaded_bytes', 0) / total)
        elif d.get('fragment_count'):
            self.progress = (d.get('fragment_index') or 0) / d['fragment_count']
        self.total, self.speed, self.eta = total, d.get('speed'), d.get('eta')

    def on_postprocess(self, d):
        if d['status'] == 'started' and d.get('postprocessor') in ('Merger', 'FFmpegMerger'):
            self.status, self.stage, self.progress, self.speed, self.eta = 'merging', 'รวมภาพและเสียง', 1.0, None, None

    def work(self):
        self.download()
        if self.cancelled:
            raise DownloadCancelled()
        if self.option.get('bitrate'):
            self.to_mp3()
        elif self.convert:
            self.transcode()

    def discard(self):
        # partial streams are kept after an error so a retry resumes them, but a cancel discards them
        for name in self._streams:
            for leftover in (name, name + '.part', name + '.ytdl'):
                Path(leftover).unlink(missing_ok=True)

    def download(self):
        DOWNLOADS.mkdir(exist_ok=True)
        label = self.option['label']
        opts = ydl_opts(
            format=self.option['selector'],
            outtmpl=str(DOWNLOADS / f'%(title).90B [%(id)s] {label}.%(ext)s'),
            progress_hooks=[self.on_progress], postprocessor_hooks=[self.on_postprocess],
        )
        if not self.option.get('audio'):
            opts['merge_output_format'] = 'mp4/webm/mkv'
        # extract again rather than reuse the lookup: some sites (YouTube) tie stream URLs to the session that fetched them
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.source['url'], download=True)
        done = (info.get('requested_downloads') or [{}])[0]
        self.file = Path(done.get('filepath') or done.get('_filename'))

    def to_mp3(self):
        src, bitrate, info = self.file, self.option['bitrate'], self.source['info']
        out = unique_path(src.with_name(src.stem.removesuffix(' ' + self.option['label']) + f' {bitrate}k.mp3'))
        artist = info.get('artist') or info.get('uploader') or info.get('channel')
        self.ffmpeg(mp3_args(src, bitrate, info.get('track') or info.get('title'), artist), out,
                    info.get('duration') or 0, f'แปลงเป็น MP3 {bitrate} kbps')
        src.unlink(missing_ok=True)
        self.file = out

    def transcode(self):
        enc = hevc_encoder()
        if not enc:
            raise RuntimeError('ไม่พบ ffmpeg ที่ใช้แปลงไฟล์ได้')
        spec = ENCODERS[enc]
        src = self.file
        out = src.with_name(src.stem + ' H265.mp4')
        args = ['-i', str(src), '-map', '0:v:0', '-map', '0:a:0?',
                '-c:v', enc, *spec['args'], *(spec['ten_bit'] if self.option.get('tenBit') else []),
                '-tag:v', 'hvc1', '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart']
        self.ffmpeg(args, out, self.source['info'].get('duration') or 0, f'แปลงเป็น H.265 · {spec["name"]}')
        src.unlink(missing_ok=True)
        self.file, self.codec = out, 'H.265'


class Mp3Job(Job):
    """Converts a file uploaded from the MP3 page."""

    def __init__(self, upload, name, bitrate):
        super().__init__('MP3')
        self.upload, self.bitrate = upload, bitrate
        self.out = unique_path(DOWNLOADS / (Path(name).stem + '.mp3'))

    def work(self):
        duration = media_duration(self.upload)
        self.ffmpeg(mp3_args(self.upload, self.bitrate, self.out.stem), self.out, duration,
                    f'แปลงเป็น MP3 {self.bitrate} kbps')
        self.file = self.out

    def cleanup(self):
        self.upload.unlink(missing_ok=True)


def clean_error(e):
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(e)).replace('ERROR: ', '').strip()
    low = msg.lower()
    if 'login' in low or 'cookies' in low or 'sign in' in low:
        msg += '\n→ คลิปนี้ต้องล็อกอิน: export cookies.txt จากเบราว์เซอร์แล้วรัน server ด้วย COOKIES=cookies.txt'
    return msg


def check_bitrate(value):
    bitrate = int(value or 320)
    if bitrate not in MP3_BITRATES:
        raise ValueError('บิตเรต MP3 ต้องเป็น ' + ' / '.join(map(str, MP3_BITRATES)))
    return bitrate


def start_download(token, option_id, convert, bitrate):
    with _lock:
        source = _lookups.get(token)
    if not source:
        raise ValueError('ข้อมูลคลิปหมดอายุ กรุณาวางลิงก์ใหม่อีกครั้ง')
    if option_id == 'mp3':
        option = {'id': 'mp3', 'selector': 'bestaudio/best', 'audio': True, 'codec': 'MP3', 'label': 'audio',
                  'bitrate': check_bitrate(bitrate)}
    else:
        option = next((o for o in source['options'] if o['id'] == option_id), None)
        if not option:
            raise ValueError('ไม่พบคุณภาพที่เลือก')
    return DownloadJob(source, option, bool(convert) and option['codec'] not in TIKTOK_READY).start()


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(name or 'audio').name).strip(' .')
    return name[:150] or 'audio'


def start_mp3(rfile, length, name, bitrate):
    """Stream an upload to disk in chunks (any size, never held in memory), then convert it."""
    bitrate = check_bitrate(bitrate)
    if not FFMPEG:
        raise RuntimeError('ไม่พบ ffmpeg — ติดตั้งด้วย pip install -r requirements.txt')
    name = safe_name(name)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    upload = UPLOADS / (uuid.uuid4().hex + Path(name).suffix.lower())
    remaining = length
    with upload.open('wb') as f:
        while remaining > 0:
            chunk = rfile.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)
    if remaining or not length:
        upload.unlink(missing_ok=True)
        raise ValueError('อัปโหลดไม่ครบ')
    return Mp3Job(upload, name, bitrate).start()


# ---------------------------------------------------------------- http

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        if '/api/jobs/' not in self.path:  # progress polling would flood the console
            super().log_message(fmt, *args)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(length) or b'{}')

    def job(self, job_id):
        with _lock:
            return _jobs.get(job_id)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/health':
            return self.send_json({'ok': True, 'ytdlp': yt_dlp.version.__version__, 'ffmpeg': bool(FFMPEG)})
        if path == '/api/encoder':
            enc = hevc_encoder()
            return self.send_json({'encoder': enc, 'name': enc and ENCODERS[enc]['name']})
        m = re.fullmatch(r'/api/jobs/(\w+)(/file)?', path)
        if m:
            job = self.job(m[1])
            if not job:
                return self.send_json({'error': 'ไม่พบงานนี้'}, 404)
            return self.send_file(job) if m[2] else self.send_json(job.public())
        # static files only: no dotfiles (.git, .claude) and no raw downloads folder
        first = path.lstrip('/').split('/', 1)[0]
        if first.startswith('.') or first in ('downloads', '__pycache__') or path.endswith('.py'):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        try:
            if path == '/api/mp3':  # raw file body; name and bitrate ride in the query string
                query = urllib.parse.parse_qs(url.query)
                self.close_connection = True  # an early error leaves the rest of the body unread
                return self.send_json(start_mp3(self.rfile, int(self.headers.get('Content-Length') or 0),
                                                query.get('name', [''])[0], query.get('bitrate', ['320'])[0]))
            data = self.read_json()
            if path == '/api/info':
                url = (data.get('url') or '').strip()
                if not re.match(r'https?://', url):
                    raise ValueError('กรุณาวางลิงก์ที่ขึ้นต้นด้วย http:// หรือ https://')
                return self.send_json(lookup(url))
            if path == '/api/download':
                return self.send_json(start_download(data.get('token'), data.get('option'), data.get('convert'),
                                                     data.get('bitrate')))
            m = re.fullmatch(r'/api/jobs/(\w+)/cancel', path)
            if m and (job := self.job(m[1])):
                job.cancel()
                return self.send_json(job.public())
            return self.send_json({'error': 'not found'}, 404)
        except Exception as e:  # noqa: BLE001
            return self.send_json({'error': clean_error(e)}, 400)

    def send_file(self, job):
        if job.status != 'done' or not job.file or not job.file.exists():
            return self.send_json({'error': 'ไฟล์ยังไม่พร้อม'}, 409)
        size = job.file.stat().st_size
        ctype = mimetypes.guess_type(job.file.name)[0] or 'application/octet-stream'
        quoted = urllib.parse.quote(job.file.name)
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(size))
        self.send_header('Content-Disposition', f"attachment; filename=\"download{job.file.suffix}\"; filename*=UTF-8''{quoted}")
        self.end_headers()
        try:
            with job.file.open('rb') as f:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252, which can't print Thai titles
        stream.reconfigure(encoding='utf-8', errors='replace')
    mimetypes.add_type('video/mp4', '.mp4')
    mimetypes.add_type('video/webm', '.webm')
    mimetypes.add_type('video/x-matroska', '.mkv')
    mimetypes.add_type('audio/mp4', '.m4a')
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'TikTok Smooth → http://localhost:{PORT}   (ffmpeg: {FFMPEG or "not found"})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
