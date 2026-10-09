# Tutorial narration

The tutorial player reads each caption aloud. Tutorials use clips rendered ahead
of time with an open-source voice model; your own workflows use the browser's
built-in voice. The 🔊 button in the player bar turns the voice off for that
browser.

## How it works

```
workflows/tutorials/*.yaml ──tutorials narrate──▶ speech server ──▶ workflows/narration/<hash>.mp3
                                                                          │
/api/workflows/tutorial/<slug>  (each step gets audio: /narration/<hash>.mp3 if its clip exists)
                                                                          │
player.js: plays the clip as the step starts; in auto mode moves on when it ends
```

- **One clip per line.** The spoken line is a pause step's text, or else the
  step's `say`. That's what the caption shows. A clip's name is a hash of its
  text, so changing a line only re-renders that line. Clips no tutorial uses
  any more are deleted when you render again.
- **Nothing runs a model at request time.** The clips are committed and served
  as static files. Only the person rendering needs the speech server.
- **Fallbacks.** If a line has no clip yet, the browser's voice reads it. If
  the browser can't play sound, the player waits long enough to read the
  caption, the same as before. That happens when a page starts autoplaying
  before anyone has clicked on it.
- In auto mode, a click step that leaves the page waits until its line has been
  said.

## The model: Kokoro-82M

[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) is licensed Apache-2.0
and has 82M parameters. It's fast on a CPU, and the voice is natural for its
size. We run it through [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI),
which offers OpenAI's `/v1/audio/speech` API. `core/narration.py` only speaks
that API, so any server that offers it works with nothing else to change.

Other open models we looked at:

| Model | License | Why not (yet) |
|---|---|---|
| Chatterbox (Resemble AI) | MIT | Natural, and can clone a voice from a short sample. Needs a GPU to render at a usable speed. Worth it if we want a house voice. |
| Piper | GPL-3.0 (piper1-gpl); each voice has its own license | Tiny and fast, but sounds more robotic. Check the license of each voice. |
| XTTS-v2 (Coqui), F5-TTS | Non-commercial weights | **Not usable:** this is a commercial product. |

## Render the clips

```bash
# 1. Start Kokoro (CPU image; a -gpu image exists too)
docker run --rm -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest

# 2. Render new and changed lines
export AGENCY_OS_TTS_URL=http://localhost:8880/v1
python3 agency_os.py tutorials narrate

# 3. Ship them
git add workflows/narration && git commit -m "chore: narrate tutorials"
```

| Variable | Default | |
|---|---|---|
| `AGENCY_OS_TTS_URL` | none | The server's base URL, ending in `/v1` |
| `AGENCY_OS_TTS_MODEL` | `kokoro` | Sent as `model` |
| `AGENCY_OS_TTS_VOICE` | `af_heart` | Kokoro voices include `af_bella`, `am_michael`, `bf_emma`, `bm_george` |
| `AGENCY_OS_TTS_API_KEY` | none | Sent as a bearer token, for hosted servers |

A clip's name depends only on its text. So after changing the voice, run
`tutorials narrate --force` to render every line again.

Write captions to be heard as well as read. Spell out what a symbol means
(`{{org_name}}` is read literally), and keep each line to a sentence or three.
