# Download MLS picks

On a public gallery, a real estate agent can star the photos they want and download those photos together as MLS-ready JPEGs. Stars belong to the gallery and are shared by its visitors.

## Gallery behavior

- Show **Download MLS picks** in the existing picks section when the gallery has at least one ready, starred photo and downloads are unlocked.
- Use the current gallery even when several galleries belong to the same listing. Include one JPEG per ready, starred photo. Videos and unstarred photos are ineligible. Each member has a recognizable, unique filename.
- Use the studio’s active MLS Standard preset (`mls-3x2`). Its default is 2048 × 1365 pixels. Honor its configured dimensions and crop settings.
- Each new download reflects the current stars. An empty selection, an unavailable preset, or a photo that cannot be prepared gets an explanatory response. Offer a retry when preparation is in progress. A successful ZIP contains the complete eligible selection.
- Apply the same studio, publication, expiry, PIN and invoice checks as Download all on every request. Existing originals and whole-listing exports retain their behavior.

## Acceptance

1. Put three distinguishable synthetic photos with different source dimensions in one gallery. Star two through the gallery controls, download the ZIP, and open every member. Verify the identities of the two selected photos, JPEG format and exact preset dimensions.
2. Exchange one star for the third photo and download again. Verify the new members and dimensions. Repeat with a nondefault MLS size to catch hardcoded output.
3. Check empty picks, unavailable output, another gallery’s stars, and the existing access restrictions using the public route. Use local fixtures for all tests.
