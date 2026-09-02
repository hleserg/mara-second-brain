#!/usr/bin/env python3
"""Аудио звонка → JSONL с сегментами и спанами (ТЗ §8).

Whisper на bigpc принимает не больше тридцати секунд и отвечает 413 на всё
длиннее («режь на стороне слушателя») — режем здесь, кусками по 25 секунд с
перекрытием в две. Перекрытие затем, что фраза, разорванная по живому, теряет
последнее слово в одном куске и первое в другом.

Спаны получаются с точностью до куска. Это честно и этого хватает, чтобы по
команде «покажи цитату» открыть нужное место записи. Пословные таймстемпы
требуют return_timestamps в generate, то есть правки /root/tts/server.py —
файла проекта голосовой маски, не этого репозитория. Отдельным шагом, когда
точность начнёт мешать.

Диаризации нет: все сегменты помечаются unknown-A. Пайплайн из-за её
отсутствия не встаёт (ТЗ §8), поле перепишет отдельная работа, когда появится.

    python3 scripts/call_asr.py --event call_<uuid>
    python3 scripts/call_asr.py --self-check
"""
import os, sys, json, argparse, subprocess, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

ASR_URL = os.environ.get("MARA_ASR_URL", "http://192.168.1.10:8770")
WINDOW_MS = int(os.environ.get("MARA_ASR_WINDOW_MS", 25000))
OVERLAP_MS = int(os.environ.get("MARA_ASR_OVERLAP_MS", 2000))
HTTP_TIMEOUT = 300


def slice_plan(duration_ms, window_ms=WINDOW_MS, overlap_ms=OVERLAP_MS):
    """Границы кусков в миллисекундах от начала записи."""
    if duration_ms <= 0:
        return []
    if duration_ms <= window_ms:
        return [(0, duration_ms)]
    step, out, start = window_ms - overlap_ms, [], 0
    while start < duration_ms:
        end = min(start + window_ms, duration_ms)
        out.append((start, end))
        if end >= duration_ms:
            break
        start += step
    return out


def duration_ms(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError("ffprobe: " + r.stderr.strip()[:200])
    return int(float(r.stdout.strip()) * 1000)


def cut_wav(path, start_ms, end_ms):
    """Кусок в моно 16 кГц WAV прямо в память: на диск не кладём, чтобы не
    плодить копии личного разговора по временным каталогам."""
    r = subprocess.run(["ffmpeg", "-v", "error",
                        "-ss", "%.3f" % (start_ms / 1000.0),
                        "-t", "%.3f" % ((end_ms - start_ms) / 1000.0),
                        "-i", path, "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                       capture_output=True, timeout=300)
    if r.returncode:
        raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "replace")[-200:])
    return r.stdout


def transcribe_spans(base_url, plan, cutter):
    """Куски в whisper, ответы в сегменты со спанами в координатах записи."""
    segs = []
    for i, (a, b) in enumerate(plan, 1):
        req = urllib.request.Request(base_url + "/transcribe", data=cutter(a, b),
                                     method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            d = json.loads(r.read() or b"{}")
        text = (d.get("text") or "").strip()
        if not text:
            continue                       # тишина сегментом не становится
        segs.append({"segment_id": "s%04d" % i, "start_ms": a, "end_ms": b,
                     "speaker": "unknown-A", "text": text,
                     "asr_confidence": None, "speaker_confidence": None})
    return segs


def write_jsonl(path, segs):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for s in segs:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def run(event_id, root=None):
    root = root or mi.ROOT
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    if not ev["blob_sha256"]:
        raise RuntimeError("у события %s нет аудио" % event_id)
    b = con.execute("select path, purged_at from blobs where sha256=?",
                    (ev["blob_sha256"],)).fetchone()
    if not b or not b["path"] or not os.path.exists(b["path"]):
        raise RuntimeError("блоб %s не на диске" % ev["blob_sha256"][:12])
    audio = b["path"]
    plan = slice_plan(duration_ms(audio))
    segs = transcribe_spans(ASR_URL, plan, lambda x, y: cut_wav(audio, x, y))
    out = write_jsonl(mi.transcript_path(root, event_id), segs)
    con.execute("update events set state='transcribed' where id=?", (event_id,))
    print("call_asr: %s — кусков %d, сегментов %d" % (event_id, len(plan), len(segs)))
    return out


def self_check():
    assert slice_plan(10000) == [(0, 10000)], "короткий звонок должен быть одним куском"
    p = slice_plan(60000)
    assert p[0] == (0, 25000) and p[1][0] == 23000, "перекрытие потерялось"
    assert p[-1][1] == 60000, "хвост записи потерялся"
    assert all(b - a <= WINDOW_MS for a, b in slice_plan(3600000)), "кусок длиннее окна"
    assert slice_plan(0) == []
    missing = [t for t in ("ffmpeg", "ffprobe")
               if subprocess.run(["which", t], capture_output=True).returncode]
    if missing:
        print("call_asr self-check: нарезка ок, нет %s — транскрипция не пойдёт"
              % ", ".join(missing))
        return 0
    print("call_asr self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="транскрипция звонка кусками")
    ap.add_argument("--event")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.event:
        ap.error("нужен --event")
    mi.ROOT = a.root
    run(a.event, a.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
