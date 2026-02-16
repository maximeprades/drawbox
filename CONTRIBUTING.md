# Contributing

DrawBox is a weekend project that turned into something real. Contributions are welcome!

## What's Welcome

- Bug fixes
- Improvements to the build guide
- New voice lines or personality tweaks
- Better image post-processing (cleaner line art)
- Support for other printers
- Translations of the build guide
- Photos/videos of your own DrawBox build

## What to Avoid

- Adding authentication or user management (this is a home toy)
- Virtual environments or complex packaging (single-file simplicity is intentional)
- Replacing OpenAI with other providers (feel free to fork for that)
- Over-engineering the architecture (one script, one dashboard, that's it)

## How to Contribute

1. Fork the repo
2. Create a branch (`git checkout -b fix/printer-timeout`)
3. Make your changes
4. Test on a Pi 5 if possible (or use the simulator)
5. Open a PR with a clear description of what and why

## Pi 5 Gotchas

If you're modifying hardware-related code, keep these in mind:

- **gpiozero only** — RPi.GPIO does not work on Pi 5
- **44100Hz sample rate** — The CHANGEEK USB mic doesn't support 16000Hz
- **No `continue` in try/finally** — It causes the `is_busy` flag to stick
- **0.5s sleep before audio** — USB speakers need time to wake up

## Questions?

Open an issue. There are no dumb questions when it comes to wiring buttons to a Pi.
