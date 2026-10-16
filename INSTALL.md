# Installing MeetRec, step by step

This is the long version of the Quickstart in [README.md](README.md). It assumes
no prior experience with the terminal, Python or Xcode. Every step says what you
are doing, the exact command to run, what you should see, and what to do when it
goes wrong.

Follow the steps in order. Later steps depend on earlier ones.

**What you need.** A Mac with Apple Silicon (M1 or later), macOS 13 or later,
and a free Hugging Face account for speaker separation. On an Intel Mac you can
still transcribe, but not tell remote speakers apart; follow the steps below and
then read [Running without speaker separation](#running-without-speaker-separation).

**Time and disk space.** About 20 minutes of your attention, plus downloads:
roughly 1 GB for the Python environment, 1.5 GB for the transcription model
(downloaded the first time you transcribe) and 32 MB for the speaker-separation
models. Budget 4 GB of free disk space, plus several GB for Apple's Command Line
Tools if you do not have them yet.

## How to run the commands in this guide

Open **Terminal**: press `Cmd + Space`, type `Terminal`, press Enter. A window
with a text prompt appears.

To run a command, copy the whole line, paste it into that window and press
Enter. The command has finished when the prompt comes back. Some commands print
nothing when they succeed — that is normal.

Keep the same Terminal window open for the whole guide.

---

## Step 1 — Check your macOS version

MeetRec needs macOS 13 (Ventura) or later, because it records system audio with
ScreenCaptureKit.

```bash
sw_vers -productVersion
```

You should see a version number such as `14.5` or `15.2`. If the first number is
13 or higher, continue.

**If it is lower than 13:** MeetRec cannot run on this Mac. Update macOS from
 → System Settings → General → Software Update, then come back.

Check the processor too:

```bash
uname -m
```

`arm64` means Apple Silicon. `x86_64` means an Intel Mac: speaker separation is
not available there, so skip Step 6 and read
[Running without speaker separation](#running-without-speaker-separation) after
Step 5.

## Step 2 — Install the Xcode Command Line Tools

MeetRec is written partly in Swift and is compiled on your machine. The compiler
(`swiftc`) and `git` both come from Apple's Command Line Tools. Until they are
installed, `git` and `python3` on your Mac are empty stand-ins that only offer to
install them, so this step has to come first.

```bash
xcode-select --install
```

A macOS dialog appears. Click **Install**, accept the licence, and wait — it
downloads several GB and can take a while. The command returns immediately, but
the install keeps going in the background: wait until the dialog says it is
done. When it finishes, check:

```bash
swiftc --version
```

It should print a version.

**If the command says "command line tools are already installed"**, that is an
error message but harmless: you already have them. Continue.

**If you already have the full Xcode app** installed, the tools are included,
but `swiftc` can still fail if macOS is pointed at the wrong location, or if you
have not accepted Xcode's licence. Fix both (each asks for your Mac password;
nothing is echoed as you type):

```bash
sudo xcode-select --switch /Applications/Xcode.app
```

```bash
sudo xcodebuild -license accept
```

## Step 3 — Install Python 3.12

MeetRec's transcription runs in Python 3.10 to 3.13; 3.12 is the one to pick.
Check whether you already have it:

```bash
python3.12 --version
```

You should see `Python 3.12.` followed by a number. If so, do the `arm64` check
below, then go to Step 4.

**Do not use the `python3` that came with the Command Line Tools.** It is version
3.9, which is too old: the speaker-separation package refuses to install on it.

If `python3.12` is `command not found`, install it in one of two ways:

- With Homebrew, if you have it (`brew --version` prints a version):

  ```bash
  brew install python@3.12
  ```

- Or, without Homebrew, download Python 3.12.10 from
  <https://www.python.org/downloads/release/python-31210/>, open the `.pkg` and
  click through. python.org's newer 3.12.x releases are security-only and ship
  no macOS installer; 3.12.10 is the last 3.12 release that has one.

Either way you end up with a `python3.12` command. If the check above still fails, close Terminal, open a new window and try again.

On Apple Silicon, check that this Python is the right kind of build:

```bash
python3.12 -c "import platform; print(platform.machine())"
```

It must print `arm64`. If it prints `x86_64`, that Python is an Intel build
(for example Homebrew under `/usr/local`, carried over from an Intel Mac): use
the python.org installer instead.

(Python 3.14 works only on Apple Silicon with macOS 14 or later; not on macOS 13
or Intel. Stay on 3.12 unless you have a reason not to.)

## Step 4 — Download the project

This copies the project into a folder named `meetrec` inside your home
directory. Wherever you cloned MeetRec is "the project folder"; this guide
assumes `~/meetrec`.

```bash
cd ~
```

```bash
git clone https://github.com/frasal-test/meetrec.git
```

```bash
cd meetrec
```

The first command moves to your home folder, the second downloads the project,
the third enters it. From now on every command in this guide assumes you are
inside that folder. If you close Terminal and come back later, run `cd ~/meetrec`
first.

Check that you are in the right place:

```bash
ls meetrec.sh
```

It should print `meetrec.sh`. If it prints `No such file or directory`, you are
in the wrong folder — run `cd ~/meetrec` and try again.

## Step 5 — Create the Python environment

Rather than installing packages system wide, MeetRec uses a *virtual
environment*: a self-contained folder named `.venv` inside the project. Nothing
here touches the rest of your Mac.

```bash
python3.12 -m venv .venv
```

This prints nothing when it works.

```bash
.venv/bin/pip install -U pip
```

This updates the installer itself and ends with `Successfully installed pip-…`
(or `Requirement already satisfied` if it was current).

Intel Mac (`uname -m` printed `x86_64`)? Run
`.venv/bin/pip install -r requirements.txt` instead and skip the next command.

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

This one is slow — several minutes and about 1 GB, most of it PyTorch. You
will see a long stream of `Collecting…` and `Downloading…` lines. It is done
when the prompt returns and the last lines read `Successfully installed …`.

**Why `requirements-diarization.txt` and not `requirements.txt`?** Separating
remote speakers is switched on by default in MeetRec's configuration, and that
feature needs the extra packages. The smaller `requirements.txt` still produces
transcripts — they simply arrive without speaker separation, carrying a warning
that explains why. On an Intel Mac, use `requirements.txt`: the speaker
separation packages cannot be installed there. Then see
[Running without speaker separation](#running-without-speaker-separation) and
turn the feature off properly.

**If pip says "Could not find a version that satisfies the requirement
pyannote.audio>=4"**, the environment was created with the wrong Python (3.9).
Recreate it with the right one:

```bash
rm -rf .venv && python3.12 -m venv .venv
```

Then run the two `pip install` commands above again.

**If `python3.12` is not found**, redo Step 3.

## Step 6 — Get a Hugging Face token

Speaker separation uses models published by pyannote on Hugging Face. They are
free, but the authors require you to have an account and to accept their
conditions before downloading. You need a token to prove that.

Skip this step only if you decided to run without speaker separation.

1. Create a free account at <https://huggingface.co/join>, or sign in to the one
   you have.
2. While signed in, open all three pages below and accept the conditions on each.
   Each shows a short form and a button such as *Agree and access repository*.
   You must be signed in, or you will only see a description and no form.
   - <https://huggingface.co/pyannote/segmentation-3.0>
   - <https://huggingface.co/pyannote/speaker-diarization-3.1>
   - <https://huggingface.co/pyannote/speaker-diarization-community-1>
3. Go to <https://huggingface.co/settings/tokens> and click **New token**. Give
   it any name, choose the **Read** type, and create it. (If you prefer a
   fine-grained token, tick "Read access to contents of all public gated repos
   you can access"; without it the token fails.)
4. Copy the token. It starts with `hf_`. Hugging Face shows it once — copy it
   now.

Now save it into a file named `.env` in the project folder. Keep the command
exactly as shown and replace only `hf_your_token_here` with the token you
copied:

```bash
echo 'HF_TOKEN=hf_your_token_here' > .env
```

That file stays on your Mac and is never committed to git.

Check it was written:

```bash
cat .env
```

It should print one line, `HF_TOKEN=hf_…`, with your real token.

**The token must belong to the same account** that accepted the conditions in
point 2. This is the most common cause of a failure later on.

## Step 7 — Create a signing certificate

Do this once. Skipping it does not break MeetRec, but it makes it annoying:
macOS will ask for microphone and screen-recording permission again after every
rebuild, and the old permission entries pile up in System Settings, switched on
but no longer doing anything.

The reason is that macOS ties a permission grant to the exact identity of the
program. Without a certificate, the identity is a fingerprint of the compiled
file itself, which changes every time it is recompiled. With a certificate, the
identity stays stable across rebuilds.

The certificate is created in the Keychain Access app:

1. Open **Keychain Access** (`Cmd + Space`, type `Keychain Access`).
2. In the menu bar: **Keychain Access → Certificate Assistant → Create a
   Certificate…**
3. **Name:** type `MeetRec Dev` exactly — MeetRec looks for this name.
4. **Identity Type:** choose **Self Signed Root**.
5. **Certificate Type:** choose **Code Signing**.
6. Tick **Let me override defaults** and click **Continue**. Set **Validity
   (days)** to `3650`, then keep clicking **Continue** until you can click
   **Create**, then **Done**.

The validity step matters: the default is 365 days. After that the certificate
expires, the build quietly falls back to ad-hoc signing, and macOS asks for
every permission again. If you skip the override, put a reminder in your
calendar for next year.

Back in Terminal, confirm macOS considers it usable:

```bash
security find-identity -v -p codesigning
```

The output should include a line containing `"MeetRec Dev"`.

**If it does not appear:** find the certificate in Keychain Access, double-click
it, expand the **Trust** section, and set **Code Signing** to *Always Trust*.
Close the window (macOS asks for your password) and run the command again.

If you prefer a different certificate name, set `MEETREC_SIGN_IDENTITY` to that
name in your shell before building.

## Step 8 — Build and install

This compiles both programs, signs them with the certificate from Step 7,
installs a LaunchAgent so MeetRec starts automatically when you log in, and
launches it now.

```bash
./meetrec.sh install
```

**A dialog may appear the first time:** *codesign wants to sign using key
"MeetRec Dev" in your keychain*. Terminal looks frozen until you answer. Type
your Mac login password and click **Always Allow**. It can appear a second time,
for the second program; answer it the same way.

Expect output along these lines:

```text
Built: /Users/you/meetrec/.recorder
/Users/you/meetrec/.recorder: replacing existing signature
Signed: /Users/you/meetrec/.recorder (com.frasal.meetrec.recorder) with 'MeetRec Dev'
Built: /Users/you/meetrec/MeetRec.app
/Users/you/meetrec/MeetRec.app: replacing existing signature
Signed: /Users/you/meetrec/MeetRec.app (com.frasal.meetrec.menubar) with 'MeetRec Dev'
MeetRec is installed and running in the menu bar.
```

The `replacing existing signature` lines come from Apple Silicon and are
harmless. When you run the command again without changing anything, `Built:`
becomes `Up to date:`.

A ✒︎ icon appears in the macOS menu bar, at the top right of the screen.

**If you see `Signed ad-hoc`** with a warning, the certificate from
Step 7 was not found (or has expired). MeetRec still works; go back to Step 7 if
you would rather not re-grant permissions after each rebuild.

**If you see `permission denied`**, run `chmod +x meetrec.sh meet.sh` and try
again.

**If no icon appears**, see [Troubleshooting](#troubleshooting).

## Step 9 — Grant the macOS permissions

macOS does not let any app hear you or capture other apps' sound without an
explicit grant, and it only asks the first time the app actually tries.

Click the ✒︎ icon and choose **Start recording — Italiano** (or whichever
language you want).

1. macOS asks for the **Microphone**. Click **Allow**.
2. macOS also shows its own screen-recording dialog; click **Open System
   Settings**. A moment later an alert says **Recording failed**. **This is
   expected on the first try.** macOS only adds MeetRec to the Screen Recording
   list after refusing it once. The refused attempt leaves a session folder with
   no audio in `recordings/` (it holds `meta.json` and `recorder.log`); you can
   delete it.
3. Open  → System Settings → Privacy & Security → **Screen & System Audio
   Recording** (on macOS 13 and 14 it is called **Screen Recording**) and switch
   **MeetRec** on.
4. Restart MeetRec so it picks the permission up: ✒︎ → **Quit MeetRec**, then
   run:

   ```bash
   ./meetrec.sh install
   ```

   This time it prints `Up to date:` lines instead of `Built:`.

Now click ✒︎ → **Start recording** again, say a few words, and click ✒︎ →
**Stop recording**. This time there is no alert. This recording exists to check
the permissions; you can delete it later. The first real transcription
downloads the transcription model, about 1.5 GB (see Step 11).

**If MeetRec is missing from the Microphone list**, note that this list has no
**+** button, so you cannot add it by hand. Reset the request and try again:

```bash
tccutil reset Microphone com.frasal.meetrec.menubar
```

Then start a recording from ✒︎ again and the prompt returns.

**If MeetRec is missing from the Screen Recording list**, make one recording
attempt first (point 2 above); that is what puts it there. If you still see
nothing, check the logs described in [Troubleshooting](#troubleshooting).

## Step 10 — Verify the installation

```bash
./meetrec.sh doctor
```

Every line should start with `✓`:

```text
✓ macOS 13+: 15.2
✓ Swift compiler: /usr/bin/swiftc
✓ Recorder binary: /Users/you/meetrec/.recorder
✓ Menu bar binary: /Users/you/meetrec/MeetRec.app/Contents/MacOS/MeetRec
✓ Python 3.10+: 3.12.7
✓ faster-whisper: Python module
✓ Apple Silicon: required for speaker separation
✓ pyannote.audio: version 4.0.1
✓ Hugging Face token: HF_TOKEN or HUGGINGFACE_TOKEN
✓ Model access: pyannote/segmentation-3.0: ok
✓ Model access: pyannote/speaker-diarization-3.1: ok
✓ Model access: pyannote/speaker-diarization-community-1: ok
✓ Recordings directory: /Users/you/meetrec/recordings
```

The version numbers and paths on your Mac will differ. If you turned speaker
separation off, the `Apple Silicon`, `pyannote.audio`, `Hugging Face token` and
`Model access` lines are not shown.

Any `✗` points at the step that did not take: `Python 3.10+` at Step 3,
`faster-whisper` or `pyannote.audio` at Step 5, `Apple Silicon` means this is an
Intel Mac, `Hugging Face token` and `Model access` at Step 6, the two binaries at
Step 8. The `Model access` lines need an internet connection.

Below the list, `doctor` prints one more line, something like:

```text
Permissions as seen by this terminal: screen_audio=missing microphone=authorized identity=com.frasal.meetrec.menubar
```

**When you run it from Terminal, this describes Terminal, not MeetRec** —
`screen_audio=missing` here does not mean the app is missing the permission.
macOS attributes a permission check to whichever process asks, so run from
Terminal it cannot see the app's own grants. Ignore this line and trust System
Settings instead; the note printed underneath it says the same thing. It is also
excluded from the pass/fail result on purpose.

To see MeetRec's own grants, click ✒︎ → **Run doctor** in the menu bar. That
runs inside MeetRec, so its permissions line is the real one. The output goes to
`recordings/.menubar.log` in the project folder.

## Step 11 — Record your first meeting

**Wear headphones.** Without them your microphone picks up the other
participants through your speakers, and they end up transcribed twice — once
from each track.

1. Click ✒︎ and choose the language of the meeting: **Italiano**, **English**,
   **Español**, or **Auto detect** if you are not sure.
2. The icon changes to show that recording is running.
3. At the end, click ✒︎ → **Stop recording**.

Transcription then starts in the background — you can start another meeting
while it runs. The menu-bar icon shows the progress percentage, and the menu
holds a progress bar naming the current phase. You get a macOS notification when
it is done.

**The first transcription is much slower than the following ones**, because it
downloads the `medium` model (about 1.5 GB) into `~/.cache/huggingface` before
transcribing anything. Later recordings reuse it. Speaker separation adds about
32 MB on its first run. To watch it happen:

```bash
tail -f recordings/.menubar.log
```

Press `Ctrl + C` to stop watching; this does not stop the transcription.

### Where your files are

Click ✒︎ → **Open recordings**, or look in `~/meetrec/recordings/`. Each session
is a folder named after its start time, for example
`2026-07-29T10-30-00/`, containing:

- `transcripts/transcript.md` — the readable transcript; start here.
- `transcripts/transcript.txt`, `.srt`, `.json` — plain text, subtitles, and the
  full structured output with timings and speakers.
- `transcripts/transcript.speakers.txt` — grouped by speaker.
- `audio/mic.caf` and `audio/system.caf` — your voice and everyone else's, kept
  as separate recordings.
- `transcribe.log` — short status lines about the transcription. The detailed
  output of recordings made from the menu bar (model download, progress,
  errors) is in `recordings/.menubar.log` instead. Read both when something
  failed.

Your voice is always labelled `ME`. Remote participants are separated on the
system track when speaker separation is enabled.

### Recording from Terminal instead

`./meet.sh` records from Terminal. Such recordings use **Terminal's**
permissions, not MeetRec's, so Terminal itself (or iTerm) must be allowed under
Microphone and Screen & System Audio Recording. Whether speakers are separated
follows your configuration unless you pass `diar` or `nodiar`.

---

## Troubleshooting

### No ✒︎ icon in the menu bar

Check whether the background agent is running:

```bash
launchctl print "gui/$(id -u)/com.frasal.meetrec" | head -5
```

If it reports that the service could not be found, run `./meetrec.sh install`
again. If it is running but invisible, look at the error log:

```bash
cat .menubar.stderr.log
```

A menu bar crowded with icons can also simply hide it — try quitting another
menu-bar app.

### "Python environment missing" alert when starting a recording

The `.venv` folder is missing or incomplete. Redo Step 5. This also happens if
you moved or renamed the project folder after installing: run
`./meetrec.sh install` again from the new location.

### `Missing Python environment` when running ./meet.sh

Same cause as above — redo Step 5.

### doctor shows ✗ Python 3.10+, or pip cannot find pyannote.audio>=4

The environment was built with the Python that comes with the Command Line
Tools (3.9), which is too old. pip stops with `Could not find a version that
satisfies the requirement pyannote.audio>=4`. Install Python 3.12 (Step 3), then
throw the environment away and make a new one:

```bash
rm -rf .venv && python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -U pip
```

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

### doctor shows ✗ Apple Silicon

This is an Intel Mac, or your Python is an Intel build running under Rosetta
(see the `arm64` check in Step 3). The speaker-separation packages cannot be
installed here. Use `requirements.txt` and turn the feature off, as described in
[Running without speaker separation](#running-without-speaker-separation).

### doctor shows ✗ pyannote.audio

Either the speaker-separation packages are not installed, or an old version is.
The line says which: `missing — install requirements-diarization.txt`, or
`version 3.x — 4.x required`. Install the full set:

```bash
.venv/bin/pip install -r requirements-diarization.txt
```

or turn speaker separation off, as described in
[Running without speaker separation](#running-without-speaker-separation).
If pip refuses, see the Python 3.10+ entry above.

Left as is you still get transcripts, but every remote participant is labelled
`REMOTE` instead of being told apart, and each transcript opens with a line
saying so. To get the speakers separated on a meeting already transcribed this
way, install the packages and then
[re-transcribe the session](#re-transcribing-a-session).

### doctor shows ✗ Hugging Face token

The `.env` file is missing, is in the wrong folder, or has a typo. It must sit in
the project root, next to `meetrec.sh`, and contain a line `HF_TOKEN=hf_…`.
Check with `cat .env` from inside `~/meetrec`.

### doctor shows ✗ Model access

Each of the three `Model access` lines checks one Hugging Face repository with
your token:

- `accept the conditions at https://huggingface.co/…` — open that page while
  signed in to the account that owns the token and accept the conditions
  (Step 6, point 2). Do it for all three pages.
- `token rejected` — the token is wrong, revoked or mistyped. Create a new
  **Read** token (or a fine-grained one with "Read access to contents of all
  public gated repos you can access") and rewrite `.env`.
- `could not check: …` — usually no internet connection. Try again later.

### "Cannot access the pyannote diarization model on Hugging Face"

The token is valid but cannot reach the models. Almost always one of:

- The conditions in Step 6, point 2 were never accepted — do it for **all three**
  pages. The third one, `speaker-diarization-community-1`, is easy to miss.
- The token belongs to a different Hugging Face account than the one that
  accepted them.
- The token is *fine-grained* without "Read access to contents of all public
  gated repos you can access". Create a plain **Read** token instead.

`./meetrec.sh doctor` shows which of the three pages is the problem.

The meeting is still transcribed while you sort this out — without speaker
separation, and with a warning at the top of the transcript. Once the token
works, [re-transcribe the session](#re-transcribing-a-session) to get the
speakers apart.

### A "torchcodec is not installed correctly" warning

pyannote prints this when FFmpeg is not installed. It is harmless: MeetRec hands
it the audio directly. Ignore it.

### The transcript starts with a line beginning "[MeetRec]"

That line is MeetRec telling you the transcript was produced with something
switched off — almost always speaker separation, because pyannote is missing or
the Hugging Face token was refused. The transcript itself is complete and
usable; only the labelling of remote participants is affected, and they all
appear as `REMOTE`.

The same text is in `transcripts/transcript.md` as a quoted warning and in
`transcripts/transcript.json` under `warnings`. Fix the cause it names, then
[re-transcribe the session](#re-transcribing-a-session) if you want that meeting
redone.

### Re-transcribing a session

Transcribing again is safe: it overwrites the files in `transcripts/` and never
touches the recorded audio in `audio/`.

A finished session is not picked up again unless you force it, which is what
`--force` is for. Replace `recordings/2026-07-29T10-30-00` with the folder of
the meeting you want:

```bash
.venv/bin/python -m meeting_recorder.control process recordings/2026-07-29T10-30-00 --force
```

This transcribes while you wait, printing progress. Leave it running until the
prompt returns.

### macOS asks for permissions again after every rebuild

The programs are ad-hoc signed — Step 7 was skipped, the certificate is not
trusted, or it has expired (the default validity is one year). Create the certificate, then clear the stale grants:

```bash
tccutil reset ScreenCapture com.frasal.meetrec.menubar
```

```bash
tccutil reset Microphone com.frasal.meetrec.menubar
```

Then run `./meetrec.sh install` and grant the prompts one last time.

### "Recording failed" alert, or "Failed to start" / "no audio tracks" from ./meet.sh

The permissions from Step 9 are not in place. Check both **Microphone** and
**Screen & System Audio Recording** in System Settings → Privacy & Security.

- If the menu bar shows the alert **Recording failed**, the permissions belong
  to **MeetRec**. Restart MeetRec after changing either.
- If `./meet.sh` prints `Failed to start: …`, the recorder exited because a
  permission is missing, before `meet.sh` made its own check. If it prints
  `Recording failed or produced no audio tracks`, neither track captured
  anything. Either way the permissions belong to **Terminal** (or iTerm), not
  MeetRec.

### Everything is transcribed twice

You recorded without headphones, so your microphone captured the other
participants coming out of your speakers. Both tracks then contain the same
speech. Use headphones.

### Transcription seems stuck

For a recording made from the menu bar, watch the worker log:

```bash
tail -f recordings/.menubar.log
```

For one made with `./meet.sh`, watch that session's log instead (replace the
folder name with yours):

```bash
tail -f recordings/2026-07-29T10-30-00/transcribe.log
```

During the first run you will see the model download. Press `Ctrl + C` to stop
watching the log — this does not stop the transcription.

### The transcript is full of repeated filler like "Okay."

A microphone recorded very quietly can make the model invent speech during
silence. MeetRec levels and filters the audio to prevent this, and the settings
are on by default. If it still happens, see *Speech leveling and filtering* in
[README.md](README.md).

---

## Running without speaker separation

Speaker separation is what distinguishes individual remote participants. Without
it, you still get a full transcript with your own voice labelled `ME` and
everyone else on the system track — you just do not learn which remote voice is
which. In exchange you skip Step 6 entirely, save about 1 GB of downloads, and
transcribe faster.

**Intel Macs belong here.** The speaker-separation packages cannot be installed
on Intel, so install the smaller set in Step 5 instead:

```bash
.venv/bin/pip install -r requirements.txt
```

Then turn the feature off by creating the configuration file:

```bash
mkdir -p ~/.config/meetrec
```

```bash
echo '{"transcription": {"diarize_system": false}}' > ~/.config/meetrec/config.json
```

This replaces any existing file, which is what you want on a fresh install. If
you already have settings in it, open `~/.config/meetrec/config.json` in a text
editor and set `"diarize_system": false` there instead. All the other settings
are explained in [README.md](README.md).

If you came here from Step 5, MeetRec is not installed yet: skip the restart and
continue with Step 7 (skip Step 6, the token). Otherwise restart MeetRec: ✒︎ →
**Quit MeetRec**, then `./meetrec.sh install`. `./meet.sh` reads the file each time, so it needs no
restart; it follows the configuration unless you pass `nodiar` (off) or `diar`
(on) as its second argument.

## Updating MeetRec

```bash
cd ~/meetrec
```

```bash
git pull
```

Update the Python packages (use `requirements.txt` instead if you run without
speaker separation):

```bash
.venv/bin/pip install -U -r requirements-diarization.txt
```

```bash
./meetrec.sh install
```

```bash
./meetrec.sh doctor
```

If `doctor` shows ✗ Python 3.10+, rebuild the environment as described in
[doctor shows ✗ Python 3.10+, or pip cannot find pyannote.audio>=4](#doctor-shows--python-310-or-pip-cannot-find-pyannoteaudio4).

`install` recompiles only what actually changed, so permissions are left alone
when the code did not move.

## Uninstalling

Stop MeetRec and remove it from login:

```bash
./meetrec.sh uninstall
```

`./meetrec.sh uninstall` keeps your recordings, because the project folder stays
where it is. To remove everything, delete the project folder
(`rm -rf ~/meetrec`); move `recordings` out of the project folder first if you
want to keep them. A few things stay behind; delete them as well if you want
a clean Mac:

- `~/Library/LaunchAgents/com.frasal.meetrec.plist.disabled` — the disabled
  LaunchAgent.
- `~/Library/Application Support/MeetRec`
- `~/.cache/huggingface` — the downloaded models, about 1.5 GB. Other programs
  may use them too.
- `~/.config/meetrec` — your configuration, if you created one.
- The `MeetRec Dev` certificate: in Keychain Access, open the login keychain,
  My Certificates, and delete it if you no longer need it.

You can also remove MeetRec from System Settings → Privacy & Security
(Microphone and Screen & System Audio Recording).
