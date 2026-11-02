# LoseSound interface design

LoseSound is used standing next to a speaker. The product is not the tables behind it;
it is six buttons. The interface is therefore built as the speaker's front panel:
a backlit display strip over a deck of six physical-feeling preset keys. Everything
else — the station library, the catalogue, diagnostics — is a service panel below it.

## The one bold thing

The preset deck. Six raised keys, numbered like the hardware, with the station
artwork, a short mode readout, and an amber signal lamp when the button is assigned.
The key that matches what the speaker is playing lights brightest.

Everything around the deck is flat and quiet: no card grid, no gradient washes, no
soft grey shadows, no pill badges on every row. Sections are separated by space and
by functional row rules, not by boxes.

## Colour

| Token | Value | Role |
| --- | --- | --- |
| `--bg` | `#121416` | page |
| `--panel` | `#191c1f` | panel and key surfaces |
| `--well` | `#0d0f10` | recessed display and inputs |
| `--line` | `#2b3035` | borders and row rules |
| `--ink` | `#edeae3` | primary text, warm white |
| `--dim` | `#a2a7ad` | secondary text |
| `--amber` | `#e8a33d` | signal lamp, now playing, focus |
| `--green` | `#6dbf8f` | healthy |
| `--red` | `#e2675b` | errors |

Amber is the tuning-lamp colour of a radio, not a generic "accent": it marks what is
engaged. Green and red carry state only. There is no second accent colour.

## Type

One bundled family, **Archivo** variable (SIL OFL, `static/archivo-var.woff2`), so the
UI works on a LAN with no internet.

- UI text: 15px, weight 400–600, width 100.
- Labels and mode readouts: 11–12px, weight 600, width 90, sentence case, small tracking.
- Preset numbers and display numerals: width 70–75, weight 700, tabular figures.
- Machine strings (URLs, IPs, firmware): system monospace, and only those.

Sentence case everywhere; no all-caps eyebrows. Numbers appear only where they are
the product's own numbering (preset slots 1–6), never as decorative `01 / 02 / 03`.

## Layout

```
┌──────────────────────────────────────────────────────────────┐
│ ▮▮▮▮▮▮ LoseSound                    ● ready   DE   ⚙        │
├──────────────────────────────────────────────────────────────┤
│ [ Presets ] [ Find stations ] [ Status ]                    │
│ ┌ display ─────────────────────────────────────────────────┐ │
│ │ Now playing                                              │ │
│ │ HR3                                  SoundTouch 20       │ │
│ │ Playing · ORION                      10.10.10.26         │ │
│ └──────────────────────────────────────────────────────────┘ │
│ ┌ 1 ──────┐ ┌ 2 ──────┐ ┌ 3 ──────┐ …  six keys            │
│ │ art HR3 │ │ art FFH │ │ art ... │   press = play         │
│ │ Direct  │ │ Direct  │ │ Direct  │   Edit = open drawer   │
│ └─────────┘ └─────────┘ └─────────┘                        │
│ editor drawer (station, mode, set / check / remove)          │
│ Stations                                     Add a station   │
│ rows: artwork · name · URL · presets · check · actions       │
└──────────────────────────────────────────────────────────────┘
```

- Deck: six columns on desktop, three on tablet, two on phones.
- Pressing a key plays that preset, like the physical button. The pencil opens the editor under
  the deck; empty keys open it directly.
- The display strip stays visible on every tab, so you can see what is playing while searching
  the catalogue.
- Row tables stack into labelled blocks on phones, so actions never sit behind a sideways scroll.
- Content is left aligned in one column, max 1160px.

## Motion

One power-on moment: the deck fades up in a short stagger on first load. After that,
motion only answers an action — a key depresses, the editor slides open. All of it is
disabled under `prefers-reduced-motion`.

## Writing

Short, plain sentences. Actions keep their name through the flow (`Set preset` →
"Preset 1 is set to HR3"). Errors say what failed and what to do. Empty states invite
the next step ("No stations yet" with a link to the catalogue) rather than explaining
that something is empty.
