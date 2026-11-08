# Tidy

Organizes every file in your Mac's home folder with Claude. You review every change before anything moves, and one click undoes it all.

## Run it

```bash
cd mac-organizer
pip3 install -r requirements.txt
python3 app.py
```

The app opens in its own window. On first scan it asks for your Anthropic API key and stores it in your Keychain.
Give Terminal **Full Disk Access** (System Settings › Privacy & Security) so it can read all your folders.

## How it works

1. **Scan** your home folders. `~/Library`, apps, hidden files and symlinks are never touched.
2. **Lock fragile folders.** Websites, code projects, app libraries, bundles, media projects and folders whose files link to each other move as one piece. Only the top folder can be renamed. Nothing inside changes.
3. **Claude designs the folder structure** from a summary of everything.
4. **Claude files and renames** every other file in batches. Extensions are always kept.
5. **You review**: rename, move, or leave anything in place.
6. **Organize**: a Time Machine snapshot is saved, every move is logged, and locked folders are verified afterwards.
7. **Undo** puts everything back exactly as it was.

Your data lives in `~/.tidy` (plan, move log, cache).

## Files

- `engine.py`: scanning, fragile-folder detection, Claude calls, validation, moves, undo
- `app.py`: local server, window, Keychain
- `ui/index.html`: the interface
