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
| **Mail** | two Gmail accounts, 9-category triage, only what matters is spoken aloud |
| **Calendar** | upcoming events, reminders |
| **Music** | full Spotify control |
| **Vision** | screenshots described by a vision model on request |
| **Dictation** | speaks into the cursor, or straight into an Obsidian vault |
| **Second brain** | searches a personal Obsidian vault and answers from it |
| **System** | CPU / RAM / disk / battery monitoring with spoken warnings |

## How it is put together

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
provider so a single outage or rate limit cannot take the assistant down.

**Mail triage as a separate module.** [`mail_triage.py`](mail_triage.py) classifies
incoming mail into nine categories and archives the noise. It lives outside the
main file so the rules can be edited and unit-tested without touching a running
assistant. On a real mailbox it cut 5119 inbox messages down to 400, and only
two categories are ever spoken aloud.

The ordering of its checks is deliberate and documented in the module: security
before money, money before noise (a failed payment notice arrives from a sender
that is otherwise pure marketing), delivery before shops.

**Resilience.** A watchdog restarts crashed components. Every external call is
wrapped so that a dead API degrades one feature instead of killing the process.

## Running it

```bash
pip install -r requirements.txt          # see the imports at the top of lin.py
cp secrets.example.json secrets.json     # then fill in your own keys
python gmail_auth.py                     # Google OAuth, once
python spotify_auth.py                   # Spotify OAuth, once
python lin.py
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
