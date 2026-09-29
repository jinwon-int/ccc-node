- **auto-distill: the entail rubric now defines "핵심" (#1846).**
  `ENTAIL_PROMPT` asked whether a quote supports "주장의 핵심 내용" without
  saying what the core is, so haiku read it as "every component must be
  quoted" while another lane read it as "the central claim is quoted" — the
  same session could produce a different AUTO.md depending on the judging
  lane. The prompt gains the one paragraph converged on in #1846: the core
  being quoted is `yes`; missing incidental detail (date, file name, line
  number, time) is still `yes`; an asserted result/state/completion absent
  from the quote is `no` (goal restated as done, existence restated as
  executed). The 3-branch core/full gate was measured and rejected, so the
  verdict stays binary and the output format and parsing are unchanged.
  `auto-distill.py`'s full SHA changes (canon `surface_sha256` does not), so
  the exact-source evaluation receipt must be re-issued before this deploys.
