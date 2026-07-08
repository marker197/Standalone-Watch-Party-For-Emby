# Emby Watch Party — Installation Guide

*Watch movies and TV shows together with friends and family, each on your own screen, perfectly synchronised. Automatically tracks what you watch on Trakt.*

**For Synology NAS · July 2026**

---

## What Is This?

Emby Watch Party lets you watch a movie or TV show at the same time as someone else — even if they're in a different room or a different house. You press play once and it starts on everyone's screen together. If someone pauses, it pauses for everyone.

It also keeps a record of what you watch on a website called Trakt, which is like a diary for your viewing habits.

---

## What You Need Before You Start

Make sure you have all of the following before you begin:

1. **A Synology NAS with Container Manager installed.** Container Manager is free in the Synology Package Center. If you don't see it, open Package Center and search for "Container Manager", then click Install. If you're not using a Synology NAS, see the [Docker getting started guide](https://docs.docker.com/get-started/) to install Docker on your system.

2. **Emby already running on your NAS.** If you can already watch your movies and TV shows through Emby, you're all set.

3. **A free Trakt account.** You'll create this in Step 1 if you don't have one yet.

> **Good to know:** This whole process takes about 15–20 minutes. You only do it once — after that, everything starts automatically whenever your NAS turns on.

---

## Part A — Set Up Your Accounts

### Step 1: Create a free Trakt account

Open your web browser and go to **trakt.tv**. Click "Join Trakt For Free" and create an account with your email address.

If you already have a Trakt account, skip to Step 2.

---

### Step 2: Create a Trakt "app"

This gives the Watch Party permission to talk to Trakt on your behalf. While logged in to Trakt:

1. Go to **trakt.tv/oauth/applications** in your browser.
2. Click **New Application**.
3. In the **Name** box, type: **Emby Watch Party**
4. In the **Redirect URI** box, type exactly: **urn:ietf:wg:oauth:2.0:oob**
5. Leave everything else as-is and click **Save App**.

After saving, you'll see two long codes on the screen:

- **Client ID** — a long string of letters and numbers
- **Client Secret** — another long string

> **Important:** Write both of these down or copy them into a note on your phone. You'll need them in a few minutes.

---

### Step 3: Get your Emby API key

An API key is a special password that lets the Watch Party talk to your Emby server.

1. Open Emby in your web browser (the same place you go to browse your movies).
2. Click the **gear icon** (Settings) in the top-right corner.
3. Scroll down to the Advanced section and click **API Keys**.
4. Click **New API Key**.
5. Type **Watch Party** as the name and click OK.

A new key will appear in the list. Write it down with your Trakt codes.

---

## Part B — Upload the Files to Your NAS

### Step 4: Create a folder for the Watch Party

1. Open **File Station** on your Synology (from the main DSM desktop).
2. Navigate to the **docker** shared folder. If you don't have one, create a shared folder called **docker** using Control Panel > Shared Folder.
3. Inside the docker folder, create a new folder called **emby-watchparty**.

---

### Step 5: Upload the project files

1. Open the **emby-watchparty** folder you just created.
2. Click the **Upload** button at the top of File Station.
3. Select the **emby-watchparty-standalone.zip** file from your computer and upload it.
4. Once uploaded, **right-click** the zip file and choose **Extract > Extract Here**.
5. After extracting, you should see files like **docker-compose.yml**, **Dockerfile**, and a folder called **app** inside the emby-watchparty folder.

> **Note:** If the zip extracts into a subfolder (e.g. emby-watchparty/emby-watchparty-standalone/), move everything up one level so that docker-compose.yml sits directly inside the emby-watchparty folder.

---

### Step 6: Create your settings file

Inside the emby-watchparty folder, you should see a file called **env.example**. You need to rename it:

1. Right-click **env.example** and choose **Rename**. Change the name to **.env** (just a dot followed by env, nothing else).

> **Can't see the file?** Files starting with a dot are hidden by default. In File Station, click the person icon (top-right) > Settings > General, and tick "Show hidden files". Click OK.

Now double-click the **.env** file to open it in the built-in text editor. Change the values to match your own details:

| Setting | What to put |
|---|---|
| `TRAKT_CLIENT_ID` | Paste your Client ID from Step 2 |
| `TRAKT_CLIENT_SECRET` | Paste your Client Secret from Step 2 |
| `EMBY_URL` | `http://YOUR-NAS-IP:8096` |
| `EMBY_API_KEY` | Paste your API key from Step 3 |
| `DB_PASSWORD` | Make up any password |
| `REDIS_PASSWORD` | Make up another password |
| `JWT_SECRET_KEY` | Type a long random string (30+ characters) |

A few things to know about these settings:

**EMBY_URL** — Replace YOUR-NAS-IP with the IP address of your NAS. You can find this in Synology under Control Panel > Network > Network Interface. Keep the :8096 at the end.

**DB_PASSWORD and REDIS_PASSWORD** — These are new passwords just for this app. They don't need to match anything else. You won't need to type them day-to-day.

**JWT_SECRET_KEY** — Type or mash your keyboard for 30 or so characters. This is an internal security key you'll never need to type again.

> **Important:** Make sure there are no spaces around the = signs. It should be `EMBY_URL=http://...` not `EMBY_URL = http://...`

Click **Save** and close the editor.

---

## Part C — Build and Start in Container Manager

### Step 7: Open Container Manager

From your Synology DSM desktop, open **Container Manager**. You'll find it in the main menu (the grid of squares in the top-left corner).

---

### Step 8: Create a new project

1. In the left sidebar, click **Project**.
2. Click **Create**.
3. Give the project a name — for example: **emby-watchparty**
4. For **Path**, click the browse button and select the **docker/emby-watchparty** folder where you uploaded the files.
5. Container Manager will detect the docker-compose.yml file automatically. You should see a preview of the configuration on screen.
6. Click **Next**.

On the next screen, you may see a Web Portal settings page. You can skip this — just click Next again.

---

### Step 9: Start the project

1. Review the summary and click **Done** (or **Build**, depending on your Container Manager version).
2. Container Manager will now download what it needs and build the Watch Party. This takes 2–5 minutes the first time. You'll see a progress indicator.
3. When it's finished, the project status will change to **Running**. You should see three containers listed: the app, Redis, and Postgres — all with green "running" indicators.

> **Good to know:** The Watch Party will start automatically whenever your NAS boots up. You don't need to come back to Container Manager to start it again.

---

## Part D — Using It for the First Time

### Step 10: Open the Watch Party in your browser

On any device connected to your home network (computer, phone, tablet), open a web browser and go to:

**http://YOUR-NAS-IP:8000**

Replace YOUR-NAS-IP with the same IP address you used in Step 6. You should see the Watch Party dashboard.

---

### Step 11: Select your Emby user and link Trakt

The dashboard will show a list of Emby users from your server.

1. Find your name in the list.
2. Click **Link Trakt** next to your name. A short code will appear (like A1B2C3).
3. On any device, go to **trakt.tv/activate** in your browser.
4. Log in to Trakt if you aren't already.
5. Type in the code from the Watch Party screen and click **Approve**.

After a few seconds, the Watch Party page will update to show your Trakt username next to your Emby name. You're connected.

---

### Step 12: Set up the Emby webhook

This tells Emby to notify the Watch Party whenever you play or stop something, so your Trakt diary updates automatically.

1. Open Emby in your browser and go to **Settings** (the gear icon).
2. Click **Webhooks** (under Notifications). If you don't see Webhooks, install the Webhooks plugin from the Emby plugin catalogue first.
3. Click **Add Webhook**.
4. For the URL, type: **http://YOUR-NAS-IP:8000/webhook/emby**
5. Tick all the boxes underneath, as well as **Users mark played**.
6. Click **Save**.

From now on, anything you watch on Emby will automatically appear in your Trakt history.

---

## Part E — Hosting a Watch Party

Now for the fun part. Here's how to watch something with a friend or family member:

1. Go to the Watch Party page (click the link on the dashboard, or go to **http://YOUR-NAS-IP:8000/watch-party**).
2. Search for a movie or TV show using the search box.
3. Click **Create Party**. You'll get a 6-letter join code like ABC123.
4. Send that code to the person you want to watch with — by text, phone call, whatever you like.
5. Your friend opens the same Watch Party page in their browser, selects their own Emby username from the drop-down list, and clicks **Join Party**, or enters the code. They must select their username or their device won't show up.
6. Once everyone has joined, click **Start**. The movie or show will begin playing on all your Emby devices at the same time.

During the party you can pause and skip forwards or backwards — the controls affect everyone's screen. There's also a chat box for sending messages while you watch.

> **Remember:** Everyone watching needs to be logged in to an Emby app (on a TV, tablet, phone, or computer) before you press Start. The Watch Party controls those apps remotely — if no app is open, there's nothing to control.

---

## Troubleshooting

### The Watch Party page won't open

Open **Container Manager**, click **Project** in the sidebar, and check that your emby-watchparty project shows "Running". If it shows "Stopped", select it and click the **Start** button (the play triangle).

---

### It says it can't connect to Emby

Double-check the EMBY_URL in your .env file. It should start with `http://` and include the port (usually `:8096`). Make sure Emby is running.

You can test the connection from the Watch Party's own Settings page at http://YOUR-NAS-IP:8000/settings — click "Test Connection" next to Emby.

---

### My friend joined but nothing plays on their screen

They need to have an Emby app open and logged in before you click Start. The Watch Party sends a remote play command — if there's no app running, there's nothing to receive it.

---

### Trakt isn't recording what I watch

Check two things: is the webhook set up in Emby (Step 12)? And is your Trakt account linked (your Trakt username should appear on the dashboard next to your Emby name)?

You can also check the Activity Log on the dashboard for error messages.

---

### How do I update to a newer version?

Replace the files in the emby-watchparty folder with the new ones using File Station (keep your .env file). Then open Container Manager, select the project, click the **Action** menu, and choose **Build**. Your settings and watch history are stored in the database and won't be lost.

---

### How do I back up my data?

Go to http://YOUR-NAS-IP:8000/settings and click "Backup Database". This creates a downloadable backup file. Keep it somewhere safe. You can restore it from the same page if needed.

---

*That's it — enjoy your watch parties!*
