# Sonosphere Expansion Skin Pack 02

Another 50 non-redundant Sonosphere skins using the same **JSON + CSS variables** architecture.

Each skin includes:

- `skin.json` — metadata, feature support, material concept, visualizer defaults
- `theme.css` — CSS variables scoped to `:root[data-skin="skin-id"]`
- `preview.svg` — lightweight generated preview image

## Usage

```js
document.documentElement.dataset.skin = "lunar-relay";
```

Load that skin's `theme.css`, or bundle all expansion CSS files if you want instant switching.

This pack intentionally avoids duplicating the first 50 by focusing on new directions: aerospace, radio hardware, studio gear, lifestyle rooms, nature, technical lab UIs, urban concepts, luxury materials, and professional pro-app skins.
