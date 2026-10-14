# FocusBot 🤖

A floating robot desktop companion for Windows that helps you focus.

## Features
- **Animated hover drone**: its eyes follow your cursor, it has thruster particles and a glowing antenna, and its mood changes (idle, focus, break, warning, angry, sleepy).
- **Pomodoro timer**: a progress ring around the bot. Focus/break/long-break lengths are configurable.
- **Distraction watcher**: blacklist apps or sites. Each entry is matched against the active window title and the program name.
- **Harshness levels**:
  1. **Chill**: gentle reminder after 20s, repeats every 60s
  2. **Firm**: direct after 8s, repeats every 25s, escalates
  3. **Drill Sergeant**: after 3s it flies to the middle of your screen, shakes, beeps, and yells
  4. **Enforcer**: Drill Sergeant behavior plus it **minimizes** the distracting window
- "Start with Windows" toggle in the right-click menu.
- Daily stats, a tray icon, and a "Blacklist this app" quick action.

## Controls
| Action | Result |
|---|---|
| Double-click | Start focus session / pause / resume |
| Click | Poke it |
| Drag | Move it (position is saved) |
| Right-click | Menu: timer, harshness, settings, stats, quit |

Settings are saved to `%APPDATA%\FocusBot\config.json`.

## Getting the .exe
Every push runs **GitHub Actions → "Build FocusBot.exe"**. Open the latest run and download the `FocusBot-windows` artifact.

Run from source: `pip install -r requirements.txt && python focusbot.py`

## Limits
Browsers only expose the **active tab's** title, so FocusBot catches YouTube when that tab is the one you're looking at, not when it sits in a background tab.
