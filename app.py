from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "jobs"
JOBS.mkdir(exist_ok=True)
MAX_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "7200"))
CLIP_COUNT = 5
MIN_CLIP = 30.0
MAX_CLIP = 60.0

app = FastAPI(title="ClipMaker for iPhone")


class JobRequest(BaseModel):
    url: str


class JobState(BaseModel):
    id: str
    status: str
    progress: int = 0
    message: str = ""
    clips: list[str] = []
    zip_url: str | None = None


_states: dict[str, JobState] = {}
_lock = threading.Lock()


def update(job_id: str, **changes) -> None:
    with _lock:
        current = _states[job_id].model_dump()
        current.update(changes)
        _states[job_id] = JobState(**current)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout)[-3000:])
    return proc.stdout


def valid_youtube_url(raw: str) -> bool:
    try:
        host = (urlparse(raw).hostname or "").lower()
        return host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
    except ValueError:
        return False


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9 _-]", "", value).strip()
    return (value[:70] or "clip").replace(" ", "_")


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    score: float = 0


def download_video(url: str, folder: Path) -> tuple[Path, str, float]:
    metadata = run([
        "yt-dlp", "--dump-single-json", "--no-playlist", "--skip-download", url
    ])
    info = json.loads(metadata)
    duration = float(info.get("duration") or 0)
    if duration <= 0 or duration > MAX_SECONDS:
        raise RuntimeError(f"Video duration must be between 1 second and {MAX_SECONDS // 60} minutes")
    title = info.get("title") or "YouTube video"
    output = folder / "source.%(ext)s"
    run([
        "yt-dlp", "--no-playlist", "--no-progress",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]",
        "--merge-output-format", "mp4", "-o", str(output), url
    ])
    candidates = [p for p in folder.glob("source.*") if p.suffix not in {".part", ".ytdl"}]
    if not candidates:
        raise RuntimeError("Video download did not produce a file")
    return candidates[0], title, duration


def transcribe(url: str, folder: Path) -> tuple[list[Word], list[Segment]]:
    # Reuse YouTube's timed English captions. This avoids loading a large speech
    # model in the small web container and gives us timing for animated text.
    template = folder / "captions.%(ext)s"
    run([
        "yt-dlp", "--no-playlist", "--skip-download", "--write-subs",
        "--write-auto-subs", "--sub-langs", "en.*,en", "--sub-format", "json3",
        "-o", str(template), url,
    ])
    caption_files = sorted(folder.glob("captions*.json3"))
    if not caption_files:
        raise RuntimeError("This video has no English captions. Try a video with English subtitles enabled.")
    data = json.loads(caption_files[0].read_text(encoding="utf-8"))
    words: list[Word] = []
    segments: list[Segment] = []
    for event in data.get("events", []):
        start = float(event.get("tStartMs", 0)) / 1000
        duration = float(event.get("dDurationMs", 0)) / 1000
        text = "".join(x.get("utf8", "") for x in event.get("segs", []))
        text = text.replace("\n", " ").strip()
        if not text or text.startswith("["):
            continue
        end = start + max(duration, 0.15)
        segments.append(Segment(start, end, text))
        tokens = text.split()
        step = max(0.08, (end - start) / max(1, len(tokens)))
        for index, token in enumerate(tokens):
            word_start = start + index * step
            words.append(Word(word_start, min(end, word_start + step), token))
    if not segments:
        raise RuntimeError("English captions were found but contained no usable speech.")
    return words, segments


HOOKS = {
    "secret", "never", "crazy", "insane", "surprised", "danger", "extreme",
    "best", "worst", "impossible", "first", "last", "million", "mistake",
    "why", "how", "but", "actually", "finally", "unbelievable", "challenge",
}


