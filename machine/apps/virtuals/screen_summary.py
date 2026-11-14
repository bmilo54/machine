import base64
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, time, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.timezone import localtime
from PIL import Image

from apps.profiles.models import Profile
from apps.virtuals.models import ScreenSummary, WorkReport
from view_api.views import AlibabaApiError, describe_recordings, parse_alibaba_time, recording_signed_url

GEMINI_MODEL = "gemini-3.5-flash-lite"
FRAME_EVERY_SECONDS = 30
MAX_FRAMES = 24
LOCK_KEY = "screen-summary-lock"
SCREEN_PROMPT = (
    "This is one screenshot from a person's work computer. "
    "Reply with one short line naming the program in front and, if a browser is open, the website. "
    "If the screen is locked or unused, reply: Screen is idle. "
    "Do not add any other words."
)
REPORT_PROMPT = (
    "Write a short work report from these screen notes for one person on one day. "
    "Use three to six plain sentences. Say which computer, which programs and websites were open, "
    "and whether the screen stayed idle. Use only the notes. Do not mention recordings or AI."
)


def gemini_key():
    return (getattr(settings, "GEMINI_API_KEY", "") or "").strip()


def parse_date_range(raw_from, raw_to):
    raw_from = (raw_from or "").strip()
    raw_to = (raw_to or "").strip()
    if not raw_from or not raw_to:
        return None, ""
    try:
        start_date = datetime.strptime(raw_from, "%Y-%m-%d").date()
        end_date = datetime.strptime(raw_to, "%Y-%m-%d").date()
    except ValueError:
        return None, "Use a real start date and end date."
    if end_date < start_date:
        return None, "The end date has to be the same day or later than the start date."
    if (end_date - start_date).days > 30:
        return None, "Choose a range of 31 days or less."
    zone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(start_date, time.min), zone)
    end = timezone.make_aware(datetime.combine(end_date, time.max), zone)
    return (start, end, start_date.isoformat(), end_date.isoformat()), ""


def recording_rows(start, end, end_user_id="", desktop_id=""):
    raw = describe_recordings(start, end, desktop_id)
    names = {
        profile.eds_end_user_id: profile.user.username
        for profile in Profile.objects.exclude(eds_end_user_id="").select_related("user")
    }
    rows = []
    for item in raw:
        end_user_ids = item.get("EndUserIds") or []
        if isinstance(end_user_ids, str):
            end_user_ids = [end_user_ids]
        person = end_user_ids[0] if end_user_ids else ""
        if end_user_id and end_user_id not in end_user_ids:
            continue
        file_path = item.get("FilePath") or ""
        if not file_path.endswith(".mp4"):
            continue
        started = parse_alibaba_time(item.get("StartTime"))
        if started is not None and (started < start or started > end):
            continue
        rows.append({
            "started": started,
            "finished": parse_alibaba_time(item.get("EndTime")),
            "end_user_id": person,
            "username": names.get(person, ""),
            "desktop_name": item.get("DesktopName") or "",
            "desktop_id": item.get("DesktopId") or "",
            "file_path": file_path,
        })
    rows.sort(key=lambda row: row["started"] or start)
    return rows


def pending_count(rows):
    paths = [row["file_path"] for row in rows]
    done = set(
        ScreenSummary.objects.filter(status="done", file_path__in=paths).values_list("file_path", flat=True)
    )
    return sum(1 for row in rows if row["file_path"] not in done)


def work_notes(rows):
    paths = [row["file_path"] for row in rows]
    saved = ScreenSummary.objects.filter(status="done", file_path__in=paths)
    names = {row["end_user_id"]: row["username"] for row in rows}
    groups = {}
    for item in saved:
        when = item.started or item.created
        day = localtime(when).date()
        key = (item.end_user_id or "Unknown person", day)
        bucket = groups.setdefault(key, [])
        for line in item.lines or []:
            bucket.append(line)
    reports = {
        (item.end_user_id, item.day): item.report
        for item in WorkReport.objects.filter(end_user_id__in=[key[0] for key in groups])
    }
    notes = []
    for (person_id, day), lines in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0]), reverse=True):
        lines = sorted(lines, key=lambda line: line.get("at") or "")
        notes.append({
            "person": names.get(person_id) or person_id or "Unknown person",
            "day": day,
            "report": reports.get((person_id, day), ""),
            "lines": lines,
        })
    return notes


def summarize_next(start, end, end_user_id="", desktop_id=""):
    if not gemini_key():
        return {"ok": False, "error": "The Gemini key is missing from local.py."}
    if not cache.add(LOCK_KEY, "1", 300):
        return {"ok": False, "error": "A summary is already running."}
    try:
        rows = recording_rows(start, end, end_user_id, desktop_id)
        done = set(
            ScreenSummary.objects.filter(
                status="done",
                file_path__in=[row["file_path"] for row in rows],
            ).values_list("file_path", flat=True)
        )
        waiting = [row for row in rows if row["file_path"] not in done]
        if not waiting:
            return {"ok": True, "remaining": 0, "done": True}
        error = _summarize_one(waiting[0])
        remaining = len(waiting) - (0 if error else 1)
        if error:
            return {"ok": False, "error": error, "remaining": remaining}
        return {"ok": True, "remaining": remaining, "done": remaining == 0}
    except AlibabaApiError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        cache.delete(LOCK_KEY)


