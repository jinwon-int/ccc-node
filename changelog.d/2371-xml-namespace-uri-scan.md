- **skill-promotion: `possible-token` no longer fires on public XML namespace
  URIs (#2371).** The generic pattern `[A-Za-z0-9+/]{40,}` allows `/`, so the
  OOXML relationship-type URIs every docx/xlsx/pptx skill must carry
  (`http://schemas.openxmlformats.org/officeDocument/2006/relationships/…`)
  read as a token: fleet-skills#484's correct revision was rejected as
  `secret_possible-token` and hit the revise cap. `_scan_text` now masks URIs on
  `schemas.openxmlformats.org`, `schemas.microsoft.com`, `www.w3.org` and
  `purl.org` before that one pattern runs, and only when every path segment is
  under 40 characters — a long run under those hosts, a look-alike host, or any
  other URL path still fails. The other credential patterns see the raw text.