def candidate_windows(segments: list[Segment], duration: float) -> list[Segment]:
    windows: list[Segment] = []
    # Start at natural transcript boundaries and build 38-55 second moments.
    for i in range(0, len(segments), 2):
        start = max(0.0, segments[i].start - 0.35)
        target = min(duration, start + 48)
        j = i
        text: list[str] = []
        while j < len(segments) and segments[j].start < target:
            text.append(segments[j].text)
            j += 1
        if j == i:
            continue
        end = min(duration, max(start + MIN_CLIP, segments[j - 1].end + 0.4))
        end = min(end, start + MAX_CLIP)
        if end - start < MIN_CLIP - 0.5:
            continue
        joined = " ".join(text).strip()
        lower = joined.lower()
        hook_score = sum(2.2 for hook in HOOKS if re.search(rf"\b{re.escape(hook)}\b", lower))
        punctuation = lower.count("!") * 2.0 + lower.count("?") * 1.3
        numbers = min(4, len(re.findall(r"\b\d+[\d,.]*\b", lower))) * 0.7
        density = min(4.0, len(joined.split()) / max(1.0, end - start) * 1.1)
        early_bonus = max(0, 1.5 - start / max(duration, 1) * 1.5)
        windows.append(Segment(start, end, joined, hook_score + punctuation + numbers + density + early_bonus))
    return windows


def choose_clips(segments: list[Segment], duration: float) -> list[Segment]:
    ranked = sorted(candidate_windows(segments, duration), key=lambda s: s.score, reverse=True)
    chosen: list[Segment] = []
    for item in ranked:
        if all(min(item.end, x.end) - max(item.start, x.start) < 8 for x in chosen):
            chosen.append(item)
        if len(chosen) == CLIP_COUNT:
            break
    # For short or low-speech videos, fill evenly while keeping 30-60 seconds.
    if len(chosen) < CLIP_COUNT and duration >= MIN_CLIP:
        step = max(MIN_CLIP, duration / CLIP_COUNT)
        for index in range(CLIP_COUNT * 2):
            start = min(max(0, index * step), max(0, duration - MIN_CLIP))
            fallback = Segment(start, min(duration, start + 45), "Highlight", 0)
            if all(abs(fallback.start - x.start) > 15 for x in chosen):
                chosen.append(fallback)
            if len(chosen) == CLIP_COUNT:
                break
    return sorted(chosen[:CLIP_COUNT], key=lambda s: s.start)


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def make_captions(words: list[Word], clip: Segment, path: Path) -> None:
    local = [w for w in words if w.end >= clip.start and w.start <= clip.end]
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Pop,DejaVu Sans,82,&H00FFFFFF,&H0000FFFF,&H00101010,&H70000000,-1,0,0,0,100,100,0,0,1,7,2,2,80,80,320,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = [header]
    group: list[Word] = []
    for word in local:
        group.append(word)
        span = group[-1].end - group[0].start
        if len(group) >= 4 or span >= 1.25 or word.text.endswith((".", "!", "?", ",")):
            start = max(0, group[0].start - clip.start)
            end = min(clip.end - clip.start, group[-1].end - clip.start + 0.08)
            text = " ".join(w.text for w in group).upper()
            effect = r"{\fad(60,90)\fscx108\fscy108\t(0,130,\fscx100\fscy100)}"
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Pop,,0,0,0,,{effect}{ass_escape(text)}\n")
            group = []
    if group:
        start = max(0, group[0].start - clip.start)
        end = min(clip.end - clip.start, group[-1].end - clip.start + 0.15)
        text = " ".join(w.text for w in group).upper()
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Pop,,0,0,0,,{{\fad(60,90)}}{ass_escape(text)}\n")
    path.write_text("".join(lines), encoding="utf-8")


def make_title(text: str, index: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .,!?")
    words = cleaned.split()[:8]
    title = " ".join(words)
    return title if title else f"Best Moment {index}"


def render_clip(video: Path, words: list[Word], segment: Segment, output: Path, index: int) -> None:
    ass = output.with_suffix(".ass")
    make_captions(words, segment, ass)
    duration = segment.end - segment.start
    # The file path is generated by us and contains no quotes.
    filters = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,"
        f"subtitles={ass.as_posix()}"
    )
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{segment.start:.3f}", "-i", str(video), "-t", f"{duration:.3f}",
        "-vf", filters, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output)
    ])
    ass.unlink(missing_ok=True)


