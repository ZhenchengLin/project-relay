# Manual mode (clipboard, no extension)

The original Project Relay workflow: you stay in control of every step and
Relay only packages text and runs the loop judges. Useful when you do not want
any browser automation.

```bash
prelay register myapp ~/code/myapp
prelay manual use myapp
```

1. Copy ChatGPT's reply, then:

   ```bash
   prelay manual cmd
   ```

   Relay extracts the single shell block and puts it on the clipboard (it
   refuses replies with several blocks). Review it and paste it into your
   terminal yourself.

2. Copy the terminal output, then:

   ```bash
   prelay manual gpt
   ```

   Relay records the cycle, runs the local loop judges and copies a
   ready-to-paste message for ChatGPT (the output plus Git state and the
   judges' verdict). If the judges agree the work is looping it copies a
   human-review report instead.

Other commands: `prelay manual status`, `prelay manual watch`,
`prelay manual history`.

Clipboard support: `pbcopy`/`pbpaste` (macOS), `wl-copy`/`wl-paste`
(Wayland) or `xclip` (X11).
