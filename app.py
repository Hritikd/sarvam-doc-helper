import os
import json
import zipfile
import threading
import tempfile
import requests
from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv
from sarvamai import SarvamAI

load_dotenv()
API_KEY = os.getenv("SARVAM_API_KEY")

app = Flask(__name__)
client = SarvamAI(api_subscription_key=API_KEY)

# ---------- per-session memory ----------
conversations = {}

def get_state(sid):
    if sid not in conversations:
        conversations[sid] = {"document": None, "doc_status": "none"}
    return conversations[sid]

# ---------- pages ----------
@app.route("/")
def home():
    return send_file("index.html")

# ---------- speech to text (Saaras) ----------
@app.route("/transcribe", methods=["POST"])
def transcribe():
    audio = request.files["audio"]
    files = {"file": ("recording.wav", audio.read(), "audio/wav")}
    data = {"model": "saaras:v3", "language_code": "unknown"}
    headers = {"api-subscription-key": API_KEY}
    r = requests.post("https://api.sarvam.ai/speech-to-text",
                      headers=headers, files=files, data=data)
    return jsonify(r.json())

# ---------- text to speech (Bulbul) ----------
def synth_speech(text, language="hi-IN"):
    headers = {"api-subscription-key": API_KEY, "Content-Type": "application/json"}
    payload = {"inputs": [text], "target_language_code": language, "model": "bulbul:v2"}
    r = requests.post("https://api.sarvam.ai/text-to-speech",
                      headers=headers, json=payload)
    try:
        return r.json()["audios"][0]
    except Exception:
        return None

@app.route("/speak", methods=["POST"])
def speak():
    body = request.get_json()
    audio = synth_speech(body["text"], body.get("language", "hi-IN"))
    return jsonify({"audio": audio})

# ---------- translation (Mayura) ----------
@app.route("/translate", methods=["POST"])
def translate():
    body = request.get_json()
    headers = {"api-subscription-key": API_KEY, "Content-Type": "application/json"}
    payload = {
        "input": body["text"],
        "source_language_code": "auto",
        "target_language_code": body.get("target", "kn-IN"),
        "model": "mayura:v1"
    }
    r = requests.post("https://api.sarvam.ai/translate", headers=headers, json=payload)
    return jsonify(r.json())

# ---------- document upload + background processing (Sarvam Vision) ----------
def process_document(filepath, language, sid):
    """Runs in a background thread: sends the file to Document Intelligence,
    waits for the job, extracts the text, stores it in the session."""
    state = get_state(sid)
    try:
        job = client.document_intelligence.create_job(
            language=language,
            output_format="md"
        )
        job.upload_file(filepath)
        job.start()
        status = job.wait_until_complete()

        if status.job_state not in ("Completed", "PartiallyCompleted"):
            state["doc_status"] = "failed"
            return

        # Output arrives as a ZIP; pull the markdown text out of it
        outdir = tempfile.mkdtemp()
        zip_path = os.path.join(outdir, "output.zip")
        job.download_output(zip_path)

        text_parts = []
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                if name.endswith(".md"):
                    text_parts.append(z.read(name).decode("utf-8", errors="ignore"))

        doc_text = "\n\n".join(text_parts).strip()
        if doc_text:
            state["document"] = doc_text
            state["doc_status"] = "ready"
        else:
            state["doc_status"] = "failed"
    except Exception as e:
        print("Document processing error:", e)
        state["doc_status"] = "failed"

@app.route("/document", methods=["POST"])
def document():
    """Receives the uploaded photo/PDF, kicks off background processing,
    returns immediately so the browser can poll."""
    sid = request.form.get("session", "default")
    language = request.form.get("language", "hi-IN")
    upload = request.files["file"]

    suffix = os.path.splitext(upload.filename)[1] or ".jpg"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    upload.save(tmp.name)

    state = get_state(sid)
    state["document"] = None
    state["doc_status"] = "processing"

    threading.Thread(target=process_document,
                     args=(tmp.name, language, sid), daemon=True).start()

    return jsonify({"status": "processing"})

@app.route("/document/status", methods=["GET"])
def document_status():
    """The polling endpoint: browser asks every couple of seconds."""
    sid = request.args.get("session", "default")
    state = get_state(sid)
    return jsonify({"status": state["doc_status"]})

# ---------- the brain: grounded Q&A over the document ----------
@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/respond", methods=["POST"])
def respond():
    data = request.get_json()
    text = data["text"]
    sid = data.get("session", "default")
    state = get_state(sid)

    doc_text = state.get("document")
    if not doc_text:
        reply = "कृपया पहले कोई दस्तावेज़ अपलोड कीजिए, फिर उसके बारे में पूछिए।"
        return jsonify({"reply": reply, "audio": synth_speech(reply)})

    system_prompt = (
        "You are a helpful assistant that answers questions about a document. "
        "The document's extracted text is provided below. Answer the user's "
        "question using ONLY the information in the document. If the answer "
        "is not in the document, say so honestly. Reply in the same language "
        "the user asked in, in 1-3 short sentences suitable for being spoken aloud.\n\n"
        "DOCUMENT:\n" + doc_text
    )

    headers = {"api-subscription-key": API_KEY, "Content-Type": "application/json"}
    payload = {
        "model": "sarvam-105b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    }
    r = requests.post("https://api.sarvam.ai/v1/chat/completions",
                      headers=headers, json=payload)
    try:
        reply = r.json()["choices"][0]["message"]["content"]
    except Exception:
        reply = "माफ़ कीजिए, अभी जवाब नहीं दे पाया। कृपया फिर से पूछिए।"

    return jsonify({"reply": reply, "audio": synth_speech(reply)})

if __name__ == "__main__":
    app.run(port=8000, debug=False)