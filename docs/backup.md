# Backup and restore

Settings → **Backup**.

## Where backups go

Into the `backups` folder next to your compose file, through this line under `volumes:`:

```yaml
      - ./backups:/app/backups
```

Without it, backups are kept inside the container and are lost if the container is removed. The Backup page warns you when the folder isn't mapped.

## What's in a backup

One `.zip` file with all your settings and DockLlama's database: containers and their notes, machines, AI servers, models, test results, your AI instructions, notification destinations and history.

**Your password and sign-in sessions are never included.** Notification tokens (for example a Discord webhook or a Telegram bot token) and DockLlama Remote pairing tokens *are* included, so keep backup files private.

## Automatic backups

On by default: once a day, keeping the last 7. You can choose every 6, 12, 24, 48 or 72 hours or once a week, and how many to keep. **Back up now** makes one straight away; backups you make yourself are kept until you delete them.

## Restoring

Click **Restore…** next to a backup, or pick a downloaded backup file under "Restore from a file". Then tick what to bring back:

| Group | What it does |
|---|---|
| Checking and fixing settings | Puts back how often to check, restart mode and limits, quiet hours, log lines. |
| Notifications | Replaces the list of places messages go, plus reminder and daily-summary settings. |
| Other settings | Time zone, history retention, backup schedule. |
| AI servers, models and test results | Replaces them with the backup's, including which model is in use. |
| AI instructions | Replaces your prompts and which one is in use. |
| Machines | Puts back DockLlama Remote machines, matched by name. Machines added since stay. |
| Containers | Puts back watched/not watched, notes for the AI, restart groups and permissions, matched by machine and container name. Containers added since stay. |
| History | Replaces check results, events and CPU/memory history, matched to containers by name. Old messages are never sent again. |

Anything you don't tick stays as it is. Before restoring, DockLlama saves a backup of how things are now ("Saved before a restore"), so you can undo a restore by restoring that one. A restore that touches AI servers or AI instructions waits until no model test is running.

A backup made by an older DockLlama is upgraded as it's restored. A backup from a newer DockLlama can't be restored until you update.

Your password is never changed by a restore. If you've forgotten it, see [Troubleshooting](troubleshooting.md).
