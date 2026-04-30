"""
ChatVault — YouTube Live Chat Auto-Downloader
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors multiple YouTube channels simultaneously.
When a stream goes live → downloads chat JSON → saves to Supabase.

Deploy on:  Render.com (free) / Railway / any Python host
NOT Netlify/Vercel — those can't run background processes.

Install:
    pip install -r requirements.txt

Run:
    python worker.py

Environment variables (set in .env or host dashboard):
    SUPABASE_URL        = https://xxxx.supabase.co
    SUPABASE_KEY        = your-service-role-key
    SUPABASE_BUCKET     = chatvault          (storage bucket name)
    CHECK_INTERVAL      = 120               (seconds between checks)
    CHANNELS            = comma-separated list of YouTube channel URLs
                          e.g. https://youtube.com/@Streamer1,https://youtube.com/@Streamer2
"""

import os, json, time, threading, subprocess, shutil, uuid, datetime, re, sys
from pathlib import Path

# ── Load .env if present ──────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "chatvault")
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "120"))
CHANNELS_RAW    = os.environ.get("CHANNELS", "")
CHANNELS        = [c.strip() for c in CHANNELS_RAW.split(",") if c.strip()]

TEMP_DIR = Path("./tmp_chats")
TEMP_DIR.mkdir(exist_ok=True)

# ── Find executables ──────────────────────────────────────────────────────────
def find_exe(name):
    found = shutil.which(name)
    if found:
        return found
    for c in [
        os.path.expanduser(f"~/.local/bin/{name}"),
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
        os.path.expanduser(f"~/AppData/Roaming/Python/Scripts/{name}.exe"),
    ]:
        if os.path.isfile(c):
            return c
    return name

YTDLP  = find_exe("yt-dlp")
CHATDL = find_exe("chat_downloader")

# ── State (in-memory) ─────────────────────────────────────────────────────────
# channel_url -> set of stream URLs already captured this session
captured = {}
# stream_url -> thread (currently downloading)
active_downloads = {}
lock = threading.Lock()

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

# ── Supabase client (pure HTTP, no extra SDK needed) ──────────────────────────
def sb_upload(local_path: Path, storage_key: str) -> str | None:
    """Upload a file to Supabase Storage, return public URL."""
    try:
        import urllib.request
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{storage_key}"
        with open(local_path, "rb") as f:
            data = f.read()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "apikey":        SUPABASE_KEY,
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{storage_key}"
    except Exception as e:
        log(f"Storage upload failed: {e}", "ERROR")
        return None


def sb_insert(table: str, row: dict) -> bool:
    """Insert a row into a Supabase table via REST API."""
    try:
        import urllib.request, json as _json
        url  = f"{SUPABASE_URL}/rest/v1/{table}"
        data = _json.dumps(row).encode()
        req  = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey":        SUPABASE_KEY,
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        log(f"DB insert failed: {e}", "ERROR")
        return False


