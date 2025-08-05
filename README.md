## Bloomberg to Audio: Listen to Matt Levine's *Money Stuff* on Your Commute

This lightweight personal project converts [Matt Levine’s *Money Stuff*](https://www.bloomberg.com/account/newsletters/money-stuff) newsletter from Bloomberg HTML into high-quality audio using the efficient and free [Kokoro TTS model](https://huggingface.co/hexgrad/Kokoro-82M).

It was born out of frustration: paid TTS services weren’t worth it, Microsoft Outlook removed “Play My Emails” from the iOS, and Apple’s “Speak Screen” made it hard to scrub between sections. I wanted a better way to:

* Hear **footnotes read inline**
* Distinguish **block quotes clearly**
* Listen **offline**, on my commute

So I built one.


### How It Works

1. **Save the HTML** of a Bloomberg newsletter.
2. Run `html-to-text.ipynb` to parse and clean it into a structured `.txt` file.
3. Run `text-to-speech.ipynb` to convert it into a `.wav` file using Kokoro TTS.
4. **Airdrop** the audio to your phone and listen on the go.


### Project Structure

```
.
├── input/                  # Raw HTML files of newsletters
├── output/                 # Clean .txt files with inline footnotes & quotes
├── example/                # Example .wav audio outputs
├── html-to-text.ipynb      # Parses HTML into readable text
├── text-to-speech.ipynb    # Converts text to audio using Kokoro
├── config.py               # Optional: Hugging Face access token (uses .env)
├── .env                    # Optional: stores access token securely
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
- Produces **clear, natural audio for free**


### Requirements

* Python 3.9+
* `kokoro>=0.9.4`
* `espeak-ng`
* `ffmpeg`
* `soundfile`, `beautifulsoup4`, `lxml`, `torch`, `numpy`, etc.
* GPU runtime (Google Colab or Kaggle recommended)


### Feedback & Contributions

Feel free to fork this, adapt it for your needs, or suggest improvements.

Pull requests are welcome!
