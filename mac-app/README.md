# DrawBox Mac app

A thin native shell (Tauri 2) around the DrawBox web dashboard. It boots to a
bundled offline screen, polls the box, and swaps to the live dashboard the
moment the box answers — so an unplugged box shows a friendly message instead
of a raw Cloudflare error page.

## Build

```bash
npm install
npx tauri build
```

The bundles land in the cargo target directory printed at the end of the
build, under `release/bundle/macos/DrawBox.app` and
`release/bundle/dmg/DrawBox_<version>_aarch64.dmg`.

## Point it at a different box

The dashboard URL is baked in at build time:

```bash
DRAWBOX_URL=https://yourbox.example.com npx tauri build
```

## Regenerate icons

`icon-source.png` is the source of truth. Regenerate everything in
`src-tauri/icons/` with:

```bash
npx tauri icon icon-source.png
```
