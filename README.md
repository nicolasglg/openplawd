# OpenPlawd

[![OpenClaw Skill](https://img.shields.io/badge/OpenClaw-Skill-orange?style=flat-square)](https://openclaw.ai)
[![License](https://img.shields.io/github/license/nicolasglg/openplawd?style=flat-square)](LICENSE)
[![Buy Me A Beer](https://img.shields.io/badge/Buy%20Me%20A%20Beer-support-yellow?style=flat-square&logo=buy-me-a-coffee)](https://buymeacoffee.com/nicolasglg)

**Turn your Plaud recordings into structured meeting notes — automatically.**

Record a meeting with your Plaud device, and OpenPlawd handles the rest: download, transcribe, generate clean HTML meeting notes, and archive the recording.

```
Plaud API → Download → Chunk → Whisper (Groq/OpenAI) → OpenClaw Agent → Meeting Notes → Email/CRM
```

## What you get

| Feature | Description |
|---------|-------------|
| Automatic polling | Detects new recordings every hour (configurable) |
| Smart transcription | Groq Whisper (free) for < 90 min, OpenAI for longer recordings |
| Chunk & resume | Large files are split automatically, resumes on interruption |
| Parallel processing | OpenAI chunks transcribed in parallel for speed |
| Custom corrections | Fix recurring Whisper misspellings (names, companies) |
| Interactive flow | Preview names and meeting type before generating notes |
| HTML meeting notes | Clean, structured output with action items table |
| Auto-cleanup | Rename and archive recordings on Plaud after processing |
| CRM integration | Optional CRM note for client meetings |

## How it works

1. **Poll** — The script checks the Plaud API for new recordings
2. **Download** — New audio files are downloaded locally
3. **Chunk** — Files larger than 24 MB are split with ffmpeg
4. **Transcribe** — Whisper transcribes each chunk (Groq free tier by default)
5. **Preview** — The OpenClaw agent shows you detected names and meeting type
6. **Validate** — You confirm or correct names before generation
7. **Generate** — The agent produces structured HTML meeting notes
8. **Dispatch** — Notes are sent via email, CRM, or messaging
9. **Cleanup** — The recording is renamed and trashed on Plaud

## Prerequisites

- [OpenClaw](https://openclaw.ai) gateway running
- A [Plaud](https://plaud.ai) device with recordings
- [Groq API key](https://console.groq.com) (free tier works)
- [ffmpeg](https://ffmpeg.org) installed on the host
- (Optional) [OpenAI API key](https://platform.openai.com) for recordings > 90 min
- (Optional) [Resend API key](https://resend.com) for email delivery

## Installation

### 1. Clone to your OpenClaw workspace

```bash
cd ~/.openclaw/workspace
git clone https://github.com/nicolasglg/openplawd.git
```

### 2. Install Python dependencies

```bash
pip install requests
```

### 3. Copy the skill

```bash
cp openplawd/SKILL.md ~/.openclaw/workspace/skills/openplawd/SKILL.md
```

### 4. Configure environment variables

```bash
cp openplawd/.env.example openplawd/.env
```

Edit `.env` with your credentials:

```env
PLAUD_TOKEN=bearer eyJ...
GROQ_API_KEY=gsk_...
```

> **How to get your Plaud token:** Log into [web.plaud.ai](https://web.plaud.ai), open Chrome DevTools → Application → Local Storage, and copy the `tokenstr` value (including the `bearer` prefix).

### 5. Set up the cron (optional)

Add to your OpenClaw cron config to poll every hour:

```json
{
  "id": "plaud-poll",
  "schedule": "0 * * * *",
  "command": "python3 ~/.openclaw/workspace/openplawd/scripts/plaud-poll.py",
  "agent": "main"
}
```

### 6. Test it

```bash
cd ~/.openclaw/workspace/openplawd
PLAUD_TOKEN="bearer eyJ..." GROQ_API_KEY="gsk_..." python3 scripts/plaud-poll.py
```

Want to make my day? [![Buy Me A Beer](https://img.shields.io/badge/Buy%20Me%20A%20Beer-support-yellow?style=flat-square&logo=buy-me-a-coffee)](https://buymeacoffee.com/nicolasglg)

## Configuration

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PLAUD_TOKEN` | Yes | Plaud API bearer token |
| `GROQ_API_KEY` | Yes | Groq API key (free tier) |
| `OPENAI_API_KEY` | No | OpenAI key (for recordings > 90 min) |
| `RESEND_API_KEY` | No | Resend key (for email delivery) |
| `EMAIL_FROM` | No | Sender email address |
| `EMAIL_TO` | No | Recipient email address |
| `OPENPLAWD_BASE_DIR` | No | Override base directory (default: repo root) |

### Whisper corrections

Edit the corrections table in `SKILL.md` to fix recurring misspellings. Whisper struggles with proper names, especially non-English ones:

```markdown
| Whisper Output | Correction | Context |
|---------------|------------|---------|
| John son      | Johnson    | Client company name |
| Marteen       | Martin     | Common name misspelling |
```

## Usage

### Automatic (cron)

If configured, OpenPlawd polls every hour and notifies you on Telegram when new recordings are found.

### Manual

Tell your OpenClaw agent:
- "check plaud"
- "meeting notes"
- "transcription"

### CLI

```bash
# Poll for new recordings
python3 scripts/plaud-poll.py

# Rename a recording
python3 scripts/plaud-poll.py rename <rec_id> "Notes — Client Meeting — 22/03"

# Trash a recording
python3 scripts/plaud-poll.py trash <rec_id>
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Plaud API unreachable" | Token expired — get a new one from web.plaud.ai |
| "Groq API error 429" | Rate limited — wait 1 hour or switch to OpenAI |
| Incomplete transcription | Will resume automatically on next run |
| Empty transcript | Recording may be silent or corrupted |
| ffmpeg not found | Install ffmpeg: `apt install ffmpeg` or `brew install ffmpeg` |

## License

MIT — see [LICENSE](LICENSE).