def sb_list(table: str) -> list:
    """Fetch all rows from a Supabase table."""
    try:
        import urllib.request, json as _json
        url = f"{SUPABASE_URL}/rest/v1/{table}?order=created_at.desc"
        req = urllib.request.Request(
            url, method="GET",
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey":        SUPABASE_KEY,
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        log(f"DB list failed: {e}", "ERROR")
        return []

# ── yt-dlp helpers ────────────────────────────────────────────────────────────
def get_live_stream(channel_url: str) -> tuple[bool, str | None, str | None, str | None]:
    """
    Check if a channel is live right now.
    Returns (is_live, stream_url, title, channel_name)
    """
    try:
        r = subprocess.run(
            [YTDLP,
             "--flat-playlist", "--dump-json",
             "--match-filter", "is_live",
             "--playlist-end", "1",
             "--no-warnings",
             channel_url],
            capture_output=True, text=True, timeout=30, errors="replace"
        )
        if r.returncode == 0 and r.stdout.strip():
            lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
            for line in lines:
                try:
                    data = json.loads(line)
                    vid  = data.get("url") or data.get("id","")
                    if vid and not vid.startswith("http"):
                        vid = f"https://www.youtube.com/watch?v={vid}"
                    if vid:
                        return True, vid, data.get("title","Live Stream"), data.get("channel","")
                except Exception:
                    pass
    except subprocess.TimeoutExpired:
        log(f"Timeout checking {channel_url}", "WARN")
    except Exception as e:
        log(f"Error checking {channel_url}: {e}", "ERROR")
    return False, None, None, None


def get_channel_name(channel_url: str) -> str:
    """Extract channel name from URL or fetch it."""
    # Try to extract from URL pattern
    m = re.search(r"@([\w\-]+)", channel_url)
    if m:
        return m.group(1)
    m = re.search(r"/c/([\w\-]+)", channel_url)
    if m:
        return m.group(1)
    return channel_url.split("/")[-1] or "unknown"


def parse_ndjson_chat(path: Path) -> list:
    """Parse yt-dlp live_chat json3 NDJSON into clean message list."""
    messages = []
    t0 = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj  = json.loads(line)
                    rcia = obj.get("replayChatItemAction", {})
                    for action in rcia.get("actions", []):
                        item = action.get("addChatItemAction", {}).get("item", {})
                        renderer = (
                            item.get("liveChatTextMessageRenderer") or
                            item.get("liveChatPaidMessageRenderer")
                        )
                        if not renderer:
                            continue
                        text   = "".join(r.get("text","") for r in renderer.get("message",{}).get("runs",[]))
                        author = renderer.get("authorName",{}).get("simpleText","")
                        money  = renderer.get("purchaseAmountText",{}).get("simpleText")
                        badges = [
                            b.get("liveChatAuthorBadgeRenderer",{}).get("tooltip","")
                            for b in renderer.get("authorBadges",[])
                        ]
                        ts = renderer.get("timestampUsec")
                        if ts:
                            ts = int(ts)
                            if t0 is None:
                                t0 = ts
                            t_sec = max(0, (ts - t0) / 1e6)
                        else:
                            t_sec = int(rcia.get("videoOffsetTimeMsec",0)) / 1000
                        if text:
                            messages.append({
                                "time_in_seconds": round(t_sec, 3),
                                "author_name":     author,
                                "message":         text,
                                "money_amount":    money,
                                "badges":          [b for b in badges if b],
                            })
                except Exception:
                    pass
    except Exception as e:
        log(f"Parse error: {e}", "ERROR")
    return messages


# ── Chat download + upload ────────────────────────────────────────────────────
def download_and_save_chat(stream_url: str, title: str, channel_name: str):
    """
    Download live chat for a stream and upload to Supabase.
    Runs in its own thread.
    """
    stream_id = str(uuid.uuid4())[:8]
    safe_title = re.sub(r"[^\w\s\-]", "_", title).strip()[:60]
    now        = datetime.datetime.utcnow()
    date_str   = now.strftime("%Y-%m-%d")
    time_str   = now.strftime("%H-%M-%S")
    filename   = f"{channel_name}_{date_str}_{time_str}_{safe_title}.json"
    tmp_raw    = TEMP_DIR / f"{stream_id}_raw.json"
    tmp_final  = TEMP_DIR / f"{stream_id}_chat.json"

    log(f"[{channel_name}] 💬 Starting chat download: {title}")

    try:
        # Method 1: chat_downloader (primary)
        log(f"[{channel_name}] Trying chat_downloader...")
        result = subprocess.run(
            [CHATDL, stream_url,
             "--output", str(tmp_raw),
             "--message_groups", "all"],
            timeout=7200,   # 2 hour max (stream length)
            capture_output=True, text=True, errors="replace"
        )
        chat_written = tmp_raw.exists() and tmp_raw.stat().st_size > 100

        # Method 2: yt-dlp live_chat subtitles fallback
        if not chat_written:
            log(f"[{channel_name}] Trying yt-dlp live_chat subs...")
            sub_base = TEMP_DIR / f"{stream_id}_sub"
            subprocess.run(
                [YTDLP, "--skip-download",
                 "--write-subs", "--write-auto-subs",
                 "--sub-langs", "live_chat",
                 "--sub-format", "json3",
                 "--no-playlist",
                 "-o", str(sub_base) + ".%(ext)s",
                 stream_url],
                timeout=300, capture_output=True, text=True, errors="replace"
            )
            for cand in [
                TEMP_DIR / f"{stream_id}_sub.live_chat.json3",
                TEMP_DIR / f"{stream_id}_sub.en.live_chat.json3",
            ]:
                if cand.exists() and cand.stat().st_size > 100:
                    tmp_raw = cand
                    chat_written = True
                    log(f"[{channel_name}] Got live chat via yt-dlp subs")
                    break

        if not chat_written:
            log(f"[{channel_name}] ⚠️ No chat data — stream may not have live chat", "WARN")
            return

        # Parse into clean JSON array
        messages = parse_ndjson_chat(tmp_raw)

        # If empty (raw is already clean JSON array from chat_downloader)
        if not messages:
            try:
                with open(tmp_raw, encoding="utf-8") as f:
                    raw = f.read().strip()
                if raw.startswith("["):
                    messages = json.loads(raw)
                else:
                    # NDJSON from chat_downloader
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            text = obj.get("message","") or obj.get("body","")
                            author = (obj.get("author") or {}).get("name","") or obj.get("author_name","")
                            t = obj.get("time_in_seconds",0)
                            if text:
                                messages.append({
                                    "time_in_seconds": t,
                                    "author_name":     author,
                                    "message":         text,
                                    "money_amount":    obj.get("money_amount"),
                                    "badges":          [],
                                })
                        except Exception:
                            pass
            except Exception as e:
                log(f"[{channel_name}] Fallback parse error: {e}", "ERROR")

        if not messages:
            log(f"[{channel_name}] ⚠️ Zero messages parsed", "WARN")
            return

        log(f"[{channel_name}] ✅ Parsed {len(messages):,} messages")

        # Save clean JSON
        with open(tmp_final, "w", encoding="utf-8") as f:
            json.dump({
                "meta": {
                    "stream_url":   stream_url,
                    "title":        title,
                    "channel":      channel_name,
                    "date":         date_str,
                    "downloaded_at": now.isoformat() + "Z",
                    "total_messages": len(messages),
                },
                "messages": messages
            }, f, ensure_ascii=False)

        log(f"[{channel_name}] ☁️  Uploading to Supabase Storage...")
        storage_key = f"{channel_name}/{date_str}/{filename}"
        public_url  = sb_upload(tmp_final, storage_key)

        if public_url:
            log(f"[{channel_name}] ✅ Uploaded: {public_url}")
        else:
            log(f"[{channel_name}] ⚠️ Upload failed — saving locally only", "WARN")

        # Insert metadata row into database
        sb_insert("chat_downloads", {
            "id":             stream_id,
            "channel":        channel_name,
            "title":          title,
            "stream_url":     stream_url,
            "date":           date_str,
            "message_count":  len(messages),
            "storage_key":    storage_key,
            "public_url":     public_url or "",
            "filename":       filename,
            "created_at":     now.isoformat() + "Z",
        })
        log(f"[{channel_name}] ✅ Metadata saved to database")

    except subprocess.TimeoutExpired:
        log(f"[{channel_name}] Stream timed out after 2h — saving what we have")
    except Exception as e:
        log(f"[{channel_name}] ❌ Error: {e}", "ERROR")
    finally:
        # Cleanup temp files
        for f in [tmp_raw, tmp_final]:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
        with lock:
            active_downloads.pop(stream_url, None)
        log(f"[{channel_name}] Done.")


# ── Check all channels (parallel) ─────────────────────────────────────────────
def check_all_channels():
    """Check every channel in parallel, start downloads for any that are live."""
    if not CHANNELS:
        log("No channels configured. Set CHANNELS env var.", "WARN")
        return

    threads = []
    for ch_url in CHANNELS:
        t = threading.Thread(target=check_one_channel, args=(ch_url,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=45)   # don't block the loop for too long


def check_one_channel(channel_url: str):
    channel_name = get_channel_name(channel_url)
    log(f"[{channel_name}] Checking...")

    is_live, stream_url, title, ch_name = get_live_stream(channel_url)
    ch_label = ch_name or channel_name

    if not is_live or not stream_url:
        log(f"[{channel_name}] Not live")
        return

    with lock:
        already_captured = stream_url in captured.get(channel_url, set())
        already_downloading = stream_url in active_downloads

    if already_captured:
        log(f"[{channel_name}] 🔴 Live ({title}) — already captured")
        return

    if already_downloading:
        log(f"[{channel_name}] 🔴 Live ({title}) — download in progress")
        return

    log(f"[{channel_name}] 🔴 LIVE! Starting chat download: {title}")

    with lock:
        captured.setdefault(channel_url, set()).add(stream_url)
        t = threading.Thread(
            target=download_and_save_chat,
            args=(stream_url, title, ch_label),
            daemon=True
        )
        active_downloads[stream_url] = t
        t.start()


# ── Validation ────────────────────────────────────────────────────────────────
def validate_config():
    ok = True
    if not SUPABASE_URL:
        log("Missing SUPABASE_URL env var", "ERROR"); ok = False
    if not SUPABASE_KEY:
        log("Missing SUPABASE_KEY env var", "ERROR"); ok = False
    if not CHANNELS:
        log("Missing CHANNELS env var — no channels to monitor", "WARN")
    if not shutil.which("yt-dlp") and not os.path.isfile(find_exe("yt-dlp")):
        log("yt-dlp not found — install: pip install yt-dlp", "WARN")
    if not shutil.which("chat_downloader") and not os.path.isfile(find_exe("chat_downloader")):
        log("chat_downloader not found — install: pip install chat-downloader", "WARN")
    return ok


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  🎯  ChatVault — YouTube Live Chat Archiver")
    print(f"  📡  Monitoring {len(CHANNELS)} channel(s)")
    for ch in CHANNELS:
        print(f"      • {ch}")
    print(f"  ⏱   Check interval: {CHECK_INTERVAL}s")
    print(f"  ☁️   Supabase: {'✅ configured' if SUPABASE_URL else '❌ not set'}")
    print("=" * 60)

    if not validate_config():
        log("Fix config errors above, then restart", "ERROR")
        # Don't exit — still run so host doesn't crash the service

    cycle = 0
    while True:
        cycle += 1
        log(f"── Cycle {cycle} — checking {len(CHANNELS)} channel(s) ──")
        check_all_channels()
        active = len(active_downloads)
        if active:
            log(f"Active downloads: {active}")
        log(f"Sleeping {CHECK_INTERVAL}s…")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
