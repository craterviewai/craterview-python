# Getting and managing an API key

When `CV_API_KEY` is not set, walk the user through getting one:

1. **Sign in at https://craterview.ai.** The workspace hands out a key without signing in,
   but that account exists only in that browser's cookie — signing in first is what makes
   the key belong to an account they can come back to, rotate, and pay into.
2. **Open the dashboard, https://craterview.ai/dashboard, and the _Developer API_ panel.**
   It is closed until its heading is clicked.
3. **Press _Show my API key_, then _Copy_.** This is the same key the web app itself is
   using, so code holding it calls exactly what the page calls.
4. **Ask them to export it in the shell they will run the code from** — not to paste it
   here:

   ```bash
   export CV_API_KEY=cv_...
   ```

   Offer to add that line to their shell profile if they want it to persist. If the key
   should live with a project instead, `.env` beside the code is the usual place — confirm
   the file is ignored by version control before it goes in.

**Rotating replaces the key immediately** — the old one stops at once, and any assistant
connected from that browser's sign-in is disconnected with it. Each sign-in in a browser has a
key of its own, so one connected from another browser keeps working, and so does one connected
from a sign-in that has since ended, until its key is revoked by id: `cv.keys()` lists the
account's keys, a sign-in's named for the browser it was made in, and `cv.revoke_key(...)` ends
one. Tell the user that before they press it.
Several keys on one account is the ordinary arrangement (`cv.create_key("staging")`,
`cv.revoke_key(...)`), and rotating without downtime means creating the new key, moving
clients onto it, then revoking the old.
