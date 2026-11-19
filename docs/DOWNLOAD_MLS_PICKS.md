# Download MLS picks

On a public gallery, a real estate agent can star the photos they want and download those photos together as MLS-ready JPEGs. Stars belong to the gallery and are shared by its visitors.

## Gallery behavior

- Show **Download MLS picks** in the existing picks section when the gallery has at least one ready, starred photo and downloads are unlocked.
- Use the current gallery even when several galleries belong to the same listing. Include one JPEG per ready, starred photo. Videos and unstarred photos are ineligible. Each member has a recognizable, unique filename.
- Use the studio’s active MLS Standard preset (`mls-3x2`). Its default is 2048 × 1365 pixels. Honor its configured dimensions and crop settings.
- Each new download reflects the current stars. An empty selection, an unavailable preset, or a photo that cannot be prepared gets an explanatory response. Offer a retry when preparation is in progress. A successful ZIP contains the complete eligible selection.
- Apply the same studio, publication, expiry, PIN and invoice checks as Download all on every request. Existing originals and whole-listing exports retain their behavior.

## Shared Star and Unstar actions

Each gallery form submits the choice shown on the page. **Star** sets the photo’s shared star and **Unstar** clears it. This applies to photos in room sections and to photos outside a section. Repeating the same choice is safe, even from a page opened before another visitor acted. When visitors make opposite choices, the last saved choice wins. Other photos retain their own selections.

The existing favorite URL accepts a POST with form field `action=star` or `action=unstar`. After a successful save the browser returns to the gallery. A form from an older page can lack the action field. For a missing or unrecognized action, show a refresh instruction and leave the selections intact. The existing gallery access checks apply to every submission.

## Acceptance

1. Put three distinguishable synthetic photos with different source dimensions in one gallery. Star two through the gallery controls, download the ZIP, and open every member. Verify the identities of the two selected photos, JPEG format and exact preset dimensions.
2. Exchange one star for the third photo and download again. Verify the new members and dimensions. Repeat with a nondefault MLS size to catch hardcoded output.
3. Check empty picks, unavailable output, another gallery’s stars, and the existing access restrictions using the public route. Use local fixtures for all tests.
4. Open the gallery in two independent pages, with the porch starred and the kitchen unstarred. Click **Star** on each already-open page and download the MLS ZIP after each click. Each archive must contain exactly one kitchen JPEG and one porch JPEG. Load both pages with **Unstar** visible, click it on each, and download after each click. Both archives must contain exactly one porch JPEG and zero kitchen JPEGs.
