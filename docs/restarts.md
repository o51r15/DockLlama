# Restarts and quiet hours

DockLlama starts in **Watch only**: it tells you about problems and never restarts anything.

To turn restarts on, pick **Auto-restart** in Settings → Checking and fixing. The list of watched containers
opens right there, grouped by machine, with every container DockLlama can restart already switched on.
Switch off any you'd rather leave alone, then click **Turn on auto-restart for N containers**.

Once it's on, the Auto-restart card says how much it covers, for example "Covers 12 of 22 watched
containers · 6 aren't chosen · 4 on a machine set to watch only". **Choose containers** opens the list
again. Each container's page also says whether DockLlama will restart it, and why not if it won't.

Containers on another machine can only be restarted if that machine's DockLlama Remote allows it
(`DOCKLLAMA_REMOTE_RESTARTS=yes` in its compose file). The list, the Machines page and the container page
show "Watch only" for machines that don't, with that line.

## When it restarts

All of these must be true:

- Auto-restart is on, and the container is allowed;
- the machine allows restarts (always for this machine; for other machines, see [Watching other machines](other-machines.md));
- the container is running, and either the AI says it's broken **and** that a restart would help, or
  Docker's health check has failed 3 checks in a row (Docker doesn't restart unhealthy containers itself),
  or its [web page check](how-checks-work.md#web-page-check) has failed 3 checks in a row;
- it isn't quiet hours, the wait since its last restart has passed (default 5 minutes), and it's under
  the limit (default 3 restarts in any hour).

It **never** restarts crash loops, out-of-memory kills or stopped containers (Docker's own restart
policy already handles those, and doing it again doesn't help; you get a message instead), DockLlama
itself, DockLlama Remote, or the container running your AI.

After a restart it checks again at 10 and 30 seconds and tells you whether it worked. At the limit it
stops and sends "*X* needs you", and won't restart that container again until it has run fine for a day.

## Restart groups

On a container's page, give containers the same group name (for example `media`) and an order.
When one of them needs a restart, the whole group restarts, lowest number first (a database before
the app that uses it).

## Quiet hours

Times when DockLlama won't restart anything or send messages. It keeps checking, and sends what's
still true when quiet hours end. "Run tonight" model tests start when quiet hours start.

## Manual restarts

The **Restart** button on a container's page works in either mode (it's greyed out, with the reason,
when the machine doesn't allow restarts).
