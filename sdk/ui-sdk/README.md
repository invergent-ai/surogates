# @invergent/ui-sdk

Surogate's shared theme and UI components, used by the web client, the chat
SDK (`@invergent/agent-chat-react`), the desktop shell and the ops console.
The ops console is the design authority: these components are its own.

## Theme

In a Tailwind v4 app, import the theme once, after Tailwind:

```css
@import "tailwindcss";
@import "@invergent/ui-sdk/theme.css";
```

`theme.css` brings the colour tokens, the fonts (Inter, Fraunces, JetBrains
Mono), the type scale, the colour/font/radius utilities, the `dark` variant
and the base styles, and points Tailwind at this package's components so
their classes are generated.

Without Tailwind, `@invergent/ui-sdk/tokens.css` is the tokens alone: plain
custom properties (`--background`, `--foreground`, `--card`, `--muted`,
`--border`, …).

Every colour is `light-dark(<light>, <dark>)`. The root's `color-scheme`
picks the half — the system's by default, a `light`/`dark` class
(next-themes) or `data-theme="light|dark"` once the app has chosen.

## Components

One import per component:

```tsx
import { Button } from "@invergent/ui-sdk/button";
import { Dialog, DialogContent } from "@invergent/ui-sdk/dialog";
import { cn } from "@invergent/ui-sdk/lib/utils";
```
