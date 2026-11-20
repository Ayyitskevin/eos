# Download all gallery originals

**Download all** delivers one file per ready gallery asset. Each file must
extract under its own name and contain the exact bytes of the stored original.

Keep the photographer’s filename when possible. When names collide, the
lowest asset ID keeps the spelling it was uploaded with. Give later copies a
suffix before the extension. Reserve the other original names first, even
when they already look like generated names. Treat case variants and
canonically equivalent Unicode spellings as collisions, so the ZIP also
extracts on case-insensitive filesystems. Keep names within 255 UTF-8 bytes.
If shortening a long name would take another photo's original name, give the
shortened name a suffix. Use the same names on rebuild.

A gallery with an archive from the previous naming scheme uses the existing
preparation screen to rebuild it on the next **Download all** request.

## Acceptance

1. Publish three different photos named `Kitchen.jpg`, `Kitchen_3.jpg`, and
   `Kitchen.jpg`, with the last photo assigned asset ID 3.
2. Enter the public gallery PIN, choose **Download all**, wait for preparation,
   and extract the downloaded ZIP into an empty directory.
3. Verify three distinct filenames. The first two keep their original names.
   Compare each extracted file byte for byte with the corresponding upload.
4. Repeat with an old archive already cached at the current gallery revision.
