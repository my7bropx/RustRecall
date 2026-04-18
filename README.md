# recall

Personal terminal knowledge base with fuzzy search. Stores commands, notes, and tool usage examples in a local SQLite database. Fully keyboard-driven TUI built with Python/Textual, with an optional Rust binary for faster startup.

## Install

```bash
chmod +x install.sh
./install.sh
```

That's it. The Python version runs immediately. If you have a recent Rust toolchain (`rustup`), the installer will also build and install the native binary automatically.

## Usage

```bash
recall                                        # launch TUI
recall add -t 'title' -c 'content' -C command --tags 'nmap,recon'
recall search nmap                            # quick search, prints to stdout
recall db-path                                # show database file location
```

## TUI keybindings

| Key         | Action                            |
|-------------|-----------------------------------|
| Type        | Fuzzy search as you type          |
| ↑ / ↓       | Move through results              |
| Enter       | Open full-screen view             |
| Ctrl+N      | Add new entry                     |
| Ctrl+E      | Edit selected entry               |
| Ctrl+X      | Delete selected entry             |
| Ctrl+Y      | Yank content to clipboard         |
| F1          | Show all categories               |
| F2          | Filter: CMD (shell commands)      |
| F3          | Filter: NOTE (theory/explanations)|
| F4          | Filter: TOOL (tool usage)         |
| Ctrl+Q      | Quit                              |

Inside the add/edit form:

| Key         | Action                            |
|-------------|-----------------------------------|
| Tab         | Next field                        |
| Shift+Tab   | Previous field                    |
| ← / →       | Cycle category                    |
| Ctrl+O      | Open $EDITOR for content field    |
| Ctrl+S      | Save entry                        |
| Esc         | Cancel                            |

## Entry categories

| Label | When to use                                    |
|-------|------------------------------------------------|
| CMD   | Shell one-liners, command flags, pipelines     |
| NOTE  | Theory, explanations, how-something-works      |
| TOOL  | Tool-specific usage examples (nmap, ffuf, etc) |

## Import from existing notes

If you have existing notes in a text file, you can bulk-import them with a quick script:

```bash
# Example: import from a file where each entry is "# title\ncontent\n\n"
python3 -c "
import re, subprocess, sys
text = open('my_notes.md').read()
for block in text.split('\n## ')[1:]:
    lines  = block.strip().splitlines()
    title  = lines[0].strip()
    content = '\n'.join(lines[1:]).strip()
    subprocess.run(['recall', 'add', '-t', title, '-c', content, '-C', 'note'])
    print('imported:', title)
"
```

## Database location

```
~/.local/share/recall/recall.db
```

Single SQLite file. Back it up with `cp` or rsync. Portable across machines.

## Clipboard

Yank (Ctrl+Y) uses the first available tool in: `xclip` → `xsel` → `wl-copy`.  
On Kali: `sudo apt install xclip`

## Building the Rust binary manually

```bash
# Requires rustup (not the distro cargo — too old)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"

cd /path/to/recall
cargo build --release
cp target/release/recall ~/.local/bin/recall
```
