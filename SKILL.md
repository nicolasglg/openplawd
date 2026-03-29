---
name: openplawd
description: Automatically poll Plaud recordings, transcribe with Whisper, and generate structured meeting notes. Triggers on "check plaud", "meeting notes", "transcription", or via cron.
---

# OpenPlawd — Plaud Meeting Notes

## Workflow

```
Plaud API → Download → Chunk audio → Whisper (Groq/OpenAI) → Agent → Meeting Notes → Dispatch
```

## Polling

Run the polling script to detect new recordings and transcribe them:

```bash
python3 scripts/plaud-poll.py
```

The script outputs structured JSON:
```json
{
  "new_detected": [...],
  "transcriptions_complete": [...],
  "transcriptions_in_progress": [...]
}
```

## Interactive Flow

When a new transcription is ready:

### Phase 1: Preview

Send a preview message with:
- Date and duration of the recording
- Detected meeting type (internal / client) and probable company
- List of proper names detected with proposed corrections
- "Say 'go' to generate the meeting notes, or correct the names first"

### Phase 2: Validation

- Wait for user response
- If name corrections → apply them to the transcript
- If "go" → proceed to generation

### Phase 3: Generate Meeting Notes

Generate an HTML meeting note using this structure:

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 11pt; line-height: 1.4; }
    h1 { color: #1D1D1F; border-bottom: 2px solid #4A80F0; padding-bottom: 5px; }
    h2 { color: #4A80F0; margin-top: 20px; }
    .tldr { background: #f5f5f5; padding: 10px 15px; border-left: 4px solid #4A80F0; margin: 15px 0; }
    table { border-collapse: collapse; width: 100%; margin-top: 10px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background: #4A80F0; color: white; }
    tr:nth-child(even) { background: #f9f9f9; }
  </style>
</head>
<body>
  <h1>Meeting Notes — [Company/Team] — [Date]</h1>
  <div class="tldr"><strong>TL;DR</strong><br>[3-5 lines summary]</div>
  <!-- Sections as needed -->
  <h2>Action Items</h2>
  <table>...</table>
</body>
</html>
```

### Writing Rules

- Concise bullet points (max 15 words each)
- Active voice ("Launch X", "Send Y") — no passive constructions
- Omit empty sections
- Section emojis are mandatory

### Available Section Emojis

| Emoji | Section |
|-------|---------|
| Target | Context |
| Person | Profile (interviews) |
| Plug | Technical |
| Money | Commercial / Pricing |
| Calendar | Planning |
| Chart | Data / Metrics |
| Lock | Security / Legal |
| Rocket | Deployment |
| Bug | Bugs / Issues |
| Check | Positives |
| Warning | Watch points |
| Question | Open questions |
| Clipboard | Action items |

### Phase 4: Dispatch

After generating the meeting notes:

1. **Send the HTML** to the user (Telegram, email, or other configured channel)
2. **Cleanup Plaud** — rename and trash the recording:
   ```bash
   python3 scripts/plaud-poll.py rename <rec_id> "Notes — Title — DD/MM"
   python3 scripts/plaud-poll.py trash <rec_id>
   ```
3. **Optional integrations** (if configured):
   - CRM note for client meetings
   - Note app (e.g. Notion) for internal meetings
   - Email draft for distribution

## Whisper Corrections

Whisper often misspells proper names, especially non-English ones. Add your custom corrections below. The agent applies these before analyzing the transcript.

| Whisper Output | Correction | Context |
|---------------|------------|---------|
| *Add your own* | *corrections here* | *e.g. company name* |

**Examples:**

| Whisper Output | Correction | Context |
|---------------|------------|---------|
| John son, John Son | Johnson | Common name misspelling |
| Marteen, Mar Teen | Martin | European name |
| Schmitt, Shmit | Schmidt | German name |

## Meeting Type Detection

### Internal Meeting
Triggers: presence of a known team member name.

Configure your team members in the corrections table above.

### Client Meeting
Triggers: mention of a client company, external contact, or commercial context.

### Ambiguous
If unclear, ask the user:
```
I detect [clues]. Is this:
1. Client meeting ([suspected company])
2. Internal meeting
```

## Cron Setup

Add to your OpenClaw cron configuration to poll automatically (e.g. every hour):

```json
{
  "id": "plaud-poll",
  "schedule": "0 * * * *",
  "command": "python3 scripts/plaud-poll.py",
  "agent": "main"
}
```

## Manual Trigger

Say "check plaud" or "meeting notes" to trigger the poll manually.
