"""Offline tests — no API key or network needed.

    python tests/test_app.py

The interesting one is the recap group. See build_recap() in app.py for why
conversation history lives in the system prompt instead of the message list.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SARVAM_API_KEY", "test-key-not-used")

import app  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  pass  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


print("\nbuild_recap")
check("empty history produces no recap", app.build_recap([]) == "")

history = [
    {"role": "user", "content": "मेरी सिक्योरिटी डिपॉज़िट कितनी है?"},
    {"role": "assistant", "content": "आपकी सिक्योरिटी डिपॉज़िट Rs. 1,71,000 है।"},
]
recap = app.build_recap(history)
check("recap includes the earlier question", "मेरी सिक्योरिटी डिपॉज़िट कितनी है?" in recap)
check("recap includes the earlier answer", "Rs. 1,71,000" in recap)
check("recap labels the speakers", "User asked" in recap and "You answered" in recap)
check(
    "recap warns against language anchoring",
    "language" in recap.lower(),
    "-> the warning line is what keeps a Hindi turn from forcing a Hindi reply",
)

long_history = [{"role": "user", "content": f"q{i}"} for i in range(40)]
check(
    "recap is capped to MAX_HISTORY_TURNS",
    app.build_recap(long_history).count("\n- ") <= app.MAX_HISTORY_TURNS * 2,
)


print("\nlanguage handling")
check("Saaras codes map to names", app.LANGUAGE_NAMES["ta-IN"] == "Tamil")
check(
    "every TTS language has a display name",
    set(app.TTS_LANGUAGES) <= set(app.LANGUAGE_NAMES),
)
check("unknown code has no display name", app.LANGUAGE_NAMES.get("xx-XX") is None)


print("\nsse framing")
frame = app.sse("answer", text="ठीक है")
check("frame names the event", frame.startswith("event: answer\n"))
check("frame ends with a blank line", frame.endswith("\n\n"))
check(
    "frame keeps Indic text unescaped",
    json.loads(frame.split("data: ", 1)[1].strip())["text"] == "ठीक है",
)


print("\nhttp surface")
app.app.config["TESTING"] = True
client = app.app.test_client()

check("GET /health is ok", client.get("/health").status_code == 200)
check("GET / serves the page", client.get("/").status_code == 200)

r = client.post("/document", data={"session": "t"})
check("upload with no file is rejected", r.status_code == 400)

r = client.post(
    "/document",
    data={"session": "t", "file": (__import__("io").BytesIO(b"x"), "virus.exe")},
    content_type="multipart/form-data",
)
check("unsupported extension is rejected", r.status_code == 400)
check("rejection names the bad extension", ".exe" in r.get_json().get("error", ""))

r = client.post("/respond", json={"session": "t", "text": ""})
check("empty question is rejected", r.status_code == 400)

r = client.post("/extract", json={"session": "no-doc", "fields": "rent"})
check("extract without a document is rejected", r.status_code == 400)

r = client.get("/document/text?session=no-doc")
check("document text 404s before upload", r.status_code == 404)


print()
if failures:
    print(f"{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
