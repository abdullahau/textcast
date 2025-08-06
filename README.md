## Bloomberg to Audio: Listen to Matt Levine's *Money Stuff*

This lightweight, personal project converts [Matt Levine’s *Money Stuff*](https://www.bloomberg.com/account/newsletters/money-stuff) newsletter from Bloomberg's HTML webpage into high-quality audio using the efficient and free [Kokoro-82M model](https://huggingface.co/hexgrad/Kokoro-82M).

This project was born out of frustration: paid TTS services weren’t worth it, Microsoft Outlook removed “Play My Emails” from the iOS, and Apple’s “Speak Screen” made it hard to scrub between sections. I wanted a better way to:

* Hear **footnotes read inline**
* Distinguish **block quotes clearly**
* **Summarize** sections using AI (optional)
* Listen **offline**, on my commute

So I built one.


### How It Works

1. **Save the HTML** of a Bloomberg newsletter.
2. Run `html-to-text.ipynb` to parse and clean it into a structured `.txt` file.
3. Optionally Run `html-to-text-with-summary.ipynb` to generate concise **section-by-section summaries** of the newsletter using Google's `gemini-2.5-flash`
4. Run `text-to-speech.ipynb` to convert it into a `.wav` file using Kokoro TTS.
5. **Airdrop** the audio to your phone and listen on the go.


### Project Structure

```
matt-levine-TTS/
├── input/                               # Raw HTML files of newsletters
├── output/                              # Clean .txt files with inline footnotes & quotes
├── example/                             # Example .wav audio outputs
├── html-to-text.ipynb                   # Parses HTML into readable text
├── html-to-text-with-summary.ipynb      # Parses HTML + adds Gemini summaries
├── text-to-speech.ipynb                 # Converts text to audio using Kokoro
├── config.py                            # Optional: Hugging Face access token and Gemini API key (uses .env)
├── .env                                 # Optional: Stores HF access token and Gemini API key securely
└── README.md
```


### Run This Project Online

Run the notebooks in-browser:

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/abdullahau/matt-levine-TTS/blob/main/text-to-speech.ipynb)

[![Open in Kaggle](https://kaggle.com/static/images/open-in-kaggle.svg)](https://kaggle.com/kernels/welcome?src=https://github.com/abdullahau/matt-levine-TTS/blob/main/text-to-speech.ipynb)

Choose the platform with available GPU compute (Kaggle offers 30hrs/week of 2 NVIDIA T4 GPUs for free!).


### Why This Project?

Matt Levine’s newsletters are intelligent, footnote-heavy, and full of nuance. Most TTS tools:

* Ignore or defer footnotes
* Flatten quoted passages
* Lock you behind paywalls

This tool:

- Reads **footnotes inline**
- Marks **block quotes with context**
- Adds **AI-generated summaries** (optional)
- Produces **clear, natural audio for free**


### Requirements

* Python 3.9+
* `kokoro>=0.9.4`
* `espeak-ng`
* `ffmpeg`
* `soundfile`, `beautifulsoup4`, `lxml`, `torch`, `numpy`, etc.
* **(Optional)** `google-generativeai` — for summaries
* GPU runtime (Google Colab or Kaggle recommended)


### Feedback & Contributions

Feel free to fork this, adapt it for your needs, or suggest improvements.

Pull requests are welcome!
