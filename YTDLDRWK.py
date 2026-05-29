import os
import yt_dlp
import re
from http.server import HTTPServer, SimpleHTTPRequestHandler
import webbrowser
import json
import threading
import time
import urllib.parse

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
        """Resets the handler state for a completely new download tracking session."""
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
            
            if format_choice == "mp3":
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'outtmpl': os.path.join(target_folder, '%(title)s.%(ext)s'),
                    'progress_hooks': [self.progress_hook],
                    'quiet': True,
                    'no_warnings': True,
                }
            else:  # MP4
                format_selection = f'bestvideo[ext=mp4][height<={resolution if resolution != "best" else 2160}]+bestaudio[ext=m4a]/best[ext=mp4]'
                ydl_opts = {
                    'format': format_selection,
                    'outtmpl': os.path.join(target_folder, '%(title)s.%(ext)s'),
                    'progress_hooks': [self.progress_hook],
                    'merge_output_format': 'mp4',
                    'quiet': True,
                    'no_warnings': True,
                }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    with self._lock:
                        self.status = "Error: Could not extract video info"
                    return
                
                self.current_download = ydl.extract_info(url, download=True)
                
                if self.current_download:
                    filename = ydl.prepare_filename(self.current_download)
                    if format_choice == "mp3":
                        filename = os.path.splitext(filename)[0] + '.mp3'
                    
                    with self._lock:
                        self.filename = filename
                        if os.path.exists(self.filename):
                            self.status = "Complete"
                        else:
                            self.status = "Error: File was not created"
                else:
                    with self._lock:
                        self.status = "Error: Download failed"
                    
        except yt_dlp.utils.DownloadError as e:
            with self._lock:
                self.status = "Error: Download failed"
                self.error_message = str(e)
        except yt_dlp.utils.ExtractorError as e:
            with self._lock:
                self.status = "Error: Could not extract video info"
                self.error_message = str(e)
        except Exception as e:
            with self._lock:
                self.status = f"Error: {str(e)}"
                self.error_message = str(e)

    def download(self, url, format_choice, resolution=None, output_folder=None):
        with self._lock:
            if self.status in ["Preparing...", "Processing..."] or "Downloading..." in self.status:
                return False
            
        t = threading.Thread(
            target=self.download_thread_worker, 
            args=(url, format_choice, resolution, output_folder),
            daemon=True
        )
        t.start()
        return True

    def progress_hook(self, d):
        if d['status'] == 'downloading':
            if '_percent_str' in d:
                try:
                    clean_percent = d['_percent_str'].replace('\x1b[0m', '').strip('% ')
                    with self._lock:
                        self.progress = float(clean_percent)
                        self.status = f"Downloading... {clean_percent}%"
                except:
                    pass
        elif d['status'] == 'finished':
            with self._lock:
                self.progress = 100
                self.status = "Processing..."

    def clean_youtube_url(self, url):
        if not url:
            return None
        youtube_regex = (
            r'(https?://)?(www\.)?'
            r'(youtube|youtu|youtube-nocookie)\.(com|be)/'
            r'(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})')
        match = re.match(youtube_regex, url)
        if not match:
            if len(url) == 11 and not re.search(r'\s', url):
                return f'https://www.youtube.com/watch?v={url}'
            return None
        return f'https://www.youtube.com/watch?v={match.group(6)}'

class WebHandler(SimpleHTTPRequestHandler):
    ytdlp = YTDLPHandler()
    
    def do_GET(self):
        if self.path == '/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            with self.ytdlp._lock:
                response = {
                    'status': self.ytdlp.status,
                    'progress': self.ytdlp.progress,
                    'filename': os.path.basename(self.ytdlp.filename) if self.ytdlp.filename else "",
                    'error': self.ytdlp.error_message
                }
            self.wfile.write(json.dumps(response).encode())
        elif self.path == '/reset':
            self.ytdlp.reset_state()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True}).encode())
        elif self.path == '/' or self.path == '':
            self.path = 'index.html'
            return super().do_GET()
        else:
            return super().do_GET()
            
    def do_POST(self):
        if self.path == '/download':
            content_length = int(self.headers['Content-Length'])
            post_data = json.loads(self.rfile.read(content_length))
            
            success = self.ytdlp.download(
                post_data['url'],
                post_data['format'],
                post_data.get('resolution'),
                post_data.get('output_folder')
            )
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            with self.ytdlp._lock:
                err = self.ytdlp.error_message
            response = {
                'success': success,
                'error': err if not success else ""
            }
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()

