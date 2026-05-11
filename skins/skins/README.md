# Sonosphere Starter Skin Pack

50 switchable skins using the proposed **JSON + CSS variables** architecture.

Each skin folder contains:

- `skin.json` — metadata, preview path, theme path, feature support, visualizer defaults
- `theme.css` — CSS variables scoped to `:root[data-skin="skin-id"]`
- `preview.svg` — lightweight generated preview card

## Example usage

```html
<html data-skin="neon-pulse">
```

```js
document.documentElement.dataset.skin = "retro-wmp";
```

Then import the selected skin's `theme.css`, or bundle all skin CSS files if you prefer instant switching.

## Recommended app structure

```txt
/skins
  /neon-pulse
    skin.json
    theme.css
    preview.svg
  /retro-wmp
    skin.json
    theme.css
    preview.svg
  manifest.json
```

Generated for Sonosphere.
