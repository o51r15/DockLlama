# Notifications

Settings → Notifications (also the last step of the setup guide). Pick a service, fill in one or
two fields, **Send test**, **Save**. You can add several.

| Service | What you need |
|---|---|
| **ntfy** (recommended) | Nothing. DockLlama makes a private topic; scan the QR code with the ntfy app. Your own ntfy server works too. |
| Discord | A channel webhook URL |
| Telegram | A bot token (from @BotFather) and your chat ID (from @userinfobot) |
| Gotify | Your server address and an app token |
| Pushover | Your user key and an app token |
| Email | Your address and an app password (Gmail, Outlook, Yahoo and most others work) |
| Other | Any [Apprise](https://github.com/caronc/apprise/wiki) URL |

## What gets sent

- **A problem starts** (Having problems / Down or failing). "Needs a look" only if you tick it.
- **It recovers**, if you were told about it.
- **"Still broken"** every 6 hours while it lasts (or 1, 3, 12, 24 hours, or never).
- **DockLlama can't check**: one message when containers couldn't be checked 3 rounds in a row (the
  AI server or a machine is down), and one when it works again.
- **Restarts** DockLlama made, and whether they worked.
- **The daily summary**, at the time you choose (default 08:00 your time): written by your AI, or a
  plain list without it.

You get one message when something changes, not one per check. More than 3 changes at once arrive
as one message. Each destination has its own ticks for what it receives, and can be paused.

**Quiet hours** (Settings → Checking and fixing) hold messages; whatever is still true when they end
is sent then.

Messages link back to the container's page, using the address you set them up from.
