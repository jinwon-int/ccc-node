- **Matrix: a refused attachment is no longer dropped without a word (#2159).**
  A client that sends media inside an encrypted room as a plaintext `url`
  (no `content.file`, e.g. family-chat web before family-messenger#308) made
  matrix-nio return a `BadEvent` — its decrypted `m.file`/`m.image` schema
  requires `file` — and the transport discarded it with no job, no meta and no
  notice; the owner had to ask whether the file arrived. A decrypted media
  `BadEvent` is now routed through `_admit_media()` and recorded body-free as
  `plaintext-attachment-refused` or `malformed-attachment`. A verified, trusted
  allowed sender gets one `📎 첨부를 읽지 못했습니다…` notice per refused event
  (direct rooms, or family rooms that address the bot; 24 h window). The #1795
  policy is unchanged: plaintext or malformed attachments are never run.
