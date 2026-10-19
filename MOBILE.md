# Using Amizon Watch on your phone

There are two ways to get the red/yellow alerts on your phone. Pick one, or use both.

## Option A - iPhone or Android, no phone battery use (recommended for iPhone)
The checker runs for free on GitHub's servers, on a timer, whether your phone or PC
is on or not. Your phone only needs the **ntfy** app to receive the alert.
Apple does not allow any app - including this kind - to run its own background
polling loop indefinitely, so this cloud option is the reliable way to get alerts
on an iPhone.

1. Install **ntfy** from the App Store or Play Store. Open it, tap "+", and
   subscribe to a topic name you make up (keep it private - anyone who knows it
   can read your alerts). Example: `amizon-yourname-7f2a`.
2. Create a **private** GitHub repository and upload this folder to it.
3. In the repo, go to Settings -> Secrets and variables -> Actions, and add:
   - `AMIZONE_USER` - your enrollment number
   - `AMIZONE_PASSWORD` - your Amizone password
   - `NTFY_TOPIC` - the topic name from step 1
4. Go to the Actions tab, open "amizon-watch", and click "Run workflow" once to
   test it. You should get a notification within a minute or two.
5. It will now run automatically every 30 minutes. Edit the `cron` line in
   `.github/workflows/amizon-watch.yml` to change that.

Tapping the notification opens your Amizone attendance page.

## Option B - Android only, runs on the phone itself, no cloud account needed
Android (unlike iOS) allows a terminal app to keep a script running in the
background, using **Termux**.

1. Install **Termux** from F-Droid (the Play Store version is outdated) -
   https://f-droid.org/en/packages/com.termux/
   Also install **Termux:Boot** from the same source, and open it once so
   Android registers it.
2. In Android's app settings for Termux, turn off battery optimisation, or the
   OS may pause the checks.
3. Open Termux and run:
   ```
   pkg update && pkg install python git termux-api termux-boot
   git clone <your repo URL, or transfer this folder another way>
   cd amizon-agent
   pip install -r requirements.txt
   python amizon_watch.py setup
   ```
   Termux has no system keychain, so `setup` will offer to save your password
   in a scrambled file instead, readable only by your Termux app.
4. Start it automatically on every phone boot:
   ```
   python amizon_watch.py autostart
   ```
5. To send alerts as phone notifications too (not just ntfy), install
   **Termux:API** from F-Droid and run `pkg install termux-api` - desktop-style
   notifications aren't available on Android, so use the ntfy option above for
   push notifications on Android as well; it's simpler and works the same way
   as on iPhone.

## If login fails on the cloud or phone
Run `python amizon_watch.py diagnose` (in Termux, or on a PC) and read the result. Cloud servers are
more likely to be blocked by Amizone than a home or phone connection, and cookie mode does not suit
the cloud option because the browser session expires. If Amizone blocks GitHub, use Option B (Android)
or run the tool on your own computer.

## What I can't build
A real installable iPhone or Android *app* (the kind you get from the App Store
or Play Store) needs Xcode and a Mac (iOS) or a signed Play Store listing
(Android), plus store review. I don't have access to any of that here. Option A
above needs nothing installed except the free ntfy app, which is the closest
equivalent, and works on both platforms today.
