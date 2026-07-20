"""
Sarvam Doc Helper
=================
Point a phone camera at a document, then ask questions about it out loud in
your own language and hear the answer back.

Built entirely on Sarvam's stack:
    Saaras v3    speech -> text (handles code-mixed Indic speech)
    Doc AI       scanned page -> structured markdown
    sarvam-105b  grounded reasoning over the extracted text
    Bulbul v2    text -> speech
    Mayura v1    translation

Run:  python app.py     (then open http://localhost:8000)
"""

import json
import os
import re
import tempfile
import threading
import time
import zipfile

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file
from sarvamai import SarvamAI

load_dotenv()

API_KEY = os.getenv("SARVAM_API_KEY")
if not API_KEY:
    raise SystemExit(
        "\n  SARVAM_API_KEY is not set.\n"
        "  Copy .env.example to .env and paste your key from https://dashboard.sarvam.ai\n"
    )

API_BASE = "https://api.sarvam.ai"
CHAT_MODEL = "sarvam-105b"          # /v1/models also exposes sarvam-30b
STT_MODEL = "saaras:v3"
TTS_MODEL = "bulbul:v2"
TRANSLATE_MODEL = "mayura:v1"

MAX_UPLOAD_MB = 20
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".pdf", ".webp", ".tiff"}
SESSION_TTL = 60 * 60               # drop idle sessions after an hour
MAX_HISTORY_TURNS = 6               # keep the prompt small; reasoning is the cost centre
DOC_CHAR_BUDGET = 24_000

