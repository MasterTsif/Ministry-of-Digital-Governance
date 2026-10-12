# Ministry of Digital Governance

Discord bot for class timetable updates, exam periods, room changes and GIF blocking.

## Setup and run

Python 3.11 or newer. Run these commands in the project folder using PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

For a new installation, set `DISCORD_TOKEN` and `DISCORD_GUILD_ID` in `.env`.
Set `ALERT_CHANNEL_ID` and `ALERT_ROLE_ID` for timetable alerts and subscriptions.
Enable the features you use, then start the bot:

```powershell
.\.venv\Scripts\python.exe -m ministry_bot
```

## Configuration

- `SCHEDULE_ENABLED`: automatic class timetable checks.
- `EXAMS_ENABLED`: automatic exam publication and room checks.
- `GIF_BLOCK_ENABLED`: GIF filtering.
- `NOTIFICATIONS_ENABLED`: role subscriptions using persistent buttons and private confirmations.
- `GIF_EXEMPT_IDS`: exempt user or role IDs.
- `GIF_EXEMPT_CHANNEL_IDS`: exempt channel, thread or category IDs.

For GIF filtering, enable **Message Content Intent** in the
[Discord Developer Portal](https://discord.com/developers/applications).
Grant the bot View Channel, Read Message History and Manage Messages.
Alerts need Send Messages and Embed Links. Subscriptions also need Manage Roles;
place the bot's role above the notification role. Make the notification role
mentionable or allow the bot to mention all roles in the alert channel.

## Commands

| Command | Purpose |
| --- | --- |
| `/ping` | Show connection latency. |
| `/schedule status` | Show timetable monitoring status. |
| `/schedule check` | Check the PDF immediately. |
| `/schedule preview` | Privately preview the alert and its working subscription buttons in the alert channel. |
| `/exams status` | Show exam monitoring status. |
| `/exams check` | Check exam publication and room changes. |
| `/gifs status` | Show GIF filtering and permissions. |
| `/notifications status` | Show your notification subscription. |
| `/notifications setup` | Create or restore the subscription message. |

Manual checks, schedule preview, GIF status and notification setup require **Manage Server**.

## Tests and diagnostics

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
.\.venv\Scripts\python.exe -m ministry_bot --inspect-schedule
.\.venv\Scripts\python.exe -m ministry_bot --inspect-exams
```

Tests use local simulations and mocked Discord objects. Inspection commands
read the real sources without saving state or connecting to Discord.
