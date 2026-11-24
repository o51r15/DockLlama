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
| Forgot the password | `docker exec -it dockllama dockllama reset-password` |
