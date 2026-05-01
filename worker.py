"""
ChatVault Worker v3.0
- Fixed: yt-dlp auto-updates on start (fixes bot detection)
- Fixed: get_video_info now logs errors properly  
- Fixed: chat_downloader stderr captured and logged
- Fixed: yt-dlp subs uses cookies workaround for age/auth
- Added: verbose logging so you can see exactly what fails
"""
import os, json, time, threading, subprocess, shutil, uuid, datetime, re
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "chatvault")
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "120"))
CHANNELS        = [c.strip() for c in os.environ.get("CHANNELS","").split(",") if c.strip()]
PORT            = int(os.environ.get("PORT", "8080"))

TEMP_DIR = Path("./tmp_chats")
TEMP_DIR.mkdir(exist_ok=True)

# Write YouTube cookies from env var to a file (bypasses bot detection on cloud servers)
COOKIES_FILE = None
_cookies_content = os.environ.get("YOUTUBE_COOKIES", "").strip()
if _cookies_content:
    COOKIES_FILE = str(TEMP_DIR / "yt_cookies.txt")
    with open(COOKIES_FILE, "w", encoding="utf-8") as _cf:
        _cf.write(_cookies_content)
    print("[INIT] YouTube cookies loaded from env var")
else:
    print("[INIT] No YOUTUBE_COOKIES env var — YouTube may block cloud server IP")

def find_exe(name):
    found = shutil.which(name)
    if found: return found
    for c in [
        os.path.expanduser(f"~/.local/bin/{name}"),
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
        os.path.expanduser(f"~/AppData/Roaming/Python/Scripts/{name}.exe"),
    ]:
        if os.path.isfile(c): return c
    return name

def ytdlp_cmd(*args):
    """Build a yt-dlp command, injecting cookies if available."""
    cmd = [YTDLP] + list(args)
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        cmd = [YTDLP, "--cookies", COOKIES_FILE] + list(args)
    return cmd

YTDLP  = find_exe("yt-dlp")
CHATDL = find_exe("chat_downloader")

captured         = {}
active_downloads = {}
lock             = threading.Lock()

def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

import urllib.request as _ur

def sb_upload(local_path, storage_key):
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{storage_key}"
        with open(local_path, "rb") as f:
            data = f.read()
        req = _ur.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "apikey":        SUPABASE_KEY,
        })
        with _ur.urlopen(req, timeout=120):
            pass
        pub = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{storage_key}"
        log(f"Uploaded -> {pub}")
        return pub
    except Exception as e:
        log(f"Upload failed: {e}", "ERROR")
        return None

def sb_insert(table, row):
    try:
        url  = f"{SUPABASE_URL}/rest/v1/{table}"
        data = json.dumps(row).encode()
        req  = _ur.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apikey":        SUPABASE_KEY,
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        })
        with _ur.urlopen(req, timeout=15):
            pass
        return True
    except Exception as e:
        log(f"DB insert failed: {e}", "ERROR")
        return False

def update_ytdlp():
    """Update yt-dlp to latest — critical, YouTube breaks old versions."""
    log("Updating yt-dlp to latest version...")
    try:
        r = subprocess.run(
            ["pip", "install", "--upgrade", "--quiet", "yt-dlp"],
            capture_output=True, text=True, timeout=120
        )
        log("yt-dlp updated OK")
    except Exception as e:
        log(f"yt-dlp update failed (non-fatal): {e}", "WARN")

def get_video_info(url):
    """Fetch video metadata — title, channel, etc."""
    log(f"Fetching metadata for: {url}")
    try:
        r = subprocess.run(
            ytdlp_cmd(
             "--dump-json",
             "--no-playlist",
             "--no-warnings",
             "--extractor-retries", "3",
             "--socket-timeout", "30",
             url),
            capture_output=True, text=True, timeout=60, errors="replace"
        )
        if r.stdout.strip():
            data = json.loads(r.stdout.strip().splitlines()[0])
            log(f"Metadata OK: title={data.get('title','?')}, channel={data.get('uploader','?')}")
            return data
        else:
            log(f"yt-dlp metadata stderr: {r.stderr[:500]}", "WARN")
    except Exception as e:
        log(f"get_video_info error: {e}", "ERROR")
    return {}

