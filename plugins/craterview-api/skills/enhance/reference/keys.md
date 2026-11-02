# Getting and managing an API key

When `CV_API_KEY` is not set, walk the user through getting one:

1. **Sign in at https://craterview.ai.** The workspace hands out a key without signing in,
   but that account exists only in that browser's cookie — signing in first is what makes
   the key belong to an account they can come back to, manage, and pay into.
2. **Open the dashboard, https://craterview.ai/dashboard, and the _Developer API_ panel.**
   It is closed until its heading is clicked.
3. **Name the key for what will use it, press _Get a key_, then _Copy_.** The key is shown
   only then. If it is lost, they get another and revoke the lost one.
4. **Ask them to export it in the shell they will run the code from** — not to paste it
   here:

   ```bash
   export CV_API_KEY=cv_...
   ```

   Offer to add that line to their shell profile if they want it to persist. If the key
   should live with a project instead, `.env` beside the code is the usual place — confirm
   the file is ignored by version control before it goes in.

**Every key on the account is listed on the account page, https://craterview.ai/account,
under _API keys_, and revoking one there stops it at once.** Each browser signed in, each device
with the app, each connected assistant and each key made for code has a key of its own, named
for what holds it, so revoking one stops that one and nothing else. To replace a key without
downtime, get a new one, move the code onto it, then revoke the old one. From code,
`cv.keys()` lists the same keys, `cv.create_key("staging")` makes one, and
`cv.revoke_key(...)` ends one.
