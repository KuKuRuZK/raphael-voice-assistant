# Рафаель — Ukrainian voice assistant for Windows

A desktop voice assistant that listens in Ukrainian, understands free-form
commands through an LLM, and acts: plays music, reads and triages mail, checks
the calendar, looks at the screen, takes dictation, and writes into an Obsidian
vault. Runs in the background with a watchdog, no console windows.

Personal project, built and used daily.

![orb](orb_preview.png)

## What it does

| | |
|---|---|
| **Voice** | wake word or hotkey, ~140 commands, free-form phrasing routed by an LLM |
| **Speech in** | Whisper → Google → Vosk fallback chain, Vosk works fully offline |
| **Speech out** | neural TTS (edge-tts), Ukrainian voice |
| **Mail** | two Gmail accounts, 10-category triage, only what matters is spoken aloud |
| **Calendar** | upcoming events, reminders |
| **Music** | full Spotify control |
| **Vision** | screenshots described by a vision model on request |
| **Dictation** | speaks into the cursor, or straight into an Obsidian vault |
| **Second brain** | searches a personal Obsidian vault and answers from it |
| **System** | CPU / RAM / disk / battery monitoring with spoken warnings |

## How it is put together

`lin.py` is only the entry point; the assistant lives in the
[`raphael/`](raphael) package, one module per concern: `tts` and `stt` for
speech, `llm` and `prompt` for the models, `assistant` for the main loop and
command routing, `actions` for executing what the model asks, and a module per
integration (`music`, `gmail`, `agenda`, `slack_chat`, `brain`, `pc`, `web`,
`sysmon`...). Settings are read as `cfg.X` at call time from
[`raphael/settings.py`](raphael/settings.py), so `config.json` and voice
commands that change settings are seen everywhere at once. The module map is in
[`raphael/__init__.py`](raphael/__init__.py).

**Provider-agnostic LLM layer.** Groq, Google Gemini and a local Ollama model all
speak the OpenAI protocol, so they sit behind one adapter and switching between
them is a one-line config change:

```python
LLM_PROVIDERS = {
    "groq":   {"base_url": "https://api.groq.com/openai/v1",  ...},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", ...},
    "local":  {"base_url": "http://127.0.0.1:11434/v1", ...},
}
```

Models are addressed as `provider:model`, with a fallback on a *different*
provider so a single outage or rate limit cannot take the assistant down. Any
error on the primary (rate limit, timeout, 5xx, dropped connection) switches to
the fallback, and SDK retries are off, so the switch takes seconds rather than
a minute of silence. Short, unambiguous commands ("пауза", "наступний трек",
"котра година") skip the model entirely.

**Mail triage as a separate module.** [`mail_triage.py`](raphael/mail_triage.py) classifies
incoming mail into ten categories and archives the noise. It lives outside the
main file so the rules can be edited and tested
([`tests/test_mail_triage.py`](tests/test_mail_triage.py)) without touching a
running assistant. On a real mailbox it cut 5119 inbox messages down to 400;
six categories are spoken aloud, the rest are labelled silently. Sender rules
look at the address only, never the display name, so a spoofed
`accounts.google.com <promo@spam.xyz>` is not read out as a security alert.

The ordering of its checks is deliberate and documented in the module: security
before money, money before noise (a failed payment notice arrives from a sender
that is otherwise pure marketing), delivery before shops.

**Voice safety.** The assistant does not record while it is speaking, so it
cannot hear itself or be driven by a mail subject it reads aloud. The wake word
must be a whole word at the start or end of a phrase. Shutdown, restart,
killing a process and clearing all plans always ask for a spoken yes, and a
shutdown waits 30 seconds ("скасуй вимкнення" cancels it).

**Resilience.** `start.bat` restarts the process if it crashes; if it dies on
start five times in a row it stops and points to `crash.log`, where startup
tracebacks go under `pythonw`. Every external call is wrapped so that a dead API
degrades one feature instead of killing the process.

## Running it

```bash
pip install -r requirements.txt          # or setup.bat
cp secrets.example.json secrets.json     # Groq + Gemini keys, Spotify app
python gmail_auth.py                     # Google OAuth, once
python spotify_auth.py                   # Spotify OAuth, once
python lin.py
```

Settings live in `config.json` (models, hotkeys, thresholds). Keys never go
there: that file is tracked, `secrets.json` is not.

Tests run on any OS: the pure logic (mail rules, wake word, reminders, command
matching) directly, and the whole assistant on stubs of the Windows-only
libraries (`tests/stubs.py`): confirmations, model fallback, the microphone
ignoring the assistant's own speech, the mail monitor, Spotify token checks.

```bash
pytest
```

For offline speech recognition, download a Vosk Ukrainian model from
[alphacephei.com/vosk/models](https://alphacephei.com/vosk/models) and unpack it
into `vosk-model-uk/`. It is 128 MB, so it is not in this repository.

`start_hidden.vbs` launches everything windowless through the watchdog.

## Notes

No secrets are committed: keys live in `secrets.json`, OAuth tokens in
`gmail_token*.json` and `.spotify_token`, all git-ignored. Logs and the
assistant's memory stay local too, since they contain transcripts of everything
said out loud.

Interface language is Ukrainian, as is most of the code commentary.