def _summarize_one(row):
    signed_url = recording_signed_url(row["file_path"])
    if not signed_url:
        _save(row, "failed", "This recording could not be opened.", [])
        return "This recording could not be opened."
    folder = tempfile.mkdtemp(prefix="screen-summary-")
    try:
        video_path = os.path.join(folder, "recording.mp4")
        try:
            _download(signed_url, video_path)
        except (HTTPError, URLError, TimeoutError, OSError):
            _save(row, "failed", "The recording file could not be downloaded.", [])
            return "The recording file could not be downloaded."
        frames = _frames(video_path, folder)
        if not frames:
            _save(row, "done", "No picture in this recording.", [])
            _refresh_report(row)
            return ""
        try:
            lines = _read_frames(frames, row.get("started"))
        except GeminiError as exc:
            _save(row, "failed", str(exc), [])
            return str(exc)
        _save(row, "done", "", lines)
        _refresh_report(row)
        return ""
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _save(row, status, detail, lines):
    ScreenSummary.objects.update_or_create(
        file_path=row["file_path"],
        defaults={
            "desktop_id": row.get("desktop_id") or "",
            "desktop_name": row.get("desktop_name") or "",
            "end_user_id": row.get("end_user_id") or "",
            "started": row.get("started"),
            "finished": row.get("finished"),
            "status": status,
            "detail": (detail or "")[:1000],
            "lines": lines,
        },
    )


def _download(url, path):
    request = Request(url, headers={"User-Agent": "machine-dashboard"})
    with urlopen(request, timeout=180) as response, open(path, "wb") as handle:
        while True:
            block = response.read(256 * 1024)
            if not block:
                break
            handle.write(block)


def _frames(video_path, folder):
    import imageio_ffmpeg

    pattern = os.path.join(folder, "frame_%03d.jpg")
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-i",
        video_path,
        "-vf",
        "fps=1/{},scale=640:-2".format(FRAME_EVERY_SECONDS),
        "-frames:v",
        str(MAX_FRAMES),
        "-q:v",
        "8",
        pattern,
    ]
    subprocess.run(command, check=False, capture_output=True, timeout=180)
    names = sorted(name for name in os.listdir(folder) if name.startswith("frame_"))
    return [os.path.join(folder, name) for name in names[:MAX_FRAMES]]


def _read_frames(frames, started):
    lines = []
    previous = None
    for index, path in enumerate(frames):
        moment = _moment(started, index * FRAME_EVERY_SECONDS)
        signature = _signature(path)
        same = previous is not None and _same_screen(previous, signature)
        previous = signature
        if same:
            if lines:
                lines[-1]["until"] = moment
            continue
        text = _ask_gemini(path)
        if lines and lines[-1]["text"] == text:
            lines[-1]["until"] = moment
        else:
            lines.append({"at": moment, "until": moment, "text": text})
    return lines


def _refresh_report(row):
    started = row.get("started")
    end_user_id = row.get("end_user_id") or ""
    if started is None or not end_user_id:
        return
    day = localtime(started).date()
    notes = []
    saved = ScreenSummary.objects.filter(status="done", end_user_id=end_user_id)
    for item in saved.order_by("started", "created"):
        when = item.started or item.created
        if localtime(when).date() != day:
            continue
        computer = item.desktop_name or "the computer"
        for line in item.lines or []:
            start_at = line.get("at") or ""
            until = line.get("until") or start_at
            span = start_at if until == start_at else "{}-{}".format(start_at, until)
            notes.append("{} on {}: {}".format(span, computer, line.get("text") or "Screen is idle"))
    person = row.get("username") or end_user_id
    if not notes:
        report = "Nothing was visible on the screen for this day."
    else:
        try:
            report = _ask_text(
                "{}\nPerson: {}\nDay: {}\nNotes:\n{}".format(
                    REPORT_PROMPT, person, day.strftime("%d %b %Y"), "\n".join(notes)
                ),
                500,
            )
        except GeminiError:
            report = " ".join(notes)
    WorkReport.objects.update_or_create(
        end_user_id=end_user_id,
        day=day,
        defaults={"report": report[:4000]},
    )


def _ask_text(prompt, max_tokens):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
    }
    request = Request(
        "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent".format(GEMINI_MODEL),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key()},
    )
    try:
        with urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise GeminiError(_gemini_message(raw) or "The report could not be written.") from exc
    except (URLError, TimeoutError) as exc:
        raise GeminiError("The screen reader could not be reached.") from exc
    return _reply_text(payload)


def _moment(started, offset_seconds):
    if started is None:
        minutes, seconds = divmod(offset_seconds, 60)
        return "{:02d}:{:02d}".format(minutes, seconds)
    return localtime(started + timedelta(seconds=offset_seconds)).strftime("%H:%M")


def _signature(path):
    with Image.open(path) as image:
        small = image.convert("L").resize((32, 18))
        return list(small.getdata())


def _same_screen(left, right):
    if len(left) != len(right) or not left:
        return False
    difference = sum(abs(a - b) for a, b in zip(left, right)) / len(left)
    return difference < 12


class GeminiError(Exception):
    pass


def _ask_gemini(path):
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    body = {
        "contents": [{
            "parts": [
                {"text": SCREEN_PROMPT},
                {"inline_data": {"mime_type": "image/jpeg", "data": encoded}},
            ]
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 80},
    }
    request = Request(
        "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent".format(GEMINI_MODEL),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key()},
    )
    try:
        with urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        message = _gemini_message(raw) or "The screen could not be read."
        raise GeminiError(message) from exc
    except (URLError, TimeoutError) as exc:
        raise GeminiError("The screen reader could not be reached.") from exc
    return (_reply_text(payload).splitlines() or ["Screen is idle"])[0].strip()[:180]


def _reply_text(payload):
    parts = (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return " ".join(part.get("text") or "" for part in parts).strip()


def _gemini_message(raw):
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    message = ((payload.get("error") or {}).get("message") or "").strip()
    key = gemini_key()
    if key:
        message = message.replace(key, "")
    return message[:240]
