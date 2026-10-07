- **timed-test-scan no longer ignores an early verdict that restates the
  deadline it beat (#2149).** `booked_at` (the start of the early-verdict
  window, #2043) now counts only hits that can be a booking: mentions demoted
  as `already-settled`, `already-settled-comment` or `past-at-posting` are
  records of a deadline, not new bookings. A verdict comment such as
  "예약 종료 시각(2026-10-08 09:00 KST)보다 앞당겨 끝냈다" (a2a-nexus#2315)
  used to move the window onto itself and come back as `expired-unjudged`
  at high confidence.
