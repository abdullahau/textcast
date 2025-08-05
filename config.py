import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

ACCESS_TOKEN = os.getenv("HUGGING_FACE_ACCESS_TOKEN", None)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")