import yt_dlp
import os
import re
from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import threading
import imageio_ffmpeg  # Portable FFmpeg engine

# Cloud environment routing configurations
PORT = int(os.environ.get("PORT", 8000))
DEFAULT_DOWNLOAD_FOLDER = "downloads"
os.makedirs(DEFAULT_DOWNLOAD_FOLDER, exist_ok=True)

class YTDLPHandler:
    def __init__(self):
        self.current_download = None
        self.progress = 0
        self.status = "Ready"
        self.filename = ""
        self.error_message = ""
        self._lock = threading.Lock()

    def reset_state(self):
        with self._lock:
            self.current_download = None
            self.progress = 0
            self.status = "Ready"
            self.filename = ""
            self.error_message = ""

    def download_thread_worker(self, url, format_choice, resolution, output_folder):
        try:
            with self._lock:
                self.status = "Preparing..."
                self.progress = 0
                self.filename = ""
                self.error_message = ""

            url = self.clean_youtube_url(url)
            if not url:
                with self._lock:
                    self.status = "Error: Invalid YouTube URL"
                return

            target_folder = output_folder.strip() if output_folder else DEFAULT_DOWNLOAD_FOLDER
            os.makedirs(target_folder, exist_ok=True)

            base_dir = os.path.dirname(os.path.abspath(__file__))
            absolute_cookie_path = os.path.join(base_dir, 'cookies.txt')

            portable_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            format_choice_clean = str(format_choice).lower().strip()

            # ── Shared base options ──────────────────────────────────────────────────
            base_opts = {
                'ffmpeg_location': portable_ffmpeg,
                'outtmpl': os.path.join(target_folder, '%(title)s.%(ext)s'),
                'progress_hooks': [self.progress_hook],
                'quiet': True,
                'no_warnings': True,
                # Abort early instead of hanging forever on broken streams
                'socket_timeout': 15,
                'retries': 5,
                'fragment_retries': 5,
                # Always try the most compatible client first
                'extractor_args': {
                    'youtube': {
                        'player_client': ['web_safari', 'web', 'android'],
                    }
                },
            }

            # Only attach cookie file if it actually exists
            if os.path.isfile(absolute_cookie_path):
                base_opts['cookiefile'] = absolute_cookie_path

            # Only attach proxy if the env var is set (avoids hardcoded credentials)
            proxy_url = os.environ.get('YT_PROXY', '')
            if proxy_url:
                base_opts['proxy'] = proxy_url

            # ── Format-specific options ──────────────────────────────────────────────
            if format_choice_clean == "mp3":
                ydl_opts = {
                    **base_opts,
                    # FIX: broader fallback chain so any available audio stream is accepted
                    'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                }
            else:  # MP4
                max_height = int(resolution) if resolution and resolution != 'best' else 2160

                # FIX: Tiered format chain — each tier is a complete fallback, not nested
                # Tier 1: separate mp4+m4a streams merged by ffmpeg (best quality)
                # Tier 2: any separate video+audio streams ffmpeg can merge
                # Tier 3: pre-merged single-file stream at or below target height
                # Tier 4: absolute best single-file stream (no height restriction)
                format_selection = '/'.join([
                    f'bestvideo[ext=mp4][height<={max_height}]+bestaudio[ext=m4a]',
                    f'bestvideo[ext=mp4][height<={max_height}]+bestaudio',
                    f'bestvideo[height<={max_height}]+bestaudio[ext=m4a]',
                    f'bestvideo[height<={max_height}]+bestaudio',
                    f'best[height<={max_height}][ext=mp4]',
                    f'best[height<={max_height}]',
                    'bestvideo+bestaudio',
                    'best',
                ])

                ydl_opts = {
                    **base_opts,
                    'format': format_selection,
                    'merge_output_format': 'mp4',
                }

            # ── Run download ─────────────────────────────────────────────────────────
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Probe first so we surface info errors before touching the disk
                info = ydl.extract_info(url, download=False)
                if not info:
                    with self._lock:
                        self.status = "Error: Could not extract video info"
                    return

                self.current_download = ydl.extract_info(url, download=True)

                if self.current_download:
                    filename = ydl.prepare_filename(self.current_download)
                    if format_choice_clean == "mp3":
                        filename = os.path.splitext(filename)[0] + '.mp3'
                    with self._lock:
                        self.filename = filename
                        self.status = "Complete" if os.path.exists(filename) else "Error: File was not created"
                else:
                    with self._lock:
                        self.status = "Error: Download failed"

        except yt_dlp.utils.DownloadError as e:
            err = str(e)
            print(f"YT-DLP ERROR: {err}")
            with self._lock:
                self.status = "Error: Download failed"
                self.error_message = err
        except Exception as e:
            err = str(e)
            print(f"CRITICAL ERROR: {err}")
            with self._lock:
                self.status = f"Error: {err}"
                self.error_message = err

    def download(self, url, format_choice, resolution=None, output_folder=None):
        with self._lock:
            if self.status in ["Preparing...", "Processing..."] or "Downloading..." in self.status:
                return False, "A download is already in progress."
        t = threading.Thread(
            target=self.download_thread_worker,
            args=(url, format_choice, resolution, output_folder),
            daemon=True,
        )
        t.start()
        return True, ""

    def progress_hook(self, d):
        if d['status'] == 'downloading':
            try:
                pct_str = d.get('_percent_str', '0%')
                clean = re.sub(r'\x1b\[[0-9;]*m', '', pct_str).strip().rstrip('%')
                with self._lock:
                    self.progress = float(clean)
                    self.status = f"Downloading... {clean}%"
            except Exception:
                pass
        elif d['status'] == 'finished':
            with self._lock:
                self.progress = 100
                self.status = "Processing..."

    @staticmethod
    def clean_youtube_url(url):
        if not url:
            return None
        # Match standard YouTube URLs and extract the 11-char video ID
        pattern = (
            r'(?:https?://)?(?:www\.)?'
            r'(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)'
            r'([A-Za-z0-9_-]{11})'
        )
        match = re.search(pattern, url)
        if match:
            return f'https://www.youtube.com/watch?v={match.group(1)}'
        # Bare 11-char IDs accepted too
        if re.fullmatch(r'[A-Za-z0-9_-]{11}', url):
            return f'https://www.youtube.com/watch?v={url}'
        return None


# ── HTTP layer ───────────────────────────────────────────────────────────────────

class WebHandler(SimpleHTTPRequestHandler):
    ytdlp = YTDLPHandler()

    def log_message(self, format, *args):
        pass  # Silence per-request access logs

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/status':
            with self.ytdlp._lock:
                payload = {
                    'status': self.ytdlp.status,
                    'progress': self.ytdlp.progress,
                    'filename': os.path.basename(self.ytdlp.filename) if self.ytdlp.filename else "",
                    'error': self.ytdlp.error_message,
                }
            self._json(payload)
        elif self.path == '/reset':
            self.ytdlp.reset_state()
            self._json({'success': True})
        else:
            if self.path in ('/', ''):
                self.path = '/index.html'
            return super().do_GET()

    def do_POST(self):
        if self.path == '/download':
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length))

            ok, err = self.ytdlp.download(
                data.get('url', ''),
                data.get('format', 'mp4'),
                data.get('resolution'),
                data.get('output_folder'),
            )
            self._json({'success': ok, 'error': err})
        else:
            self.send_response(404)
            self.end_headers()