def check_channel_live(channel_url):
    live_url = channel_url.rstrip("/") + "/live"
    try:
        r = subprocess.run(
            ytdlp_cmd("--dump-json", "--no-playlist", "--no-warnings",
             "--socket-timeout", "20", live_url),
            capture_output=True, text=True, timeout=40, errors="replace"
        )
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.strip().splitlines():
                try:
                    data = json.loads(line)
                    live_status = data.get("live_status", "")
                    is_live = data.get("is_live") is True or live_status == "is_live"
                    if is_live:
                        vid = data.get("webpage_url") or f"https://youtube.com/watch?v={data.get('id','')}"
                        return True, vid, data.get("title","Live Stream"), data.get("uploader","")
                except Exception:
                    pass
    except Exception as e:
        log(f"check_live error: {e}", "WARN")
    return False, None, None, None

def get_channel_name(url):
    m = re.search(r"@([\w\-]+)", url)
    if m: return m.group(1)
    m = re.search(r"/c/([\w\-]+)", url)
    if m: return m.group(1)
    return url.split("/")[-1] or "unknown"

def parse_chat_file(path):
    messages = []
    t0 = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read().strip()
        if not raw:
            return []

        if raw.startswith("["):
            for m in json.loads(raw):
                text   = m.get("message") or m.get("body","")
                author = (m.get("author") or {}).get("name","") or m.get("author_name","")
                t      = float(m.get("time_in_seconds", 0) or 0)
                if text:
                    messages.append({"time_in_seconds": round(t,3), "author_name": author,
                                     "message": text, "money_amount": m.get("money_amount"), "badges": []})

        elif '"replayChatItemAction"' in raw:
            for line in raw.splitlines():
                line = line.strip()
                if not line.startswith("{"): continue
                try:
                    obj  = json.loads(line)
                    rcia = obj.get("replayChatItemAction", {})
                    for action in rcia.get("actions", []):
                        item = action.get("addChatItemAction",{}).get("item",{})
                        r    = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                        if not r: continue
                        text   = "".join(x.get("text","") for x in r.get("message",{}).get("runs",[]))
                        author = r.get("authorName",{}).get("simpleText","")
                        money  = r.get("purchaseAmountText",{}).get("simpleText")
                        ts     = r.get("timestampUsec")
                        if ts:
                            ts = int(ts)
                            if t0 is None: t0 = ts
                            t_sec = max(0, (ts - t0) / 1e6)
                        else:
                            t_sec = int(rcia.get("videoOffsetTimeMsec",0)) / 1000
                        if text:
                            messages.append({"time_in_seconds": round(t_sec,3), "author_name": author,
                                             "message": text, "money_amount": money, "badges": []})
                except Exception:
                    pass
        else:
            for line in raw.splitlines():
                line = line.strip()
                if not line: continue
                try:
                    m      = json.loads(line)
                    text   = m.get("message") or m.get("body","")
                    author = (m.get("author") or {}).get("name","") or m.get("author_name","")
                    t      = float(m.get("time_in_seconds", 0) or 0)
                    if text:
                        messages.append({"time_in_seconds": round(t,3), "author_name": author,
                                         "message": text, "money_amount": m.get("money_amount"), "badges": []})
                except Exception:
                    pass

    except Exception as e:
        log(f"parse error: {e}", "ERROR")
    return messages

