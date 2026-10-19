# Amizone-Attendence-Watch-System
This is a System which will notify you when your attendance get marked on the Amizone website Of Amity University

Checks your Amizone class dots on a schedule, in the background, and alerts you
when one turns **red (absent)** or **blue (unmarked)**. You do not need to be
logged in to Amizone or have the site open. Every alert is saved so you can
review it later.

## How it works
Every 30 minutes (you choose) it logs in to Amizone with your own account, reads
the same class-schedule data the Home page uses for the last 7 days, and compares
the dot colours with the previous check.

- red dot: red alert (phone push tagged red, high priority, plus a desktop notification)
- blue dot on a class that ended more than 2 hours ago: yellow alert
- a dot that goes from marked (green/red) back to blue: yellow alert right away
- green dot: no alert
- future classes are ignored; the same problem is never announced twice

## Install (Windows, macOS, Linux)
1. Install Python 3.9 or newer from python.org (on Windows tick "Add python.exe to PATH").
2. Unzip this folder and open a terminal in it.
3. `pip install -r requirements.txt`
4. `python amizon_watch.py setup` (tests your login once, stores the password in
   your system keychain, never in a file)
5. `python amizon_watch.py autostart` (starts now and at every login, no window)

On Windows use `python`, on macOS/Linux you may need `python3`.

## Phone alerts (works anywhere)
Install the free **ntfy** app, subscribe to the topic name `setup` shows you.
Keep that name private; anyone who knows it can read your alerts.

## Commands
| command | what it does |
|---|---|
| `setup` | save login, interval and alert options |
| `run` | check forever (what autostart runs) |
| `once` | check one time and exit |
| `history` | list every alert received |
| `export file.csv` | save all alerts as CSV |
| `diagnose` | step-by-step report on why login works or fails (no password shown) |
| `cookie` | use your browser's logged-in session instead of a password |
| `debug` | show what Amizone returned (colour values only) |
| `test-notify` | send a red and a yellow test alert |
| `autostart [--remove]` | start at login / stop doing that |

Logs are in `~/.amizon-watch/agent.log`.

## If login fails
1. Check that the same enrollment number and password work in your browser, on the **Student** tab.
2. Run `python amizon_watch.py diagnose`. It shows what the login page looks like, each step
   the tool took, and the reason it stopped. The output contains no password, so you can share it.
3. Typical causes and fixes:
   - **Captcha ("I am not a robot") on the login page.** A program cannot solve it. Use
     `python amizon_watch.py cookie` to reuse the session your browser already has. That session
     ends when Amizone logs it out, so you will get an alert and repeat the command.
   - **Login is done by JavaScript, not a normal form.** Same fix: `cookie` mode.
   - **"HTTP 403" or "blocked".** Amizone is refusing this network (often a cloud server or VPN).
     Run the tool on your own computer or phone instead.
   - **"Invalid username or password" shown by the page.** The details are wrong, or the account
     is temporarily locked. Fix that in your browser first; the tool stops after one rejected
     attempt so it cannot make a lock worse.
4. If `diagnose` says login WORKS but alerts still do not appear, run `debug`.

## Runs while your PC is off (optional)
Put this folder in a **private** GitHub repository and add secrets `AMIZONE_USER`,
`AMIZONE_PASSWORD`, `NTFY_TOPIC`. `.github/workflows/amizon-watch.yml` then checks
every 30 minutes. Amizone may block cloud IP addresses, so try `Run workflow` once
and read the log before relying on it.

## On your phone
See MOBILE.md - short version: install the free ntfy app and let the included
GitHub Actions workflow check for you, so it works on iPhone and Android with
nothing installed on the phone except ntfy. Android also has an on-device
option via Termux.

## Try it without touching Amizone
`python tests/mock_amizone.py 8099 scenario.json` starts a fake portal (see the
file header for a sample scenario). Point the agent at it with
`AMIZONE_BASE=http://127.0.0.1:8099`.

## Safety notes
- Use it only for your own account, and check Amizone's rules for automated access.
- It logs in at most once per session and stops for good if the password is
  rejected, so it cannot lock your account by retrying.
- The checks are spaced at least 10 minutes apart; please don't lower that.
- Amizone is reverse-engineered here (from community projects) and can change
  without notice. If alerts stop, run `debug`.
