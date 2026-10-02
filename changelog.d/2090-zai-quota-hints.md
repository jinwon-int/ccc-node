- Danso bridge ErrorEvents for Z.AI quota exhaustion now append a fixed,
  code-derived `zai_hint=<hint>` to the user-facing line (#2090). Window-reset
  codes (1308, 1310, 1316-1321) point to waiting for the window/cycle reset or
  moving the lane; account-action codes (1113, 1309, 1311, 1313-1315) state
  the required account step and that waiting will not clear it. The raw
  `zai_code` stays for diagnostics, no reset time is claimed, and transient
  429s (1302, 1305, no code) keep the previous rendering.
