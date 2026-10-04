# media

The brand mark, in the forms other things can consume. Everything here is one drawing: two columns
of schema rows, a light-steel rule between them for the comparison, and one row that is light steel and short —
the drift the tool exists to find.

| File | Use it for |
|------|------------|
| `logo.svg` | The lockup — mark plus wordmark — on a light ground. Docs, slides, the website, a README header. |
| `logo-inverse.svg` | The same lockup on steel or any dark ground. The mark loses its own ground there, so the rows carry the shape. |
| `logo-mark.svg` | The mark alone, square. Avatars, favicons, anywhere the wordmark will not fit. |
| `logo-mark-mono.svg` | The mark in one ink, rows knocked out of the ground. Single-colour print, stencils, a stamp. Recolour it by changing the single `fill`. |
| `logo-mark-<n>.png` | The mark where an SVG is not accepted: a Windows or Linux launcher entry, a favicon, a store listing, a Slack or Confluence avatar. |

The macOS application icon is not here — it is `packaging/db-schema-diff-gui.icns`, built from the
same geometry, because that is where `make exe` looks for it.

## Changing the brand

Nothing here is drawn by hand twice. The geometry and the palette live in `packaging/logo.py`,
which takes its colours from `src/db_schema_comparer/gui/ui/tokens.slint` and nowhere else. To
change the mark:

1. Edit `packaging/logo.py` (or the tokens, if it is a colour).
2. Bring the same numbers into the `.svg` files here. They are plain `<rect>`s with the generator's
   coordinates on a 1024 grid, so this is a search and replace, and `tests/unit/test_logo.py` fails
   until the two agree.
3. Regenerate:

   ```
   python3 packaging/make_logo.py   # the PNGs here
   python3 packaging/make_icon.py   # the macOS .icns (macOS only)
   ```

Every PNG is rendered from the grid at its own size rather than resampled from the 1024, so the
16px and 32px files are anti-aliased for the size they are actually shown at.
