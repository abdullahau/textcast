## Bloomberg to Audio: Listen to Matt Levine's Money Stuff on Your Commute

This is a lightweight personal project designed to convert [Matt Levine’s Money Stuff](https://www.bloomberg.com/account/newsletters/money-stuff) newsletter from HTML to high-quality audio using a free and efficient Text-To-Speech (TTS) model — [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M). It was born out of frustration with paid TTS services and limited playback functionality on mobile platforms.

When Microsoft Outlook removed "Play My Emails" and Apple's "Speak Screen" made scrubbing between sections difficult, I wanted a better way to **listen to footnotes inline, block quotes clearly marked**, and control my experience while commuting — without breaking the bank.

So I took matters into my own hands.

---

### Workflow

1. **Save HTML** of the Bloomberg newsletter (via browser).
2. Run `BloombergHTML-to-Text.ipynb` to convert it into a structured `.txt` file.
3. Use `Kokoro TTS.ipynb` to turn the `.txt` into a `.wav` audio file.
4. **AirDrop** or sync the audio to your phone — done!

---

### Project Structure

```
.
├── input/           # Raw HTML files of Bloomberg newsletters
├── output/          # Processed .txt files with inlined footnotes & block quote markers
├── example/         # Generated .wav audio files
├── BloombergHTML-to-Text.ipynb   # Parses Bloomberg HTML into clean text
├── Kokoro TTS.ipynb              # Converts text into audio using Kokoro
├── config.py        # Optional: stores Hugging Face access token
├── .env             # Optional: stores access token securely
└── README.md
```

---

### Run this project online

You can run the notebooks directly in your browser using:

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/yourusername/your-repo-name/blob/main/BloombergHTML-to-Text.ipynb)

[![Open in Kaggle](https://kaggle.com/static/images/open-in-kaggle.svg)](https://kaggle.com/kernels/welcome?src=https://github.com/yourusername/your-repo-name/blob/main/BloombergHTML-to-Text.ipynb)

---

### Why This Project?

Matt Levine’s newsletters are rich in nuance — with tons of footnotes and block quotes. Most TTS systems:

* Skip or defer footnotes
* Flatten quote structure
* Cost money!

This tool **preserves structure**, **reads footnotes inline**, and **tags block quotes** clearly — with free, high-quality output using Kokoro's TTS model.

---

### Requirements

You’ll need:

* Python 3.9+
* `beautifulsoup4`, `lxml`, `transformers`, `pydub`, etc.
* GPU runtime via Google Colab or Kaggle

---

### Feedback?

Feel free to fork, adapt, or improve this. PRs welcome!

---