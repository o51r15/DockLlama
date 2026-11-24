# AI and models

DockLlama uses one Ollama server at a time (Settings → AI). Everything it learns about a server,
model test results, Advanced settings, the model in use, is kept when you switch to another server,
and comes back when you switch back. Servers are recognised by the models installed on them (names,
versions and when they were downloaded), not by IP address, so a new address doesn't lose anything.
DockLlama always asks before treating a server as one it has seen.

## Which model?

Ollama can't say how much graphics memory its computer has, so DockLlama asks once ("What does the
machine running Ollama have?"). It then suggests a model:

1. Models that can't write answers (embedding models) and cloud models are left out.
2. Models clearly too big for that hardware drop down the list.
3. Once models have been **tested on your server**, the best result wins: the highest score, and
   among scores within 2 points, the one that fits on the graphics card and answers fastest.
   Before that, DockLlama uses its own reference results and prefers a model that's already loaded.

DockLlama only suggests. You pick, with **Use this model**, and it never switches by itself.

**Suggested downloads** are models DockLlama has tested that fit your hardware and aren't installed;
one click downloads them through Ollama, and they're tested when the download finishes.

## Testing models

Results differ from computer to computer, so every model can be tested on yours. A test asks 8
practice questions about container logs (a healthy database, an out-of-memory loop, routine noise, an
outside service being down, and so on) and records the score out of 100, seconds per check, load
time, whether it fit on the graphics card, and whether any answer was cut off.

- **Test / Retest** on any model. Earlier results are kept under **Results**, with the settings used.
- **Test all**: every usable model is ticked (models marked "may not fit" too: your hardware answer is a
  guess, and testing finds out; one that's far too slow stops early). Untick any, see the time estimate, then run
  it in the background, now while you watch, or tonight (at the start of quiet hours, default 01:00).
- **One test at a time.** While one runs, the other test buttons are greyed out and say what's running.
  Pause and Cancel stay available. Container checks keep running between models.
- A model that's far too slow here stops early ("Too slow on this system"). A question that times
  out once is tried again at the end, in case the server was just busy.
- The tested model is unloaded afterwards, unless it was already loaded (another app is using it) or
  it's the model DockLlama uses.
- Testing loads each model in turn, so other apps using the same Ollama will be slower while it runs.
- New and downloaded models are tested automatically (Settings → AI; you can turn that off).

When a background run finds a model that's clearly better (3 points higher at similar speed, or the
same score and 30% faster, without moving off the graphics card), a banner offers it on your next
page anywhere in DockLlama: **Review results**, **Switch to it** or **Keep current**.

## Qwen-based models and links

Some security testing in 2026 reported that Qwen models running as agents with shell access occasionally tried to send files to outside servers. DockLlama doesn't give any model tools: the model only receives the check details and writes an answer, and DockLlama never runs or opens anything the model writes. So a Qwen-based model can't do anything here except answer the check questions.

To keep it that way:

- **Qwen-based models are labelled.** DockLlama looks at the model's name, family, architecture and recorded base model, so fine-tunes with other names (built on qwen2, qwen3 and so on) are labelled too.
- **You can leave them out of suggestions.** Settings → AI → "Don't suggest Qwen-based models". They're still tested and you can still choose one yourself; they just won't be suggested or announced as a better model. It's off by default.
- **Links are removed from everything the AI writes**, for every model: web addresses, markdown links and email addresses become "[link removed]" before anything is stored, shown or sent in a notification. File names (config.yml) and plain IP addresses are kept.
- **No tools for Qwen-based models.** If a later DockLlama version ever gives models tools, Qwen-based models won't get them until more is known.

## Advanced

Per model, behind **Advanced**. Only what that model supports is shown:

| Setting | Notes |
|---|---|
| Thinking | Greyed out for models that don't think. On/off, or levels (low / medium / high) for models like gpt-oss that can't turn it off. Default: off, or the lowest level. |
| Context size | A slider from 4K up to that model's own maximum. Default 8K. DockLlama's checks use about 4K. |
| Keep the model loaded | Between checks (default), always, or unload after each round to free graphics memory. |
| Max answer length | Default 500 tokens, 2,000 when thinking is on (thinking uses the same budget). |
| Temperature | Default 0.1. |
| Timeout | Default 120 seconds. |
| Retry once / exact format | On by default. |

**Reset to recommended** puts everything back; **Save and test with these settings** shows whether a
change helped.
