# Clipboard Docker for Krita

A lightweight Krita docker that gives you a persistent clipboard history panel — copy images, layers, or files and they stay available across your entire session.
<img width="447" height="994" alt="grafik" src="https://github.com/user-attachments/assets/25e13f54-86d9-4979-b876-cf1d66bce57c" />


## Features

- 📋 Captures images, local files, and image URLs copied to the clipboard
- 💾 **Persistent history** — survives Krita restarts, saved to the Krita user directory
- 🖼️ Paste clipboard entries as a new document or directly as a new layer
- 🔍 Thumbnail preview with metadata (size, type, timestamp)

## Storage

History is saved automatically to your Krita user directory:

| Platform | Path |
|----------|------|
| Windows  | `%APPDATA%\krita\clipboarddocker\` |
| Linux    | `~/.local/share/krita/clipboarddocker\` |
| macOS    | `~/Library/Application Support/krita/clipboarddocker\` |

Hover over the status bar at the bottom of the docker to see the exact path on your system.

## Credits

Forked and inspired by [BuliCommander](https://github.com/Grum999/BuliCommander) by [Grum999](https://github.com/Grum999) — this plugin extracts just the clipboard subsystem and wraps it in its own standalone docker.

## License

GPL-3.0-or-later