def download_chat(video_url, title, channel, job_id):
    def upd(status, msg):
        log(f"[{channel}] {msg}")
        with lock:
            if video_url in active_downloads:
                active_downloads[video_url]["status"] = status
                active_downloads[video_url]["log"] += msg + "\n"

    upd("running", f"Starting: {title}")
    now   = datetime.datetime.now(datetime.timezone.utc)
    ds    = now.strftime("%Y-%m-%d")
    ts    = now.strftime("%H-%M-%S")
    safe  = re.sub(r"[^\w\s\-]", "_", title).strip()[:60]
    fname = f"{channel}_{ds}_{ts}_{safe}.json"
    raw   = TEMP_DIR / f"{job_id}_raw.json"
    final = TEMP_DIR / f"{job_id}_chat.json"
    got   = False

    try:
        # ── Method 1: chat_downloader ─────────────────────────────────────────
        upd("running", "Trying chat_downloader...")
        try:
            r = subprocess.run(
                [CHATDL, video_url,
                 "--output", str(raw),
                 "--message_groups", "all"],
                capture_output=True, text=True,
                timeout=7200, errors="replace"
            )
            # Log stderr so we can see what happened
            if r.stderr:
                upd("running", f"chat_downloader stderr: {r.stderr[:300]}")
            if raw.exists() and raw.stat().st_size > 50:
                got = True
                upd("running", f"chat_downloader OK ({raw.stat().st_size:,} bytes)")
            else:
                upd("running", f"chat_downloader returned nothing (exit={r.returncode})")
        except subprocess.TimeoutExpired:
            upd("running", "chat_downloader timed out (stream may have ended)")
            got = raw.exists() and raw.stat().st_size > 50
        except Exception as e:
            upd("running", f"chat_downloader error: {e}")

        # ── Method 2: yt-dlp live_chat subs ──────────────────────────────────
        if not got:
            upd("running", "Trying yt-dlp --write-subs live_chat...")
            sub = TEMP_DIR / f"{job_id}_sub"
            try:
                r2 = subprocess.run(
                    ytdlp_cmd(
                     "--skip-download",
                     "--write-subs",
                     "--write-auto-subs",
                     "--sub-langs", "live_chat",
                     "--sub-format", "json3",
                     "--no-playlist",
                     "--extractor-retries", "3",
                     "--socket-timeout", "30",
                     "-o", str(sub) + ".%(ext)s",
                     video_url),
                    capture_output=True, text=True,
                    timeout=300, errors="replace"
                )
                if r2.stderr:
                    upd("running", f"yt-dlp stderr: {r2.stderr[:400]}")
                for cand in [
                    TEMP_DIR / f"{job_id}_sub.live_chat.json3",
                    TEMP_DIR / f"{job_id}_sub.en.live_chat.json3",
                ]:
                    if cand.exists() and cand.stat().st_size > 50:
                        raw = cand
                        got = True
                        upd("running", f"yt-dlp subs OK ({cand.stat().st_size:,} bytes)")
                        break
                if not got:
                    upd("running", f"yt-dlp subs: no file found (exit={r2.returncode})")
            except Exception as e:
                upd("running", f"yt-dlp subs error: {e}")

        if not got:
            upd("error", "No chat found. Possible reasons:\n"
                         "  1. Video is not a live stream / VOD with chat\n"
                         "  2. yt-dlp is outdated (auto-update runs on startup)\n"
                         "  3. YouTube is blocking the server IP\n"
                         "  4. Chat was disabled by the streamer")
            return

        upd("running", "Parsing messages...")
        msgs = parse_chat_file(raw)

        if not msgs:
            upd("error", "File downloaded but 0 messages parsed. File may be empty or wrong format.")
            return

        upd("running", f"Parsed {len(msgs):,} messages")

        output = {
            "meta": {
                "stream_url":     video_url,
                "title":          title,
                "channel":        channel,
                "date":           ds,
                "downloaded_at":  now.isoformat(),
                "total_messages": len(msgs),
            },
            "messages": msgs,
        }
        with open(final, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False)

        upd("running", "Uploading to Supabase...")
        key = f"{channel}/{ds}/{fname}"
        pub = sb_upload(final, key) if SUPABASE_URL else None
        sb_insert("chat_downloads", {
            "id":            job_id,
            "channel":       channel,
            "title":         title,
            "stream_url":    video_url,
            "date":          ds,
            "message_count": len(msgs),
            "storage_key":   key,
            "public_url":    pub or "",
            "filename":      fname,
            "created_at":    now.isoformat(),
        })
        upd("done", f"Done! {len(msgs):,} messages saved -> {fname}")

    except Exception as e:
        upd("error", f"Unexpected error: {e}")
    finally:
        for f in [raw, final]:
            try:
                if Path(f).exists(): Path(f).unlink()
            except Exception:
                pass
        with lock:
            if video_url in active_downloads and active_downloads[video_url]["status"] == "running":
                active_downloads[video_url]["status"] = "done"

