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

The look is the ops console's Work pages: sentence case, 13–14px medium
text, rounded corners (7px buttons, 9–10px fields, rows and tabs, round
pills), faint foreground fills, muted text that brightens on hover, amber
for what is picked or primary. Besides the primitives (`button`, `input`,
`select`, `tabs`, `dropdown-menu`, `toggle`, `toggle-group`, `dialog`, …),
the kit carries Work's own components:

| Import | Exports | What it is |
| --- | --- | --- |
| `sidebar` | `Sidebar`, `SidebarBrand`, `SidebarUserBar`, `SidebarAction`, `useSidebar` | The desktop sidebar: open, or an icon rail that opens on hover, with the pin toggle at its bottom. `ListRow` and `SectionLabel` inside it collapse with the rail; outside it (a phone drawer) the parts draw open. |
| `list-row` | `ListRow`, `ListGroup` | A list entry (icon, label, description, count, chevron) in three densities; a button, or any link via `asChild`. |
| `section-label` | `SectionLabel` | A rule and a quiet word starting a group of rows. |
| `collapsible-header` | `CollapsibleHeader` | A styled `CollapsibleTrigger`: `nav` (a group in a list of rows) or `section` (a disclosure). |
| `chip` | `Chip`, `TonePill`, `TagPill`, `TONE` | Round chips by tone (`ok`, `warn`, `bad`, `muted`), removable with `onRemove`. |
| `choice-card` | `ChoiceCard` | A selectable option card (`aria-pressed`), amber when picked. |
| `text-link` | `TextLink` | Amber or muted text link; `asChild` for router links. |
| `segmented-control` | `SegmentedControl` | The Work / Dev mode switch, for any two or three peers. |
| `toggle`, `toggle-group` | `FilterPill`, `ViewSwitch`, `useRememberedView` | A list filter pill; a tab's view switch. |
| `pill-tabs` | `PillTabs` | A page's section tabs as a bare strip (`tabs` draws the same over Radix). |
| `empty-state` | `EmptyState`, `EmptyStateLogo` | The brand badge, a heading, copy and actions for an empty list. |
| `stat-card` | `StatCard`, `InfoHint`, `ChannelChip` | A stat tile: label, value, hint, detail. |
| `control-styles` | `CONTROL_*` | Class strings for native controls that cannot be a component. |
| `command` | `Command`, `CommandList`, `CommandGroup`, `CommandItem`, … | A searchable list (cmdk) with Work's menu rows; `data-checked` on an item lights its amber check. |
| `hover-card` | `HoverCard`, `HoverCardTrigger`, `HoverCardContent` | A card shown on hover, on the popover surface. |
| `item` | `Item`, `ItemMedia`, `ItemContent`, `ItemTitle`, `ItemDescription`, `ItemActions`, … | A content row (media, title, description, actions), plain, outlined or muted. |
| `button-group` | `ButtonGroup`, `ButtonGroupText`, `ButtonGroupSeparator` | Buttons joined into one control, rounded only at its ends. |
| `progress-bar` | `ProgressBar` | A 4px bar on the line colour, filled with `color` (amber by default). |
| `responsive-panel` | `ResponsivePanel` | A popover with a cursor, a bottom sheet on a phone. |
| `timeline` | `Timeline`, `TimelineItem`, `TimelineIndicator`, `TimelineSeparator`, … | A vertical run of steps, each a dot on a connecting line. |
| `rabbit-mark` | `RabbitMark`, `RABBIT_MARK_PATH`, `RABBIT_MARK_BOX` | The rabbit silhouette as an icon in `currentColor`, and its path for a canvas. |

Several rely on the `data-*` variants and the `no-scrollbar` utility from
`shadcn/tailwind.css`; import it alongside the theme.
