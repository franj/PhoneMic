# PhoneMic User Guide

## 1. What this is

PhoneMic solves the annoyance of dictating on a desktop computer. It connects your phone and your PC over the local network, so the phone becomes your microphone: you speak, the phone's speech recognition (for example the Doubao keyboard) turns it into text, and the text lands at the cursor of whatever app is active on the PC.

Beyond dictation, the phone page also has a control panel you can use as a trackpad or a mouse joystick, fire off keyboard shortcuts with one tap, and send files and photos straight to the PC. Explore it on the phone page yourself.

**Highlights:**

- **Connect by QR code:** the PC shows a QR code, the phone scans it and connects through the browser. No app to install.
- **Live preview:** while you dictate, a translucent panel appears at the bottom of the PC screen showing what you are saying.
- **One-tap insertion:** when the phone sends, the text is pasted into the active input field on the PC. The phone can either send automatically or wait for you; **Settings → Phone settings → Send mode** sets the default for the phone, and it applies as soon as the phone connects.
- **Voice commands:** define your own rules, so that saying "OK" presses Enter, saying "quote" types a pair of quotation marks and parks the cursor in between, or saying "open calculator" launches Calculator. It saves a lot of manual fiddling.
- **Dictionary corrections:** speech recognition makes the same mistake on the same word every time. Keep a "wrong → right" replacement table and it is applied before the text is inserted, so you stop fixing the same word by hand. See [5. Dictionary corrections](#5-dictionary-corrections-optional).
- **Remote control:** the phone page expands into a control panel that works as a trackpad, a mouse joystick, a shortcut pad (Ctrl+Z and friends), and a way to push files and photos to the PC. Tap the plus button at the bottom right while the input box is empty to open it; explore the rest yourself.
- **Encrypted transport:** everything between phone and PC is **end-to-end encrypted**, and the keys exist only at the two ends — a relay in the middle sees ciphertext. In LAN mode the data never leaves your network; Cloudflare tunnel mode adds an extra end-to-end layer on top of HTTPS, which is what you want on untrusted Wi-Fi. Connecting can be authenticated either by **QR capture** or by **manual approval**; see [2.7 Encryption and authentication](#27-encryption-and-authentication).

---

## 2. Using it on the PC

### 2.1 Starting up and choosing an IP

1. **Start the app:** double-click the PhoneMic icon.
2. **Choose an IP address:**
   - If the PC has a single LAN address, PhoneMic picks it and starts on its own.
   - If there are several (wired and wireless at the same time, say), a window asks you to pick one. Choose an address the phone can reach — normally the one on the Wi-Fi network the phone is using.
   - **Note:** phone and PC must be on the same local network (same router or hotspot).
3. **Main window:** once the IP is chosen, the app shows a QR code.

### 2.2 Connecting your phone

1. **Scan the QR code:** use the phone's camera, or the scanner in WeChat or Alipay.
2. **The browser opens:** the phone opens the input page automatically.
3. **Connected:** the phone page says "Connected", and so do the main window and the tray icon.

> If scanning is awkward, you can also type the address shown in the main window into the phone browser. When authentication is set to **manual approval** (the LAN default), scanning and typing the address behave the same: the first connection still needs an "Allow" click on the PC. See [2.7 Encryption and authentication](#27-encryption-and-authentication).

### 2.3 The floating overlay

- **It appears by itself:** when you type on the phone, a translucent panel pops up in the lower middle of the PC screen showing the text as you go.
- **It disappears by itself:** after **3 seconds** without input, or as soon as the phone clears its input box, the panel hides.
- **It stays out of the way:** the overlay never takes focus, so you can keep working in whatever app you were in.

### 2.4 The system tray icon

While PhoneMic is running there is a small icon in the notification area.

- **Status:** a red dot means disconnected, a green dot means connected.
- **Right-click menu:**
  - **Show main window:** brings back the window with the QR code.
  - **Settings:** opens the preferences window.
  - **Commands:** opens the voice command manager (see chapter 4).
  - **Dictionary:** opens the dictionary window, where you maintain the "misheard → intended" table (see chapter 5).
  - **Clipboard / Simulated typing / Simulated typing (live):** switches how text is inserted, without opening Settings (see 2.5).
  - **About:** version information and the project home page (both the version and the commit are selectable, which is what you want to paste into a bug report).
  - **Quit:** closes the app.
- **Left-click:** shows or hides the main window.

---

### 2.5 How text gets into the target app

There are three ways to get text from the phone to the cursor on the PC. The active one is marked with a dot.

You can switch in two places, and it is the same setting — change it in one and the other follows:

- **Main window menu:** **Insertion** → **Clipboard** / **Simulated typing** / **Simulated typing (live)**.
- **Tray right-click menu:** the same three entries.

| Mode | How it works | When to use it |
| --- | --- | --- |
| **Clipboard paste (Ctrl+V)** (default) | Writes the text to the clipboard, sends Ctrl+V, then restores the previous clipboard contents | Fast, and long passages go in in one shot |
| **Simulated typing** | Types the text character by character, never touching the clipboard | Terminals, SSH clients, vim and anything else where Ctrl+V does something else |
| **Simulated typing (live)** | Types while you are still speaking, then applies a final correction at the end | When you want words to appear as if from an IME; works in terminals too |

**When to switch to "Simulated typing":** terminals (cmd, PowerShell, Windows Terminal), SSH clients (PuTTY, Xshell, MobaXterm), Git Bash and vim do not paste on Ctrl+V — they read it as a control character and the text never shows up. Simulated typing does not rely on paste, so it works there, and it leaves your clipboard alone.

The price is that it types character by character, so long passages take longer than a paste, and you should not switch windows during it — otherwise the second half lands in the wrong window.

**Three things to know about "Simulated typing (live)":** the text goes wherever the cursor was **at the moment you started speaking**, so do not switch windows or click elsewhere while recognition is running. If focus changes, PhoneMic stops immediately, shows the rest in the overlay instead, and leaves what was already typed in place. Because it types keys, the target app's undo stack records that string of keystrokes — **Ctrl+Z tends to undo the dictation you just inserted, not your earlier work**. If you want one undo to cover a whole paragraph, use clipboard paste, which counts as a single step. For the same reason, when you wire Ctrl+Z / Ctrl+Y into voice commands, prefer clipboard paste (see 4.8).

> Tip: if the target app runs as administrator, Windows blocks simulated input from a non-elevated app. Run PhoneMic as administrator as well.

---

## 2.6 Internet access (Cloudflare tunnel mode)

LAN mode needs the phone and the PC on the same network. If you want to reach your home PC from outside, switch to **Cloudflare tunnel mode**. It builds an HTTPS connection through Cloudflare's free tunnel — no public IP, no port forwarding, no router configuration.

### Installing cloudflared

> If you installed the **bundled** build, cloudflared is already included. Skip this section.

1. Open the [cloudflared releases](https://github.com/cloudflare/cloudflared/releases) page.
2. On Windows you have two options:
   - **Installer (recommended):** download the `.msi`, double-click it, and PATH is configured for you.
   - **Standalone:** download `cloudflared-windows-amd64.exe`, rename it to `cloudflared.exe`, and put it in a system directory (such as `C:\Windows\`) or in the PhoneMic program folder.
3. Open a command prompt and run `cloudflared --version`. If you see a version number, you are done.

> No Cloudflare account is needed. PhoneMic uses a quick tunnel, and a temporary domain is assigned on every start.

### Switching to Cloudflare mode

1. In the main window menu, click **Network → Cloudflare**.
2. The app starts the tunnel; the menu entries grey out while it switches. Give it a few seconds.
3. When it finishes, the window shows a new QR code.
4. If `cloudflared` is missing or fails to start, the app falls back to LAN mode and tells you why.

### Connecting your phone

1. Scan the QR code shown in the window.
2. The phone completes the key exchange and authentication automatically — nothing to type. This mode always uses QR authentication.
3. Once authenticated, everything works as usual.

### Things to watch out for

- **End-to-end encryption:** the tunnel already gives you HTTPS, and PhoneMic's end-to-end layer sits on top of it, so even Cloudflare's relay cannot read your messages (see [2.7](#27-encryption-and-authentication)).
- **The domain changes:** every time Cloudflare mode starts you get a different domain, so you have to scan again. The app refreshes the QR code for you.
- **Mainland China:** Cloudflare tunnels can be unreliable there. If it will not connect or is very slow, use LAN mode.
- **Corporate networks:** if even a direct LAN connection is blocked, the network is tightly managed. A Cloudflare tunnel relays traffic over the internet and is encrypted (HTTPS plus end-to-end), but check whether your organisation allows outbound tunnels before using it.
- **When the tunnel dies:** Cloudflare reclaims free temporary domains after a period without traffic, and then the phone cannot connect (DNS resolution fails). The status bar shows **Tunnel expired, please restart the service**; click **Network → Restart service** to build a new tunnel and get a new domain, then scan again.
- **The address is copyable:** you can select and copy the address below the QR code with the mouse, which is handy for sending it to someone.

## 2.7 Encryption and authentication

Messages between phone and PC are **end-to-end encrypted for the whole session**, and the keys are generated and used only at the two ends — even when traffic passes through a router or Cloudflare, the relay only sees ciphertext. There is **no switch for this; it is always on**.

> **History:** before 0.6.1, LAN mode allowed unencrypted (plaintext) transport. Since 0.6.1 every mode is encrypted.

The phone must authenticate once before it can connect. There are two methods, and **both work by scanning the QR code**:

| Method | How you connect |
| --- | --- |
| **Manual approval** (LAN default) | Scan the QR code, or type the address shown in the main window into the phone browser; the first time, check the 4-digit code on the PC and click "Allow" |
| **QR authentication** (always used by Cloudflare mode) | Scan the QR code; the PC public key travels out of band inside the QR code, so no approval is needed |

The difference is the trust anchor. QR authentication carries the PC's public key out of band in the QR code, so scanning completes authentication. Manual approval has no such anchor, so you confirm on the PC that the 4-digit code matches. Switch with **Manual approval** or **QR authentication** in the **Network** menu.

> Cloudflare tunnel mode **always uses QR authentication**: the endpoint is reachable from the internet, and a first connection over manual approval has no trust anchor, so someone else could race you to the approval prompt. It is therefore not offered.

### How manual approval works

1. Scan the QR code, or open the address shown in the main window (something like `http://192.168.1.100:12000`).
2. The phone shows a **4-digit code**, and the PC shows an approval request with the same code and the source IP.
3. If the two match, click "Allow" on the PC; if not, click "Deny".

After pairing, the phone remembers this PC and reconnects without approval. **Restarting the app, restarting the service or switching modes gives the PC a new identity**, so you pair once more — the phone starts it automatically and you click "Allow" again. An approval request left alone for 30 seconds counts as denied; just reconnect.

### Re-scanning after switching authentication

The address changes with the method. The PC tells any connected phone to scan again; scan the new QR code (under manual approval, open the new address shown in the window).

### How it works

Keys are generated on the PC. The public key travels out of band inside the QR code and never crosses the network (under manual approval it is delivered after approval, in a sealed message only that phone can open). The phone seals its own public key with crypto_box_seal to finish the handshake, and from then on all traffic in both directions is encrypted. Every connection also exchanges a one-time nonce, so replaying a recorded old message fails.

Once connected, the status bar shows the authentication method and the negotiated algorithm — the two ends pick the algorithm themselves, there is nothing for you to choose.

The connection address carries a random path, which is a lock on the door: a scanner hitting the root address gets a 404 and cannot enumerate the real entry point. (Manual approval uses the bare address without that path; the approval prompt is the gate.)

---

## 3. Using it on the phone

### 3.1 What the page looks like

Open the PhoneMic page in the phone browser and you get a plain, chat-like screen:

- **Top:** connection status, such as "Connected" or "Reconnecting…".
- **Middle:** the text you have sent to the PC, one bubble per message (the last 50 are kept).
- **Bottom:** an input box, with "Clear" on the left and "Send / Auto" on the right.

> The first time you connect with **manual approval**, a code prompt covers the middle of the page. Check the digits on the PC, click "Allow", and the normal screen appears.

### 3.2 Typing and preview

1. **Autofocus:** the input box takes focus as soon as the page loads, and the phone keyboard pops up.
2. **Live preview:** as you type, the overlay on the PC shows the same text.
3. **Auto-grow:** the input box grows to fit the text.

### 3.3 Auto-send mode (default)

Auto mode is the default, and it is built for dictation:

- **How it knows you stopped:** while your keyboard (Doubao, say) is still recognising, the text is underlined. When recognition finishes and the underline disappears, PhoneMic sends the text to the PC.
- **After sending:** the input box clears and keeps focus, so you can start the next sentence without tapping anything.
- **How to tell:** the button on the right reads **"Auto"** and is orange.

### 3.4 Manual send mode

If you would rather decide when to send (when typing, for instance), switch to manual:

- **How to switch:** while the input box is empty, a lightning bolt (⚡) appears inside it on the right; tap it to toggle.
- **What manual looks like:**
  - The button on the right reads **"Send"** and is green.
  - Recognised speech is not sent automatically; tap "Send" or press Enter.
- **You get feedback:** the phone may buzz briefly (if it supports it), and the button text and colour change at once.

### 3.5 Sending text (manual mode)

- **Tap "Send":** sends what is in the input box to the PC.
- **Press Enter:** the keyboard's Enter key does the same.
- **What happens:** the text is pasted at the PC cursor immediately.
- **History and clearing:** the message appears in the transcript, and the input box clears and keeps focus.

### 3.6 Clearing the input

- **Tap "Clear":** the button on the left of the input box empties it, and the overlay on the PC disappears with it.

### 3.7 Resending an earlier message

1. **Long-press a bubble:** in the transcript, long-press any message you sent before.
2. **It fills the input box:** the text is appended to what is already there, so you can edit before sending.
3. **Short-press to resend:** a short tap asks whether to resend; confirm and it goes straight to the PC.

### 3.8 Control panel (remote control)

While the input box is empty, the "Send" button at the bottom right becomes a plus (**+**). Tap it to open the control panel. Four tabs sit at the top: **Trackpad, Joystick, Keyboard, Files**. Tap to switch; the default is Trackpad.

- **Trackpad:** like a laptop trackpad — point where you want the cursor, tap to click.
- **Joystick:** push inside a circle to move the cursor, with left/right/middle click, double-click and scroll wheel.
- **Keyboard:** one-tap shortcuts such as undo (Ctrl+Z) and redo (Ctrl+Y).
- **Files:** pick a file on the phone and it is saved to the **Downloads → PhoneMic** folder on the PC; pick an image and it goes straight into the PC clipboard, ready to paste.

> The panel can be collapsed at any time, and it does not interfere with dictation.

---

## 4. Voice commands (optional)

You can define **voice commands**: when text from the phone matches a rule you wrote, PhoneMic stops treating it as plain text and instead **simulates keystrokes**, **types text**, or **runs an external program** — open Calculator, run a script, delete a batch of files, whatever.

### 4.1 Opening the command manager

Two ways:

- **Main window menu:** **Program → Commands…**
- **Tray icon:** right-click → **Commands…**

The command manager is a separate dialog where you add, edit, delete, enable and disable commands, and reorder them.

### 4.2 Adding a command

Click **Add** and fill in:

| Field | Meaning | Example |
|------|------|------|
| Name | Whatever makes the command obvious to you | "Clear the input box" |
| Match type | `Exact` / `Prefix` / `Regex` | Exact |
| Match pattern | The text that triggers it | `clear` |
| Action type | `Simulate keystrokes` or `Run program` | Simulate keystrokes |
| Action argument | A mixed sequence of keys and text, or a command line | `ctrl+a, delete` |

Saved commands take effect immediately; no restart.

### 4.3 Matching rules

- **Exact:** the incoming text must be **identical** to the pattern.
  - Example: pattern `ok` → "ok" fires, "okay" does not.
- **Prefix:** the incoming text only has to **start with** the pattern.
  - Example: pattern `note ` → "note buy milk" fires, and `buy milk` is passed in as `{content}`.
- **Regex:** search the whole sentence with a regular expression (case-insensitive). In the action argument you can refer to `{0}` (the whole match), `{1}` (the first group), `{2}` and so on.
  - Example: pattern `^note[:：,，\s]*(.*)`, action argument `cmd /c echo {1} >> d:/todo.txt` → saying "note: buy milk" appends "buy milk" to d:/todo.txt.

### 4.4 Action types

#### (1) Simulate keystrokes

The action argument is an **input sequence** made of key segments and text segments, separated by commas and executed in order.

**Two segment types:**

| Segment | Syntax | Meaning |
|--------|------|------|
| Key | Unquoted | A key combination, joined with `+` |
| Text | Wrapped in double or single quotes | Types the text as-is, placeholders allowed |

**Key segment examples:**
- Single keys: `enter`, `tab`, `space`, `backspace`, `delete`, `up`, `down`
- Combinations: `ctrl+c`, `shift+alt+a`, `ctrl+shift+esc`
- Sequences (select all, then delete): `ctrl+a, delete`

**Text segment examples:**
- Type text and press Enter: `"hello", enter`
- Type a pair of quotes and put the cursor between them: `"\"\"", left`
- Type the current time: `"{time}"`
- Type today's date: `"{date}"`

> **Escaping:** to type a literal quote inside a text segment, escape it with a backslash — `\"` inside double quotes, `\'` inside single quotes. So `"\"\""` types two double quotes, `""`. Commas inside quotes are not separators.

**Handy examples:**

| What you want | Match pattern | Action argument (keystrokes) |
|------------|----------|----------------------|
| Say "ok" and press Enter | `ok` | `enter` |
| Say "clear" to select all and delete | `clear` | `ctrl+a, delete` |
| Say "really ok" to press Ctrl+Enter | `really ok` | `ctrl+enter` |
| Type a pair of quotes, cursor in the middle | `quotes` | `"\"\"", left` |
| Type the current time | `the time` | `"{time}"` |
| Type today's date | `today` | `"{date}"` |

> Note: key names are lowercase English, joined with `+`, and segments are separated by **commas** (spaces around them are fine).

> **Which insertion mode does a text segment use?** It follows your current **insertion mode** (see 2.5) rather than always pasting. Under Clipboard it writes the clipboard and pastes; under Simulated typing it types character by character; under Live it types as it goes. So to have a command type boilerplate into a terminal or SSH client, where Ctrl+V does nothing, switch insertion to **Simulated typing**. Key segments are unaffected — they always simulate keys directly.

#### (2) Run a program

Put a command line in the action argument:

- `calc` → opens Calculator
- `notepad.exe D:\log.txt` → opens a file in Notepad
- `python D:\my_script.py {content}` → hands dynamic content to a Python script

**On safety:** the command line is **split safely** and executed without going through the system shell, which blocks the usual injection tricks. But if you write a template such as `python -c "import os; os.system('{content}')"`, then `{content}` can still end up executing as code — so make sure what the phone sends is trustworthy (and dictating code is rare anyway).

**More examples:**

| What you want | Match type | Match pattern | Action argument (run program) |
|------------|----------|----------|----------------------|
| Append a to-do item to a text file | Prefix | `todo ` | `cmd /c echo {content} >> D:\note\todo.txt` |
| Run a fixed AutoHotkey automation | Exact | `collect today's stock data` | `"C:\Program Files\AutoHotkey\AutoHotkey.exe" D:\my_scripts\collect_stocks.ahk` |
| Call your own assistant script with an argument | Prefix | `assistant ` | `python D:\my_agent.py --message "{content}"` |

> Notes: in the first example, saying "todo, buy milk" appends `, buy milk` to `D:\note\todo.txt`. The second is an exact match, so the phrase alone runs the AutoHotkey script you wrote (opening pages, scraping data, whatever) with nothing else passed in. The third wakes your own agent script, which can do more elaborate things: check the weather, take a note, control a device.

### 4.5 Placeholders (dynamic content)

These placeholders are substituted in the action argument. They work both in **key** action text segments and in **run program** command lines.

| Placeholder | Meaning | Example |
|--------|------|------|
| `{time}` | Current time (hour:minute:second) | `17:30:25` |
| `{date}` | Today's date (year-month-day) | `2026-08-08` |
| `{content}` | With prefix matching, everything after the pattern | "note buy milk" → `buy milk` |
| `{prefix}` | The pattern itself | `note ` |
| `{all_text}` | The full text sent by the phone | `note buy milk` |
| `{0}` / `{1}` … | Regex capture groups (`{0}` is the whole match, `{1}` onward the parenthesised groups) | "^I said: (.*)", `{1}` is everything after "I said: " |

**Placeholders in text segments:**
- `"{date} {time}"` → types `2026-08-08 17:30:25`
- `"【{content}】"` → after a prefix match, types `【buy milk】`

**Placeholders in run program:**
- `python my_agent.py --msg "{content}"` → hands the matched content to the script
- `echo "{date} {time}: {content}" >> log.txt` → writes a timestamped log line

### 4.6 Setting the working directory ({cwd:...})

When running a program, the working directory defaults to `%LOCALAPPDATA%\PhoneMic\exec_workdir` (for example `C:\Users\your-name\AppData\Local\PhoneMic\exec_workdir`). To use another one, put `{cwd:"absolute path"}` at the **very front** of the command line. The marker is stripped before the program runs.

**Syntax rules:**
- Keep it compact: **no spaces inside the braces, and none around the colon**.
- Quote the path (so spaces in it are safe).
- Forward slashes `/` or doubled backslashes `\\` both work, and `~` expands to your home directory.

**Examples:**
```
{cwd:"D:\\my_work"} python script.py
{cwd:"~/Desktop"} echo hello > out.txt
```
The first runs `python script.py` in `D:\my_work`; the second runs in the Desktop folder.

> Tip: if you do not need a custom directory, just write the command. The default directory is created for you, and it is a convenient place to keep scripts and data.

### 4.7 Managing the command list

The manager shows a table where you can:

- **Enable/disable:** untick the "Enabled" checkbox to switch a command off temporarily.
- **Priority:** commands are matched **top to bottom** and the first hit wins. Use **Move up** / **Move down** to reorder.
- **Edit/delete:** **double-click any row** to edit; click the ✖ in the action column to delete.
- **Add:** the **Add** button at the bottom of the window.

Every change is saved immediately.

**For advanced users:** if you are comfortable with JSON you can edit the config file directly:
```
C:\Users\your-name\AppData\Local\PhoneMic\config\commands.json
```
Back it up first, and keep the JSON valid (no missing commas, quotes in pairs) or the app cannot read it.

### 4.8 Caveats

- Running an external program may flash a window briefly, depending on the program.
- If a command does not fire, check:
  - whether it is **enabled**;
  - whether the match type and pattern are right (watch out for Chinese punctuation);
  - whether ordering is the problem — a broad prefix command can shadow an exact one above it.
- Segments in an input sequence are separated by 0.05 s so each one is picked up. If a key segment fails, the rest are skipped.
- **When a command fires, text already inserted during recognition is removed first** (in live mode). So a "press Enter" command does not leave the word "enter" behind in your document.
- **Pair undo actions (Ctrl+Z / Ctrl+Y) with the Clipboard insertion mode.** In live mode the text is typed key by key, so those keystrokes sit in the target app's undo stack, and an undo command tends to undo the dictation itself. Clipboard paste has no such problem: one paste is one step in the undo stack.

---

## 5. Dictionary corrections (optional)

Speech recognition makes consistent mistakes: the same word is misheard the same way every time. The dictionary lets you keep a "wrong → right" replacement table that is applied before text is inserted, so you stop fixing it by hand.

### 5.1 Opening the dictionary manager

Two ways:

- Main window menu bar: **Program → Dictionary**
- Right-click the tray icon and choose **Dictionary**

### 5.2 Adding an entry

Click **Add** and fill in four things:

| Field | Meaning |
|--------|------|
| Name | A label for your own benefit, e.g. "JSON misheard" |
| Match type | See 5.3 |
| Find | The text speech recognition **got wrong** |
| Replace with | The text you **want**. Leave it empty to delete the match |

### 5.3 Choosing a match type

| Match type | When to use it |
|----------|------------|
| Literal replacement (default) | A fixed misspelling, fixed every time it appears. The right choice in almost every case |
| Whole-sentence only | Replaces only when the entire sentence equals the find text, so the body text is never touched |
| Regex | For bulk work, e.g. deleting filler words |

> **None of the three is case-sensitive.** If the phone gives you `console` this time and `Console` or `CONSOLE` the next, all of them match, and the text is inserted exactly as you wrote it in Replace with. Case comes from the phone's speech engine, so making it part of the match would only cause misses.

**Examples:**

| What you want | Match type | Find | Replace with |
|------------|----------|----------|--------|
| "json" is heard as "jason" | Literal | `jason` | `JSON` |
| "COMSOL" is heard as "console", and Console/CONSOLE should be fixed too | Literal | `console` | `COMSOL` |
| Strip filler words like "um" and "uh" | Regex | `(um|uh)+` | (empty) |
| Say "ok" to fire a command, but keep "ok" in body text | Whole-sentence only | `ok` | `ok` |

### 5.4 A few things to know

- **Entries apply in list order**, top to bottom, and the output of one feeds the next. Reorder with the ▲▼ on the right of each row.
- **An empty Replace with** deletes the matched text; that is how you remove verbal tics.
- **The live preview shows raw recognition output** — correction happens at the moment of insertion. You may briefly see the wrong word in the overlay; what lands is already corrected.
- **The dictionary runs before voice commands.** If "ok" was misheard, the dictionary repairs it and the command from chapter 4 still fires.
- **Changes apply immediately**; no restart.
- Untick the checkbox on the left of a row to disable an entry without deleting it.

### 5.5 Editing the config file directly (advanced)

You can also edit `C:\Users\your-name\AppData\Local\PhoneMic\config\dictionary.json` to manage entries in bulk. Back it up first and keep the JSON valid. Field meanings are in [docs/text-dictionary-design.md](docs/text-dictionary-design.md).

---

## 6. Troubleshooting

- **Cannot connect / blank page:**
  - Check that phone and PC are on the same local network.
  - Make sure PhoneMic is running on the PC.
  - Check whether the PC firewall blocks PhoneMic's port (the main window shows it, usually 12000).
  - Type the full address from the main window into the phone browser, e.g. `http://192.168.1.100:12000`.
  - Under **manual approval**, check the PC for a pending approval request (it is denied after 30 seconds). If the PC was restarted or the mode changed, pair again.
- **Text does not land:**
  - Make sure an app that accepts text (Notepad, a chat window, Word) is active on the PC.
  - If it fails in a terminal, SSH client or vim, switch the insertion mode to **Simulated typing** (see 2.5); those apps do not paste on Ctrl+V. You do not need the Settings window — the **Insertion** menu and the tray menu both switch it.
  - If the target app runs as administrator, PhoneMic must too, or Windows blocks the simulated input.
  - Check whether security software is blocking PhoneMic's simulated keys.
- **The overlay does not show:**
  - Confirm the phone is connected and that you are typing.
  - Check the graphics driver, or try restarting the PC.
- **The app crashes:**
  - Try running PhoneMic as administrator.
  - If it happens repeatedly, the environment is probably at fault; contact the developer.
- **Cloudflare mode will not connect:**
  - Check that `cloudflared` is installed and that `cloudflared --version` runs.
  - If the app says it fell back to LAN mode, the tunnel failed to start; check that the network can reach Cloudflare.
  - If the phone reports a DNS failure, the temporary domain was reclaimed. Click **Network → Restart service** for a new one, then scan again.
  - Mainland China can be unreliable; prefer LAN mode.

---

Hope PhoneMic makes your day a little smoother.
