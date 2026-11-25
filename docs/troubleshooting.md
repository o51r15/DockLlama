# Troubleshooting

| You see | What to do |
|---|---|
| "DockLlama can't see Docker on this machine" | The compose file needs the `/var/run/docker.sock:/var/run/docker.sock` line under `volumes`. |
| "DockLlama found Docker but isn't allowed to use it" | Remove any `user:` line from the compose file (use PUID/PGID for the data folder instead). Rootless Docker and Podman aren't supported yet. |
| "Nothing answered at …:11434" | Ollama only listens on its own computer by default. See [Ollama on another computer](setup.md#ollama-on-another-computer). |
| "Something answered at that address, but it isn't Ollama" | Check the port number (Ollama uses 11434). |
| "This Ollama is version X. DockLlama needs 0.9.0 or newer" | Update Ollama on that machine. |
| A model shows "Can't be used" | Embedding models can't write answers; models whose memory (context) is under 4K are too small. Pick another. |
| "Too slow on this system" | That model answered too slowly on this computer. Try a smaller one, or one marked as fitting your hardware. |
| "This container's logs can't be read (log driver: …)" | Its log driver doesn't keep logs Docker can read back. DockLlama still watches its state. |
| Couldn't check, "The AI's answer couldn't be read" | Try Retest on the model; if it keeps happening, pick another model. |
| An answer warning "the input may have been cut off" | Raise the model's context size in Settings → AI → Advanced. |
| Can't reach DockLlama Remote | Is it running on that machine, is port 8557 open, and is the address right? |
| "didn't accept the pairing token" | Copy the snippet again from Settings → Machines and recreate the Remote container. |
| A test message didn't arrive | Check the details; ntfy needs the app subscribed to the exact topic. |
| "No answer from ntfy.sh in time" (or another service) although it works from your browser | Usually a VPN or Tailscale exit node on the machine running DockLlama; see [below](#a-vpn-or-tailscale-exit-node-on-the-docker-machine). |
| "This DockLlama Remote is … older than DockLlama" | On that machine, next to the Remote's compose file: `docker compose pull && docker compose up -d`. |
| Forgot the password | `docker exec -it dockllama dockllama reset-password` |

## A VPN or Tailscale exit node on the Docker machine

**What you see:** sending a notification fails with "No answer from ntfy.sh in time" (or Discord, Telegram…), web
page checks or downloads to some sites time out, but the same address works fine from the machine itself or
from your browser.

**Why:** the machine's internet traffic goes through a tunnel (a VPN, or a Tailscale exit node) that only carries
smaller packets, often 1280 bytes. Docker gives containers 1500-byte packets. The machine itself knows about the
smaller limit, but traffic coming out of a container doesn't, and larger packets are silently dropped inside the
tunnel. A secure (HTTPS) connection needs a few large packets to start, so it stalls.

**Check:** on the Docker machine, `ip route get 1.1.1.1` shows `dev tailscale0`, `wg0` or similar, and
`ip link show <that device>` shows `mtu 1280` (or anything below 1500).

**Fix:** give DockLlama's own network the same packet size. Add this to the end of DockLlama's compose file,
using the number from the check, and recreate it with `docker compose down && docker compose up -d`:

```yaml
networks:
  default:
    driver_opts:
      com.docker.network.driver.mtu: "1280"
```

Other containers on that machine can have the same problem with some sites. To fix it for all of them at once,
the usual way is "MSS clamping" on the machine:
`iptables -t mangle -A FORWARD -o tailscale0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu`
(it has to be made to survive a reboot).