# ── HTML frontend ────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YouTube Downloader</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f2027, #203a43, #2c5364);
            min-height: 100vh;
            padding: 20px;
            display: flex;
            justify-content: center;
            align-items: center;
            color: white;
        }
        .container {
            background: rgba(0,0,50,.7);
            padding: 30px;
            border-radius: 15px;
            width: 100%;
            max-width: 600px;
            box-shadow: 0 8px 32px 0 rgba(31,38,135,.37);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,.2);
        }
        h1 {
            text-align: center;
            background: linear-gradient(to right, #1c3aff, #3c2bac);
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 25px;
        }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; font-weight: bold; }
        input[type="text"] {
            width: 100%; padding: 12px; border-radius: 6px;
            border: 1px solid #ddd; background: rgba(255,255,255,.9);
            font-size: 16px; color: #333;
        }
        .radio-group { display: flex; gap: 15px; margin-top: 10px; }
        input[type="radio"] { display: none; }
        .option-label {
            background: rgba(126,186,228,.3); color: white;
            padding: 10px 20px; border-radius: 20px; display: inline-block;
            cursor: pointer; transition: all .3s;
            border: 1px solid rgba(255,255,255,.2);
        }
        input[type="radio"]:checked + .option-label {
            background: linear-gradient(to right, #1c3aff, #3c2bac);
            font-weight: bold;
        }
        .option-label:hover { background: rgba(59,159,231,.5); }
        select {
            width: 100%; padding: 12px; border-radius: 6px;
            border: 1px solid #ddd; background: rgba(255,255,255,.9);
            font-size: 14px; color: #333; cursor: pointer;
        }
        button {
            background: linear-gradient(to right, #1c3aff, #3c2bac);
            color: white; border: none; padding: 15px; border-radius: 6px;
            cursor: pointer; font-size: 16px; width: 100%;
            transition: all .3s; margin: 20px 0;
        }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(28,58,255,.4); }
        button:disabled { background: #6c757d; cursor: not-allowed; transform: none; box-shadow: none; }
        #reset-btn { background: linear-gradient(to right,#2b9348,#55a630); display:none; margin-top:10px; }
        #reset-btn:hover { box-shadow: 0 5px 15px rgba(85,166,48,.4); }
        progress { width:100%; height:20px; border-radius:5px; border:1px solid #333; }
        progress::-webkit-progress-bar { background:#f0f0f0; border-radius:5px; }
        progress::-webkit-progress-value { background:linear-gradient(to right,#1c3aff,#3c2bac); border-radius:5px; }
        #status { margin-top:10px; font-weight:bold; text-align:center; }
        #filename { color:#7ebae4; margin-top:5px; text-align:center; word-break:break-all; }
        .message { padding:10px; margin:10px 0; border-radius:5px; text-align:center; display:none; }
        .error { background:#ff6b6b; color:white; }
        .success { background:#51cf66; color:white; }
    </style>
</head>
<body>
<div class="container">
    <h1>YouTube Downloader</h1>

    <div class="form-group">
        <label for="url">YouTube URL or Video ID:</label>
        <input type="text" id="url" placeholder="Paste YouTube URL or Video ID here">
    </div>
    <div class="form-group">
        <label for="output_folder">Custom Save Directory (Optional):</label>
        <input type="text" id="output_folder" placeholder="e.g., downloads">
    </div>
    <div class="form-group">
        <label>Format:</label>
        <div class="radio-group">
            <input type="radio" id="mp4" name="format" value="mp4" checked>
            <label for="mp4" class="option-label">MP4 (Video)</label>
            <input type="radio" id="mp3" name="format" value="mp3">
            <label for="mp3" class="option-label">MP3 (Audio)</label>
        </div>
    </div>
    <div class="form-group" id="resolution-group">
        <label for="resolution">Resolution:</label>
        <select id="resolution">
            <option value="best">Best Available</option>
            <option value="1080">1080p</option>
            <option value="720">720p</option>
            <option value="480">480p</option>
            <option value="360">360p</option>
        </select>
    </div>

    <button id="download-btn">Download</button>
    <button id="reset-btn">Download Another Video</button>

    <div class="progress-container">
        <progress id="progress" value="0" max="100"></progress>
        <div id="status">Ready to download</div>
        <div id="filename"></div>
    </div>
    <div id="message" class="message"></div>
</div>

<script>
document.addEventListener('DOMContentLoaded', function () {
    const urlInput     = document.getElementById('url');
    const folderInput  = document.getElementById('output_folder');
    const downloadBtn  = document.getElementById('download-btn');
    const resetBtn     = document.getElementById('reset-btn');
    const progressBar  = document.getElementById('progress');
    const statusText   = document.getElementById('status');
    const filenameText = document.getElementById('filename');
    const messageDiv   = document.getElementById('message');
    let interval = null;

    document.querySelectorAll('input[name="format"]').forEach(r => {
        r.addEventListener('change', function () {
            document.getElementById('resolution-group').style.display =
                this.value === 'mp4' ? 'block' : 'none';
        });
    });

    downloadBtn.addEventListener('click', function () {
        const url = urlInput.value.trim();
        if (!url) { showMsg('Please enter a YouTube URL or Video ID', 'error'); return; }

        const format     = document.querySelector('input[name="format"]:checked').value;
        const resolution = format === 'mp4' ? document.getElementById('resolution').value : null;
        const folder     = folderInput.value.trim();

        progressBar.value = 0;
        statusText.textContent = 'Starting…';
        filenameText.textContent = '';
        hideMsg();
        downloadBtn.disabled = true;
        downloadBtn.textContent = 'Downloading…';
        resetBtn.style.display = 'none';

        fetch('/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, format, resolution, output_folder: folder }),
        })
        .then(r => r.json())
        .then(data => {
            if (!data.success) throw new Error(data.error || 'Server rejected request');
            interval = setInterval(poll, 1000);
        })
        .catch(err => {
            statusText.textContent = 'Failed to start';
            showMsg('Error: ' + err.message, 'error');
            downloadBtn.disabled = false;
            downloadBtn.textContent = 'Download';
            resetBtn.style.display = 'block';
        });
    });

    resetBtn.addEventListener('click', function () {
        fetch('/reset').then(r => r.json()).then(d => { if (d.success) location.reload(); });
    });

    function poll() {
        fetch('/status').then(r => r.json()).then(data => {
            progressBar.value = data.progress;
            statusText.textContent = data.status;
            if (data.filename) filenameText.textContent = 'File: ' + data.filename;

            if (data.status === 'Complete') {
                clearInterval(interval);
                showMsg('Download completed successfully!', 'success');
                downloadBtn.disabled = false;
                downloadBtn.textContent = 'Download';
                resetBtn.style.display = 'block';
            } else if (data.status.startsWith('Error')) {
                clearInterval(interval);
                showMsg(data.error || data.status, 'error');
                downloadBtn.disabled = false;
                downloadBtn.textContent = 'Download';
                resetBtn.style.display = 'block';
            }
        }).catch(console.error);
    }

    function showMsg(text, type) {
        messageDiv.textContent = text;
        messageDiv.className = 'message ' + type;
        messageDiv.style.display = 'block';
    }
    function hideMsg() { messageDiv.style.display = 'none'; }
});
</script>
</body>
</html>"""


def generate_html():
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(HTML)


def start_server():
    generate_html()
    server = HTTPServer(('0.0.0.0', PORT), WebHandler)
    print(f"Server running at http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
        server.shutdown()


if __name__ == "__main__":
    start_server()