# Bulbul speaks these. Anything else falls back to Hindi.
TTS_LANGUAGES = {
    "hi-IN", "bn-IN", "kn-IN", "ml-IN", "mr-IN",
    "od-IN", "pa-IN", "ta-IN", "te-IN", "gu-IN", "en-IN",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

client = SarvamAI(api_subscription_key=API_KEY)
_headers = {"api-subscription-key": API_KEY}
_json_headers = {**_headers, "Content-Type": "application/json"}


# ----------------------------------------------------------------------------
# session state
# ----------------------------------------------------------------------------
sessions = {}
_lock = threading.Lock()


def get_state(sid):
    """Fetch (or create) a session, and opportunistically evict stale ones."""
    now = time.time()
    with _lock:
        for key in [k for k, v in sessions.items() if now - v["touched"] > SESSION_TTL]:
            sessions.pop(key, None)

        if sid not in sessions:
            sessions[sid] = {
                "document": None,
                "doc_status": "none",
                "doc_name": None,
                "doc_error": None,
                "history": [],
                "reply_language": None,   # set by /transcribe from Saaras detection
                "touched": now,
            }
        sessions[sid]["touched"] = now
        return sessions[sid]


# ----------------------------------------------------------------------------
# Sarvam helpers
# ----------------------------------------------------------------------------
def synth_speech(text, language="hi-IN"):
    """Bulbul v2. Returns base64 wav, or None if synthesis fails."""
    if not text:
        return None
    if language not in TTS_LANGUAGES:
        language = "hi-IN"
    try:
        r = requests.post(
            f"{API_BASE}/text-to-speech",
            headers=_json_headers,
            json={
                "inputs": [text[:2500]],
                "target_language_code": language,
                "model": TTS_MODEL,
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["audios"][0]
    except Exception as exc:
        app.logger.warning("TTS failed: %s", exc)
        return None


def chat(messages, stream=False, reasoning_effort="high", timeout=180):
    """POST to Sarvam's OpenAI-compatible chat endpoint."""
    return requests.post(
        f"{API_BASE}/v1/chat/completions",
        headers=_json_headers,
        json={
            "model": CHAT_MODEL,
            "messages": messages,
            "stream": stream,
            "reasoning_effort": reasoning_effort,
        },
        stream=stream,
        timeout=timeout,
    )


def iter_chat_stream(response):
    """Yield ('reasoning'|'content', text) tuples from an SSE chat response.

    sarvam-105b is a reasoning model: it emits several hundred
    `reasoning_content` deltas before the first `content` delta arrives.
    We surface that as a live 'thinking' state instead of a dead spinner.
    """
    for raw in response.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        payload = raw[6:].strip()
        if payload == "[DONE]":
            return
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("reasoning_content"):
            yield "reasoning", delta["reasoning_content"]
        if delta.get("content"):
            yield "content", delta["content"]


def sse(event, **data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ----------------------------------------------------------------------------
# pages / health
# ----------------------------------------------------------------------------
@app.route("/")
def home():
    return send_file("index.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/health")
def health():
    with _lock:
        active = len(sessions)
    return jsonify({"ok": True, "model": CHAT_MODEL, "sessions": active})


# ----------------------------------------------------------------------------
# speech to text  (Saaras v3)
# ----------------------------------------------------------------------------
@app.route("/transcribe", methods=["POST"])
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "no audio uploaded"}), 400

    sid = request.form.get("session", "default")
    audio = request.files["audio"]
    try:
        r = requests.post(
            f"{API_BASE}/speech-to-text",
            headers=_headers,
            files={"file": ("recording.wav", audio.read(), "audio/wav")},
            data={"model": STT_MODEL, "language_code": "unknown"},
            timeout=90,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        app.logger.error("STT failed: %s", exc)
        return jsonify({"error": "transcription failed"}), 502

    # Saaras auto-detects the language. Remember it so the spoken reply comes
    # back in the same voice the user actually used.
    detected = body.get("language_code")
    if detected:
        state = get_state(sid)
        state["reply_language"] = detected if detected in TTS_LANGUAGES else "hi-IN"

    return jsonify(body)


# ----------------------------------------------------------------------------
# text to speech  (Bulbul v2)
# ----------------------------------------------------------------------------
@app.route("/speak", methods=["POST"])
def speak():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not text:
        return jsonify({"error": "text required"}), 400
    return jsonify({"audio": synth_speech(text, body.get("language", "hi-IN"))})


# ----------------------------------------------------------------------------
# translation  (Mayura v1)
# ----------------------------------------------------------------------------
@app.route("/translate", methods=["POST"])
def translate():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not text:
        return jsonify({"error": "text required"}), 400
    try:
        r = requests.post(
            f"{API_BASE}/translate",
            headers=_json_headers,
            json={
                "input": text,
                "source_language_code": body.get("source", "auto"),
                "target_language_code": body.get("target", "kn-IN"),
                "model": TRANSLATE_MODEL,
            },
            timeout=60,
        )
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as exc:
        app.logger.error("translate failed: %s", exc)
        return jsonify({"error": "translation failed"}), 502


# ----------------------------------------------------------------------------
# document ingestion  (Sarvam Doc AI)
# ----------------------------------------------------------------------------
def process_document(filepath, language, sid):
    """Background worker: digitize the upload and cache the markdown.

    Doc AI hands back a ZIP of per-page markdown, so we unpack and join it.
    """
    state = get_state(sid)
    try:
        job = client.document_intelligence.create_job(
            language=language, output_format="md"
        )
        job.upload_file(filepath)
        job.start()
        status = job.wait_until_complete(timeout=600)

        if status.job_state not in ("Completed", "PartiallyCompleted"):
            state["doc_status"] = "failed"
            state["doc_error"] = f"Doc AI returned state: {status.job_state}"
            return

        outdir = tempfile.mkdtemp()
        zip_path = os.path.join(outdir, "output.zip")
        job.download_output(zip_path)

        parts = []
        with zipfile.ZipFile(zip_path) as z:
            for name in sorted(z.namelist()):
                if name.endswith((".md", ".html", ".txt")):
                    parts.append(z.read(name).decode("utf-8", errors="ignore"))

        text = "\n\n".join(parts).strip()
        if text:
            state["document"] = text[:DOC_CHAR_BUDGET]
            state["doc_status"] = "ready"
            state["history"] = []
        else:
            state["doc_status"] = "failed"
            state["doc_error"] = "No text could be read from this document."
    except Exception as exc:
        app.logger.error("Doc AI failed: %s", exc)
        state["doc_status"] = "failed"
        state["doc_error"] = str(exc)
    finally:
        try:
            os.unlink(filepath)
        except OSError:
            pass


@app.route("/document", methods=["POST"])
def document():
    """Accept the upload, start digitizing in the background, return at once."""
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400

    sid = request.form.get("session", "default")
    language = request.form.get("language", "hi-IN")
    upload = request.files["file"]

    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(
            {"error": f"Unsupported file type '{ext or 'unknown'}'. "
                      f"Use one of: {', '.join(sorted(ALLOWED_EXT))}"}
        ), 400

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    upload.save(tmp.name)
    tmp.close()

    state = get_state(sid)
    state.update(
        document=None,
        doc_status="processing",
        doc_error=None,
        doc_name=upload.filename,
        history=[],
    )

    threading.Thread(
        target=process_document, args=(tmp.name, language, sid), daemon=True
    ).start()
    return jsonify({"status": "processing"})


@app.route("/document/status")
def document_status():
    state = get_state(request.args.get("session", "default"))
    return jsonify(
        {
            "status": state["doc_status"],
            "name": state["doc_name"],
            "error": state["doc_error"],
            "chars": len(state["document"] or ""),
        }
    )


@app.route("/document/text")
def document_text():
    """Lets the UI show exactly what the model can see. No hidden context."""
    state = get_state(request.args.get("session", "default"))
    if state["doc_status"] != "ready":
        return jsonify({"error": "no document"}), 404
    return jsonify({"text": state["document"], "name": state["doc_name"]})


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": f"File too large. Limit is {MAX_UPLOAD_MB} MB."}), 413


# ----------------------------------------------------------------------------
# structured field extraction
# ----------------------------------------------------------------------------
EXTRACT_PROMPT = (
    "You extract structured data from documents.\n"
    "Return ONLY a JSON object - no prose, no markdown fences.\n"
    "Each requested field becomes a key. Use the exact field name given.\n"
    "If a field is genuinely absent from the document, set it to null.\n"
    "Copy values verbatim from the document; never invent or infer them.\n"
)


@app.route("/extract", methods=["POST"])
def extract():
    """Describe the fields you want in plain English, get JSON back.

    Doc AI gives us clean text; sarvam-105b turns a plain-English field list
    into structured output grounded in that text.
    """
    body = request.get_json(silent=True) or {}
    sid = body.get("session", "default")
    fields = (body.get("fields") or "").strip()
    state = get_state(sid)

    if state["doc_status"] != "ready":
        return jsonify({"error": "Upload a document first."}), 400
    if not fields:
        return jsonify({"error": "Describe the fields you want."}), 400

    messages = [
        {
            "role": "system",
            "content": f"{EXTRACT_PROMPT}\nDOCUMENT:\n{state['document']}",
        },
        {"role": "user", "content": f"Extract these fields: {fields}"},
    ]

    try:
        r = chat(messages, reasoning_effort="high")
        r.raise_for_status()
        raw = (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:
        app.logger.error("extract failed: %s", exc)
        return jsonify({"error": "Extraction failed. Try again."}), 502

    # The model is asked for bare JSON, but tolerate a fenced block.
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return jsonify({"fields": json.loads(cleaned)})
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return jsonify({"fields": json.loads(match.group(0))})
            except json.JSONDecodeError:
                pass
        return jsonify({"error": "Model did not return valid JSON.", "raw": raw}), 502


# ----------------------------------------------------------------------------
# grounded Q&A  (streamed)
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You answer questions about a document that the user has photographed.\n"
    "Rules:\n"
    "1. Use ONLY the document text below. Never guess or add outside knowledge.\n"
    "2. If the answer is not in the document, say so plainly.\n"
    "3. Match the language of the user's MOST RECENT question. Earlier turns in\n"
    "   this conversation may be in other languages - ignore them when choosing\n"
    "   your language. An English question gets an English answer even if the\n"
    "   previous question was in Hindi.\n"
    "4. Keep it to 1-3 short sentences - this is read aloud, not printed.\n"
    "5. Quote exact figures, dates and names as they appear.\n\n"
    "DOCUMENT:\n"
)

def build_recap(history):
    """Render prior turns as a system-prompt recap instead of chat messages.

    This looks roundabout, so: sarvam-105b picks its reply language from the
    assistant turns in the message list, and no instruction overrides it. Ask a
    question in Hindi, then follow up in English, and the English question still
    gets answered in Hindi - tested with directives on the user turn, on a
    trailing system message, and both at once. All of them lose to the history.

    Moving the same turns into the system prompt as plain recap text fixes it:
    the model still resolves "and what about the deposit?" against the previous
    question, but takes its language from the live user turn. Verified in both
    directions (Hindi -> English and English -> Hindi).
    """
    turns = history[-MAX_HISTORY_TURNS * 2:]
    if not turns:
        return ""

    lines = [
        "\n\nEARLIER IN THIS CONVERSATION (context only - do not let the "
        "language of these earlier turns influence your reply):"
    ]
    for msg in turns:
        who = "User asked" if msg["role"] == "user" else "You answered"
        lines.append(f"- {who}: {msg['content']}")
    return "\n".join(lines)


LANGUAGE_NAMES = {
    "hi-IN": "Hindi", "bn-IN": "Bengali", "kn-IN": "Kannada",
    "ml-IN": "Malayalam", "mr-IN": "Marathi", "od-IN": "Odia",
    "pa-IN": "Punjabi", "ta-IN": "Tamil", "te-IN": "Telugu",
    "gu-IN": "Gujarati", "en-IN": "English",
}


@app.route("/respond", methods=["POST"])
def respond():
    """Server-sent events: thinking -> answer tokens -> audio.

    Streaming matters here. The model spends most of its wall-clock time
    reasoning before the first answer token, so the UI shows live progress
    rather than freezing.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    sid = body.get("session", "default")
    state = get_state(sid)

    if not text:
        return jsonify({"error": "text required"}), 400

    # Explicit override wins; otherwise use whatever Saaras detected.
    language = body.get("language") or state["reply_language"]

    if state["doc_status"] != "ready" or not state["document"]:
        msg = "कृपया पहले कोई दस्तावेज़ अपलोड कीजिए, फिर उसके बारे में पूछिए।"

        def no_doc():
            yield sse("answer", text=msg)
            yield sse("audio", audio=synth_speech(msg, "hi-IN"))
            yield sse("done")

        return Response(no_doc(), mimetype="text/event-stream")

    prompt = SYSTEM_PROMPT + state["document"] + build_recap(state["history"])

    spoken = LANGUAGE_NAMES.get(language)
    if spoken:
        prompt += f"\n\nThe user's current question is in {spoken}. Answer in {spoken}."

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": text},
    ]

    def generate():
        answer = ""
        reasoning_chars = 0
        started = time.time()
        try:
            r = chat(messages, stream=True, reasoning_effort="high")
            r.raise_for_status()

            for kind, piece in iter_chat_stream(r):
                if kind == "reasoning":
                    reasoning_chars += len(piece)
                    # Throttle: a heartbeat every ~400 chars of reasoning.
                    if reasoning_chars % 400 < len(piece):
                        yield sse(
                            "thinking",
                            elapsed=round(time.time() - started, 1),
                            chars=reasoning_chars,
                        )
                else:
                    answer += piece
                    yield sse("answer", text=answer)

            answer = answer.strip()
            if not answer:
                answer = "माफ़ कीजिए, अभी जवाब नहीं बन पाया। कृपया दोबारा पूछिए।"
                yield sse("answer", text=answer)

            # Persist the turn so follow-ups ("and the deposit?") work.
            state["history"].append({"role": "user", "content": text})
            state["history"].append({"role": "assistant", "content": answer})
            state["history"] = state["history"][-MAX_HISTORY_TURNS * 2:]

            yield sse("audio", audio=synth_speech(answer, language))
            yield sse("done", elapsed=round(time.time() - started, 1))
        except Exception as exc:
            app.logger.error("respond failed: %s", exc)
            yield sse("error", message="Something went wrong. Please ask again.")

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    print(f"  Sarvam Doc Helper  ->  http://localhost:8000   (model: {CHAT_MODEL})")
    app.run(port=8000, debug=False, threaded=True)
