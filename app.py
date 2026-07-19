import os
import json
import requests
from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("SARVAM_API_KEY")

app = Flask(__name__)

# --- a tiny in-memory "calendar": True = free, False = taken ---
available_slots = {
    ("Tuesday", "06:00"): False,   # taken -> triggers the alternative
    ("Tuesday", "07:00"): True,
    ("Tuesday", "18:00"): False,   # taken -> triggers the alternative
    ("Tuesday", "19:00"): True,
    ("Wednesday", "18:00"): True,
    ("Monday", "18:00"): True,
    ("Friday", "18:00"): True,
}

# remembers a slot we offered but the user hasn't confirmed yet (this is "state")
conversations = {}   # holds state per session

def get_state(sid):
    if sid not in conversations:
        conversations[sid] = {"pending": None, "history": []}
    return conversations[sid]


@app.route("/")
def home():
    return send_file("index.html")


@app.route("/transcribe", methods=["POST"])
def transcribe():
    audio = request.files["audio"]
    files = {"file": ("recording.wav", audio.read(), "audio/wav")}
    data = {"model": "saaras:v3", "language_code": "unknown"}
    headers = {"api-subscription-key": API_KEY}
    r = requests.post("https://api.sarvam.ai/speech-to-text",
                      headers=headers, files=files, data=data)
    return jsonify(r.json())


def extract_intent(transcript):
    system_prompt = (
        "You are a booking assistant. From the user's sentence, extract the "
        "action (book, cancel, or reschedule), the day (a weekday like Monday), "
        "and the time. Reply with ONLY a JSON object and nothing else, exactly: "
        '{"action": "...", "day": "...", "time": "..."}. '
        "Use 24-hour time like 18:00. If any field is missing, set it to \"unknown\"."
    )
    headers = {"api-subscription-key": API_KEY, "Content-Type": "application/json"}
    payload = {
        "model": "sarvam-105b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript}
        ]
    }
    r = requests.post("https://api.sarvam.ai/v1/chat/completions",
                      headers=headers, json=payload)

    print("LLM status code:", r.status_code)      # 200 = ok, 429 = rate limit, 401/403 = auth
    print("LLM raw response:", r.text)             # the exact message from Sarvam

    try:
        raw = r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None
    return safe_parse_json(raw)


def safe_parse_json(text):
    if not text:                      # None or empty -> no crash, just fail gracefully
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def synth_speech(text, language="hi-IN"):
    headers = {"api-subscription-key": API_KEY, "Content-Type": "application/json"}
    payload = {"inputs": [text], "target_language_code": language, "model": "bulbul:v2"}
    r = requests.post("https://api.sarvam.ai/text-to-speech",
                      headers=headers, json=payload)
    try:
        return r.json()["audios"][0]
    except Exception:
        return None


def find_alternative(day):
    for (d, t), free in available_slots.items():
        if d == day and free:
            return t
    return None


@app.route("/respond", methods=["POST"])
def respond():
    data = request.get_json()
    text = data["text"]
    sid = data.get("session", "default")
    state = get_state(sid)
    lower = text.lower()

    yes_words = ["haan", "haa", "ha", "yes", "yeah", "yep", "theek", "thik",
                 "ok", "okay", "kar do", "kardo", "karo", "sure", "ji",
                 "हाँ", "हा", "ठीक", "जी", "कर दो", "करो", "बुक"]

    # confirmation of a pending offer
    if state["pending"] and any(w in lower for w in yes_words):
        d, t = state["pending"]["day"], state["pending"]["time"]
        available_slots[(d, t)] = False
        reply = f"{d} {t} का स्लॉट बुक हो गया है।"
        state["pending"] = None
        return jsonify({"intent": None, "reply": reply, "audio": synth_speech(reply)})

    intent = extract_intent(text)
    if not intent:
        reply = "माफ़ कीजिए, मुझे समझ नहीं आया।"
        return jsonify({"intent": None, "reply": reply, "audio": synth_speech(reply)})

    day, time = intent.get("day"), intent.get("time")

    if day == "unknown" or time == "unknown":
        reply = "कृपया बताइए किस दिन और किस समय का अपॉइंटमेंट चाहिए?"
    elif available_slots.get((day, time)) is True:
        available_slots[(day, time)] = False
        reply = f"{day} {time} का स्लॉट बुक हो गया है।"
    elif available_slots.get((day, time)) is False:
        alt = find_alternative(day)
        if alt:
            state["pending"] = {"day": day, "time": alt}
            reply = f"{time} का स्लॉट भरा हुआ है। {day} {alt} उपलब्ध है। क्या मैं बुक कर दूँ?"
        else:
            reply = f"{day} को कोई स्लॉट उपलब्ध नहीं है।"
    else:
        reply = f"{day} {time} के लिए कोई स्लॉट नहीं है।"

    return jsonify({"intent": intent, "reply": reply, "audio": synth_speech(reply)})

if __name__ == "__main__":
    app.run(port=8000, debug=False)