def generate_html():
    html = """<!DOCTYPE html>
<html>
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
            background: rgba(0, 0, 50, 0.7);
            padding: 30px;
            border-radius: 15px;
            width: 100%;
            max-width: 600px;
            box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        h1 {
            text-align: center;
            background: linear-gradient(to right, #1c3aff, #3c2bac);
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 25px;
        }
        .form-group { margin-bottom: 20px; }
        .url-label, .format-header, .settings-label {
            display: block;
            margin-bottom: 8px;
            font-weight: bold;
        }
        #url, #output_folder {
            width: 100%;
            padding: 12px;
            border-radius: 6px;
            border: 1px solid #ddd;
            background: rgba(255, 255, 255, 0.9);
            font-size: 16px;
            color: #333;
        }
        .radio-group { display: flex; gap: 15px; margin-top: 10px; }
        input[type="radio"] { display: none; }
        .option-label {
            background-color: rgba(126, 186, 228, 0.3);
            color: white;
            padding: 10px 20px;
            border-radius: 20px;
            display: inline-block;
            cursor: pointer;
            transition: all 0.3s;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        input[type="radio"]:checked + .option-label {
            background: linear-gradient(to right, #1c3aff, #3c2bac);
            font-weight: bold;
        }
        .option-label:hover { background-color: rgba(59, 159, 231, 0.5); }
        .styled-select {
            padding: 12px;
            border-radius: 6px;
            border: 1px solid #ddd;
            background-color: rgba(255, 255, 255, 0.9);
            font-size: 14px;
            cursor: pointer;
            width: 100%;
            color: #333;
        }
        button {
            background: linear-gradient(to right, #1c3aff, #3c2bac);
            color: white;
            border: none;
            padding: 15px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 16px;
            width: 100%;
            transition: all 0.3s;
            margin: 20px 0;
        }
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(28, 58, 255, 0.4);
        }
        button:disabled { background: #6c757d; cursor: not-allowed; transform: none; box-shadow: none; }
        
        #reset-btn {
            background: linear-gradient(to right, #2b9348, #55a630);
            display: none; /* Hidden by default */
            margin-top: 10px;
        }
        #reset-btn:hover {
            box-shadow: 0 5px 15px rgba(85, 166, 48, 0.4);
        }

        .progress-container { margin: 20px 0; width: 100%; }
        progress { width: 100%; height: 20px; border-radius: 5px; border: 1px solid #333; }
        progress::-webkit-progress-bar { background-color: #f0f0f0; border-radius: 5px; }
        progress::-webkit-progress-value { background: linear-gradient(to right, #1c3aff, #3c2bac); border-radius: 5px; }
        #status { margin-top: 10px; font-weight: bold; text-align: center; }
        #filename { color: #7ebae4; margin-top: 5px; text-align: center; word-break: break-all; }
        .message { padding: 10px; margin: 10px 0; border-radius: 5px; text-align: center; display: none; }
        .error { background-color: #ff6b6b; color: white; }
        .success { background-color: #51cf66; color: white; }
        @media (max-width: 600px) { .container { padding: 20px; } .radio-group { flex-direction: column; gap: 10px; } }
    </style>
</head>
<body>
    <div class="container">
        <h1>YouTube Downloader</h1>
        
        <div class="form-group">
            <label for="url" class="url-label">YouTube URL or Video ID:</label>
            <input type="text" id="url" placeholder="Paste YouTube URL or Video ID here">
        </div>

        <div class="form-group">
            <label for="output_folder" class="url-label">Custom Save Directory (Optional):</label>
            <input type="text" id="output_folder" placeholder="e.g., downloads or C:/Users/Name/Music">
        </div>
        
        <div class="form-group">
            <label class="format-header">Format:</label>
            <div class="radio-group">
                <input type="radio" id="mp4" name="format" value="mp4" checked>
                <label for="mp4" class="option-label">MP4 (Video)</label>
                
                <input type="radio" id="mp3" name="format" value="mp3">
                <label for="mp3" class="option-label">MP3 (Audio)</label>
            </div>
        </div>
        
        <div class="form-group" id="resolution-group">
            <label class="settings-label">Resolution:</label>
            <select id="resolution" class="styled-select">
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
        document.addEventListener('DOMContentLoaded', function() {
            const urlInput = document.getElementById('url');
            const folderInput = document.getElementById('output_folder');
            const downloadBtn = document.getElementById('download-btn');
            const resetBtn = document.getElementById('reset-btn');
            const progressBar = document.getElementById('progress');
            const statusText = document.getElementById('status');
            const filenameText = document.getElementById('filename');
            const messageDiv = document.getElementById('message');
            let statusCheckInterval = null;
            
            document.querySelectorAll('input[name="format"]').forEach(radio => {
                radio.addEventListener('change', function() {
                    document.getElementById('resolution-group').style.display = 
                        this.value === 'mp4' ? 'block' : 'none';
                });
            });
            
            downloadBtn.addEventListener('click', function() {
                const url = urlInput.value.trim();
                if (!url) {
                    showMessage('Please enter a YouTube URL or Video ID', 'error');
                    return;
                }
                
                const format = document.querySelector('input[name="format"]:checked').value;
                const resolution = format === 'mp4' ? document.getElementById('resolution').value : null;
                const output_folder = folderInput.value.trim();
                
                progressBar.value = 0;
                statusText.innerText = 'Starting background worker...';
                filenameText.innerText = '';
                hideMessage();
                downloadBtn.disabled = true;
                downloadBtn.textContent = 'Downloading...';
                resetBtn.style.display = 'none'; // Hide if a retry occurred
                
                fetch('/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, format, resolution, output_folder })
                })
                .then(response => response.json())
                .then(data => {
                    if (!data.success) {
                        throw new Error(data.error || 'Server rejected download assignment');
                    }
                    statusCheckInterval = setInterval(updateProgress, 1000);
                })
                .catch(error => {
                    statusText.innerText = 'Initialization failed';
                    showMessage('Error: ' + error.message, 'error');
                    downloadBtn.disabled = false;
                    downloadBtn.textContent = 'Download';
                    resetBtn.style.display = 'block'; // Show reset button on immediate failures
                });
            });
            
            // "Download Another" Handler
            resetBtn.addEventListener('click', function() {
                fetch('/reset')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        // Force clean reload of the webpage
                        window.location.reload();
                    }
                })
                .catch(error => console.error('Failed to clear state:', error));
            });
            
            function updateProgress() {
                fetch('/status')
                .then(response => response.json())
                .then(data => {
                    progressBar.value = data.progress;
                    statusText.innerText = data.status;
                    
                    if (data.filename) {
                        filenameText.innerText = 'File: ' + data.filename;
                    }
                    
                    if (data.status === 'Complete') {
                        clearInterval(statusCheckInterval);
                        showMessage('Download completed successfully!', 'success');
                        downloadBtn.disabled = false;
                        downloadBtn.textContent = 'Download';
                        resetBtn.style.display = 'block'; // Show the option to reload/clear
                    } else if (data.status.startsWith('Error')) {
                        clearInterval(statusCheckInterval);
                        showMessage(data.error || data.status, 'error');
                        downloadBtn.disabled = false;
                        downloadBtn.textContent = 'Download';
                        resetBtn.style.display = 'block'; // Show on execution errors
                    }
                })
                .catch(error => console.error('Poller error:', error));
            }
            
            function showMessage(text, type) {
                messageDiv.textContent = text;
                messageDiv.className = `message ${type}`;
                messageDiv.style.display = 'block';
            }
            
            function hideMessage() { messageDiv.style.display = 'none'; }
        });
    </script>
</body>
</html>"""
    with open('index.html', 'w') as f:
        f.write(html)

def start_server():
    generate_html()
    server = HTTPServer(('0.0.0.0', PORT), WebHandler)
    # COMMENT THESE LINES OUT:
# def open_browser():
#     time.sleep(1.5)
#     webbrowser.open(f"http://localhost:{PORT}")
# threading.Thread(target=open_browser, daemon=True).start()
    print(f"Server started at http://localhost:{PORT}")
    
    def open_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{PORT}")
    
    threading.Thread(target=open_browser, daemon=True).start()
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.shutdown()

if __name__ == "__main__":
    start_server()