def process_job(job_id: str, url: str) -> None:
    folder = JOBS / job_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        update(job_id, status="working", progress=5, message="Downloading video")
        video, source_title, duration = download_video(url, folder)
        update(job_id, progress=25, message="Finding the strongest moments")
        words, transcript = transcribe(url, folder)
        picks = choose_clips(transcript, duration)
        if not picks:
            raise RuntimeError("Could not find enough spoken content for clips")
        outputs: list[Path] = []
        for idx, pick in enumerate(picks, 1):
            update(job_id, progress=30 + idx * 11, message=f"Rendering clip {idx} of {len(picks)}")
            title = safe_name(make_title(pick.text, idx))
            output = folder / f"{idx:02d}_{title}.mp4"
            render_clip(video, words, pick, output, idx)
            outputs.append(output)
        manifest = {
            "source_title": source_title,
            "source_url": url,
            "clips": [
                {"file": p.name, "start": round(s.start, 2), "end": round(s.end, 2), "title": make_title(s.text, i)}
                for i, (p, s) in enumerate(zip(outputs, picks), 1)
            ],
        }
        (folder / "clips.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        bundle = folder / "clips.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in outputs:
                archive.write(path, path.name)
            archive.write(folder / "clips.json", "clips.json")
        video.unlink(missing_ok=True)
        update(
            job_id, status="done", progress=100, message="Your clips are ready",
            clips=[f"/api/jobs/{job_id}/files/{p.name}" for p in outputs],
            zip_url=f"/api/jobs/{job_id}/zip",
        )
    except Exception as exc:
        message = str(exc).strip() or f"{type(exc).__name__}: processing failed"
        print(f"Job {job_id} failed: {type(exc).__name__}: {message}", flush=True)
        update(job_id, status="error", message=message, progress=0)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ClipMaker</title><style>body{font-family:-apple-system;background:#08090c;color:white;margin:0;padding:28px}main{max-width:520px;margin:8vh auto}h1{font-size:48px;line-height:.95}p{color:#aaa;line-height:1.5}input,button,a{box-sizing:border-box;width:100%;padding:17px;border-radius:14px;font-size:16px;margin-top:12px}input{background:#15171d;color:white;border:1px solid #333}button,a{border:0;background:#c8ff41;color:#111;font-weight:800;text-align:center;display:block;text-decoration:none}.bar{height:8px;background:#292d37;margin-top:20px}.fill{height:100%;background:#c8ff41;width:0}.msg{color:#aaa}</style></head><body><main><h1>One link.<br>Five clips.</h1><p>Five vertical 30–60 second videos with animated English captions.</p><input id="u" placeholder="Paste YouTube link"><button id="b">Make my clips</button><div class="bar"><div class="fill" id="f"></div></div><p class="msg" id="m"></p><div id="d"></div></main><script>const q=new URLSearchParams(location.search).get('url');if(q)u.value=q;const sleep=n=>new Promise(r=>setTimeout(r,n));b.onclick=async()=>{b.disabled=true;d.innerHTML='';try{let r=await fetch('/api/jobs',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({url:u.value.trim()})}),j=await r.json();if(!r.ok)throw Error(j.detail);for(;;){await sleep(2500);let s=await fetch('/api/jobs/'+j.id).then(x=>x.json());f.style.width=s.progress+'%';m.textContent=s.message;if(s.status==='error')throw Error(s.message);if(s.status==='done'){s.clips.forEach((x,i)=>d.innerHTML+=`<a href="${x}">Download clip ${i+1}</a>`);d.innerHTML+=`<a href="${s.zip_url}">Download all 5 clips</a>`;break}}}catch(e){m.textContent=e.message}finally{b.disabled=false}}</script></body></html>"""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/jobs", response_model=JobState)
def create_job(request: JobRequest, tasks: BackgroundTasks) -> JobState:
    if not valid_youtube_url(request.url):
        raise HTTPException(400, "Please enter a valid YouTube link")
    job_id = uuid.uuid4().hex
    state = JobState(id=job_id, status="queued", message="Queued")
    with _lock:
        _states[job_id] = state
    tasks.add_task(process_job, job_id, request.url)
    return state


@app.get("/api/jobs/{job_id}", response_model=JobState)
def job_status(job_id: str) -> JobState:
    if job_id not in _states:
        raise HTTPException(404, "Job not found")
    return _states[job_id]


@app.get("/api/jobs/{job_id}/files/{filename}")
def clip_file(job_id: str, filename: str):
    path = (JOBS / job_id / Path(filename).name)
    if not path.exists() or path.suffix != ".mp4":
        raise HTTPException(404, "Clip not found")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/zip")
def clip_zip(job_id: str):
    path = JOBS / job_id / "clips.zip"
    if not path.exists():
        raise HTTPException(404, "Clips are not ready")
    return FileResponse(path, media_type="application/zip", filename="youtube_clips.zip")
