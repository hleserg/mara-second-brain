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
import vault_common

# Файл читаем на импорте, а не лениво: ниже из окружения берутся ручки
# настройки, и после ленивого перехода они молча перестали читаться из файла —
# `нужен_адрес` вызывается уже после того, как константы вычислены. Тестовый
# процесс от этого больше не страдает: `run-tests.sh` уводит MARA_ENV_FILE в
# несуществующий файл.
vault_common.load_env()

ASR_URL = os.environ.get("MARA_ASR_URL") or None
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


def transcribe_spans(base_url, plan, cutter, движок=None):
    """Куски в whisper, ответы в сегменты со спанами в координатах записи.

    `движок` — словарь, в который кладутся `engine`/`model`/`language`, если
    коробка их сообщает (ADR-0004 п.4: что сообщает коробка, не догадка).
    """
    segs = []
    for i, (a, b) in enumerate(plan, 1):
        req = urllib.request.Request(base_url + "/transcribe", data=cutter(a, b),
                                     method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            d = json.loads(r.read() or b"{}")
        if движок is not None:
            for k in ("engine", "model", "language"):
                if d.get(k) and not движок.get(k):
                    движок[k] = str(d[k])
        text = (d.get("text") or "").strip()
        if not text:
            continue                       # тишина сегментом не становится
        segs.append({"segment_id": "s%04d" % i, "start_ms": a, "end_ms": b,
                     "speaker": "unknown-A", "text": text,
                     "asr_confidence": None, "speaker_confidence": None})
    return segs


def записать_сегменты(con, event_id, blob_sha256, segs, движок=None):
    """Т5.1, ADR-0004 п.1: расшифровка и её сегменты — строки реестра.

    Каждый прогон ASR заводит **новый** `transcripts` с новыми сегментами
    (переобработка по §9.1 старые строки не трогает — evidence остаётся
    приколоченным к той расшифровке, по которой его нашли). `seq` — номер
    из метки `s%04d`, по нему `call_extract` переводит метку модели в
    `segment_id`. Пишется одной транзакцией с переходом события; у
    вызывающего транзакция уже открыта — тогда это её шаг. Возвращает id
    расшифровки."""
    движок = движок or {}
    tid = mi.uuid7()
    with mi.транзакция(con):
        con.execute("insert into transcripts(id,event_id,blob_sha256,engine,model,"
                    "language,created) values(?,?,?,?,?,?,?)",
                    (tid, event_id, blob_sha256, движок.get("engine", "unknown"),
                     движок.get("model", "unknown"), движок.get("language"),
                     mi.now_iso()))
        for s in segs:
            con.execute("insert into transcript_segments(id,transcript_id,seq,start_ms,"
                        "end_ms,speaker,text) values(?,?,?,?,?,?,?)",
                        (mi.uuid7(), tid, int(s["segment_id"][1:]), s["start_ms"],
                         s["end_ms"], s.get("speaker"), s.get("text")))
    return tid


def сегменты_события(con, event_id):
    """Сегменты последней расшифровки события: `(transcript_id, {seq: row})`.
    Нет расшифровки — `(None, {})`."""
    t = con.execute("select id from transcripts where event_id=? order by created desc, "
                    "id desc limit 1", (event_id,)).fetchone()
    if not t:
        return None, {}
    rows = con.execute("select * from transcript_segments where transcript_id=? "
                       "order by seq", (t["id"],)).fetchall()
    return t["id"], {r["seq"]: r for r in rows}


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
    движок = {}
    segs = transcribe_spans(ASR_URL or vault_common.нужен_адрес(
        "MARA_ASR_URL", "коробка с whisper"), plan,
        lambda x, y: cut_wav(audio, x, y), движок)
    out = write_jsonl(mi.transcript_path(root, event_id), segs)
    # файл — для следующего шага, строки — для evidence (Т5.1, ADR-0004);
    # строки и переход события — одной транзакцией (§5.2)
    with mi.транзакция(con):
        записать_сегменты(con, event_id, ev["blob_sha256"], segs, движок)
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
