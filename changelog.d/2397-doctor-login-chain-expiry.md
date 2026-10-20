- **doctor warns before a `/login` chain ends (#2397).** A refresh login's
  `refreshTokenExpiresAt` is fixed at login (measured: 27-29 days later, not
  extended by rotation). After that point the next refresh is rejected and
  the CLI wipes the login. `claude host credentials` now reads that field. A
  chain that ends within 2 days, or has already ended, is 수동필요, so the
  daily fleet doctor sweep alerts before the outage. Within 7 days it is
  경고. A missing or unusable value keeps the previous 정상 row.
