import os
from dotenv import load_dotenv
load_dotenv()
API_KEY = os.getenv("SARVAM_API_KEY")


import requests
   # temporary — we fix this properly in Step 5

url = "https://api.sarvam.ai/speech-to-text"

headers = {"api-subscription-key": API_KEY}

files = {"file": ("test.wav", open("test.wav", "rb"), "audio/wav")}
data = {"model": "saaras:v3", "language_code": "unknown"}  # "unknown" = auto-detect

response = requests.post(url, headers=headers, files=files, data=data)

print(response.status_code)   # 200 means success
print(response.json())        # the transcript lives in here