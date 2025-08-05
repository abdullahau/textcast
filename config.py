import os
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("HUGGING_FACE_ACCESS_TOKEN", None)