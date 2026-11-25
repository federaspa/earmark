# Earmark

Click **Start recording** in the menu bar when a Teams call begins and **Stop** when it ends. The
audio is transcribed locally with Whisper, and a summary note is filed into the Obsidian vault by
GitHub Copilot CLI (Claude Code CLI as fallback). **Start voice note** records just your
microphone, for thinking out loud: what you say is added to the notes it belongs in, or becomes
a new note. Audio never leaves the Mac; only transcript text is
sent to Copilot/Claude.

```
menu bar ──Stop──> audiorec (others.wav + me.wav) ──> transcriber ──> inbox/<DD-MM-YYYY HHMM>.md
                                                                          │
                     Teams .vtt / .docx dropped into inbox/ ──────────────┤
                                                                          ▼
                                     filer ──> copilot -p (fallback: claude -p, then Anthropic API)
                                                                          ▼
                         <vault>/<Project>/Meetings/Meeting <Series> DD-MM-YYYY.md + 000 Index.md
                         (voice notes: the existing notes they belong in, or new ones)
```

All dates the system writes (note names, headings, the index, inbox file names) are DD-MM-YYYY.

## Setup

Needs an Apple Silicon Mac (macOS 14.2+ for Teams-only capture), [uv](https://docs.astral.sh/uv/),
Xcode command-line tools, and [GitHub Copilot CLI](https://github.com/github/copilot-cli) and/or
[Claude Code](https://claude.com/claude-code) signed in.

```sh
cp config.example.toml config.toml   # set your vault path and your name
uv sync
make audiorec                        # build bin/AudioRec.app
make install                         # start the menu-bar app and the filer at login
```

## Layout

| Path | What |
|---|---|
| `app/` | Python code: `menubar.py`, `transcriber.py`, `filer.py`, `teams_import.py` |
| `recorder-swift/` | Source of the native `audiorec` capture tool |
| `bin/AudioRec.app` | Built `audiorec` (fixed path so macOS permissions stick) |
| `inbox/` | Transcripts waiting to be filed (drop Teams `.vtt`/`.docx` here too) |
| `processed/` | Transcripts already filed (never deleted automatically) |
| `failed/` | Transcripts that failed after all retries |
| `recordings/` | WAVs of the current/unfinished recording (deleted after transcription) |
| `logs/` | `filer.log`, `menubar.log`, `transcriber.log` |
| `prompts/filer.md` | The prompt for meetings given to Copilot/Claude — edit freely |
| `prompts/voice-note.md` | The prompt for voice notes — edit freely |
| `prompts/local.md` | Optional, not in git: examples from your own vault (folders, series, notes to link), appended to both prompts |
| `config.example.toml` | Settings template: copy to `config.toml` (not in git) — vault path, your name, models |
| `.env` | `ANTHROPIC_API_KEY` (only for the last-resort API fallback) |

## Commands

```sh
make test               # unit tests
make file-now           # file everything in inbox/ now
make audiorec           # (re)build bin/AudioRec.app
make menubar            # run the menu-bar app in the foreground
make reset-test-vault   # recreate test-vault/ as a copy of your vault
uv run python -m app.transcribe_file <audio>   # transcribe any audio file into inbox/
```

## Permissions (one-time)

Audio capture is done by `bin/AudioRec.app` (the `audiorec` tool). macOS grants permissions to
that app — not to Terminal or Python — and because it is signed with a stable identifier
(`io.github.earmark.audiorec`), rebuilding it with `make audiorec` keeps them.

On first launch the menu-bar app asks for both permissions. Click **Allow** on each prompt. If you
missed them or clicked Don't Allow:

1. **Microphone** (your voice): System Settings → Privacy & Security → **Microphone** → turn on
   **AudioRec**.
2. **System Audio Recording** (the Teams call): System Settings → Privacy & Security →
   **Screen & System Audio Recording** → section **System Audio Recording Only** → turn on
   **AudioRec**. (If it isn't listed, click **+** and add `bin/AudioRec.app` from this folder.)

Check the state any time with `bin/audiorec check` (`"granted"` for both is what you want). If
the call audio comes out silent while `system_audio` says `granted`, toggle AudioRec off and on.

The ScreenCaptureKit fallback (used only if process taps fail) needs **Screen & System Audio
Recording** (the full one, not "Only") for AudioRec instead.

## How recording works

- Call audio comes from a Core Audio *process tap* on Teams (`com.microsoft.teams2*` processes;
  if Teams isn't running, all system audio). Your voice comes from the default microphone.
- Two files, `others.wav` and `me.wav` (16 kHz mono), are streamed to `recordings/<timestamp>/`
  and stay aligned on one clock. If Teams' audio processes or your audio devices change mid-call,
  capture restarts automatically and the gap is filled with silence.
- Recording stops by itself after 4 hours, or if the menu-bar app dies. WAV headers are rewritten
  every 5 seconds, so after a crash the audio is still usable: relaunch and click **Recover last
  recording**.
- After **Stop**, the transcript is written to `inbox/` and the WAVs are deleted. If
  transcription fails, the WAVs are kept for **Recover last recording**.
- **Voice notes** record only `me.wav` (no call or system audio), and the transcript is marked
  `source: voice-note`.
- Wearing headphones gives the cleanest result. With speakers, your mic also hears the call; the
  transcriber drops "Me" lines that are an echo of "Others".

## Transcription

`mlx-whisper` with `whisper-large-v3-turbo`, fully local (the model is downloaded once from
Hugging Face to `~/.cache/huggingface`). Each channel is split at pauses and each chunk gets its
own language detection, restricted to `languages` in `config.toml` (Italian and English), so
mixed-language meetings stay in their original language. Silent stretches, repetition loops and
known Whisper hallucinations are dropped.

## Filing

The filer takes each transcript in `inbox/` (recorder `.md`, Teams `.vtt` / `.docx`), one at a
time under a lock, and:

1. Runs Copilot CLI from the vault folder with only file tools (`view`, `glob`, `rg`,
   `apply_patch`, `create`, `edit`), no shell, no URLs, access to the vault and `inbox/` only.
2. Falls back to `claude -p` (tools `Read, Write, Edit, Glob, Grep`, no user hooks or MCP
   servers), then to the Anthropic API if `ANTHROPIC_API_KEY` is set (the code writes the files).
3. Verifies the result in code: exactly one new note in `*/Meetings/` or `Inbox/` (or an existing
   note with the same name filled in, keeping its content), starting with an H1 and a `#tags`
   line, listed in `000 Index.md`; no other changes except the previous note's `Next:` link and
   the index's new line / `Last updated`. Anything else is rolled back from a snapshot and the
   next engine is tried.
   For a voice note the check is different: up to 5 notes created or updated (at most 3 new,
   never in a `Meetings/` folder), each listed in the index. An updated note must keep every
   existing line; the only edit allowed besides adding content is ticking a to-do.
4. Moves the transcript to `processed/` (or `failed/` with a notification).

Everything is logged to `logs/filer.log`. Test switches: `FILER_ENGINES=claude` limits the
engines tried, `FILER_FORCE_FAIL=copilot` makes an engine fail on purpose.

**Privacy:** audio never leaves the Mac. The transcript text, and the vault notes the engine reads
to file it, are sent to GitHub Copilot (or Anthropic for the fallback).