def start_job(vid_url, title, channel, channel_url=None):
    job_id = str(uuid.uuid4())[:8]
    now = datetime.datetime.now(datetime.timezone.utc)
    with lock:
        active_downloads[vid_url] = {
            "id":      job_id,
            "url":     vid_url,
            "title":   title,
            "channel": channel,
            "status":  "starting",
            "log":     "",
            "started": now.isoformat(),
        }
        if channel_url:
            captured.setdefault(channel_url, set()).add(vid_url)
    t = threading.Thread(target=download_chat, args=(vid_url, title, channel, job_id), daemon=True)
    t.start()
    return job_id

def check_and_start(channel_url):
    ch = get_channel_name(channel_url)
    log(f"[{ch}] Checking...")
    is_live, vid, title, uploader = check_channel_live(channel_url)
    if not is_live or not vid:
        log(f"[{ch}] Not live")
        return
    log(f"[{ch}] LIVE: {title}")
    with lock:
        already = vid in captured.get(channel_url, set()) or vid in active_downloads
    if already:
        log(f"[{ch}] Already capturing")
        return
    start_job(vid, title, uploader or ch, channel_url)

def monitor_loop():
    cycle = 0
    while True:
        cycle += 1
        if CHANNELS:
            log(f"Cycle {cycle} - checking {len(CHANNELS)} channel(s)")
            threads = [threading.Thread(target=check_and_start, args=(ch,), daemon=True) for ch in CHANNELS]
            for t in threads: t.start()
            for t in threads: t.join(timeout=50)
        log(f"Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)

# ── Flask API ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

@app.route("/")
def index():
    return jsonify({
        "service":  "ChatVault",
        "version":  "3.0",
        "channels": len(CHANNELS),
        "active":   len([v for v in active_downloads.values() if v.get("status") == "running"]),
        "ytdlp":    YTDLP,
        "chatdl":   CHATDL,
    })

@app.route("/status")
def status():
    with lock:
        jobs = [{k: v for k, v in j.items() if k != "thread"} for j in active_downloads.values()]
    return jsonify({"jobs": jobs, "channels": CHANNELS})

@app.route("/download", methods=["POST"])
def manual_download():
    data = request.get_json(force=True)
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    with lock:
        if url in active_downloads and active_downloads[url].get("status") == "running":
            return jsonify({"error": "Already downloading this URL"}), 409

    log(f"Manual download requested: {url}")
    info    = get_video_info(url)
    title   = info.get("title") or data.get("title") or "Unknown Stream"
    channel = info.get("uploader") or info.get("channel") or get_channel_name(url)
    job_id  = start_job(url, title, channel)
    return jsonify({"ok": True, "job_id": job_id, "title": title, "channel": channel})

@app.route("/job_status/<job_id>")
def job_status(job_id):
    with lock:
        job = next((j for j in active_downloads.values() if j.get("id") == job_id), None)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({k: v for k, v in job.items() if k != "thread"})

if __name__ == "__main__":
    print("=" * 55)
    print("  ChatVault Worker v3.0")
    print(f"  Channels: {len(CHANNELS)}")
    for ch in CHANNELS:
        print(f"    - {ch}")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"  Supabase: {'connected' if SUPABASE_URL else 'NOT SET - uploads disabled'}")
    print(f"  yt-dlp:   {YTDLP}")
    print(f"  chatdl:   {CHATDL}")
    print(f"  API port: {PORT}")
    print("=" * 55)

    # Auto-update yt-dlp on startup — prevents YouTube blocking old versions
    update_ytdlp()

    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
