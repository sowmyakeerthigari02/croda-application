# ApplyPilot

Paste a job URL, pick a resume, click **Start Application**. A server browser fills the form from
your saved profile, asks you only what it cannot determine, waits for your yes before submitting,
and moves the application to **Applied** only after the employer's page confirms it.

## How it works

| Stage | What happens |
| --- | --- |
| Analyze | Opens the posting and extracts company, title, location, skills and salary. |
| Sign-in | Uses a saved browser session, then the password vault (one automatic try, HTTPS and the exact site only). With no saved account it asks once: create one, or sign in yourself. Turning on automatic accounts skips that question and accepts each site's terms on your behalf, listing them in the log. New accounts get a unique generated password, saved encrypted. |
| Emailed code | With an email connected, reads the code from that site's own email and enters it, at most twice. Otherwise pauses for you. |
| Email verification link | After creating an account, with an email connected, opens the one verification link in that site's own email (only links on the site's own domain, never unsubscribe or tracking links), then goes back to the job and signs in with the saved password. Otherwise pauses for you. |
| CAPTCHA, text message code or authenticator code | Pauses for you in the dashboard's live browser. |
| Fill | Uses Build Your Profile, then earlier answers, then Claude for experience questions the resume supports. |
| Unknown required answer | Pauses with numbered questions and their options. Reply with buttons, or one line: `1. No, 2. Leave blank`. |
| Employer rejects a step | Asks about the exact field it rejected. |
| Final step | Pauses for **Yes, submit / No, hold**, showing the declaration and the boxes left unchecked. |
| Confirmation | Only the employer's confirmation page moves it to **Applied**. |

Cover letters: created from the resume and profile and attached only when the posting or the form
makes one mandatory. Otherwise that box stays empty.

Answer rules: salary follows a posted range (its top minus 5,000, staying inside the range), or your
profile figure if none is posted (digits only),
notice is "2 weeks" or a date counting today as day 1 with weekends moved to Monday, and optional
fields and opt-in boxes stay empty unless you answered them.

## Run it locally

```bash
python -m venv .venv
```
```bash
.venv/Scripts/python -m pip install -r requirements.txt pytest httpx
```
```bash
.venv/Scripts/python -m playwright install chromium
```
```bash
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000. Tests: `.venv/Scripts/python -m pytest tests`.

## Deploy to DigitalOcean

**Supabase holds the data** (Postgres for records, a private Storage bucket for resumes and
screenshots). **A Droplet runs the app and its browsers**, because Chromium needs a long-running
server that Supabase does not provide. The Droplet keeps no data of its own, so it can be replaced
at any time by deploying again with the same `.env`.

1. Create a Supabase project and note its database password.
2. Create an Ubuntu 24.04 Droplet with at least **4 GB RAM** and add your SSH key.
3. Point a domain's A record at the Droplet's IP address.
4. Copy this folder to the Droplet, then run the setup script from inside it. The first run creates
   `.env` and stops so you can add the Supabase values:
   ```bash
   scp -r . root@YOUR_DROPLET_IP:/opt/applypilot
   ```
   ```bash
   ssh root@YOUR_DROPLET_IP "cd /opt/applypilot && bash deploy/setup-droplet.sh your-domain.com"
   ```
5. Fill in `DATABASE_URL`, `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in `/opt/applypilot/.env`,
   optionally `ANTHROPIC_API_KEY` too, then run the setup script again. Tables and the bucket are
   created on first start. Your dashboard password is `APP_PASSWORD` in the same file.

Keep `SESSION_KEY` and `VAULT_KEY` safe and back them up: Supabase stores saved sign-ins and employer
account passwords encrypted with them, and without `VAULT_KEY` those passwords cannot be recovered. Row level security is
turned on for every table with no policies, so Supabase's public API cannot read them. Other
applicant data, including answers and resumes, is readable by anyone with access to your Supabase
project, so limit who is on it.

## Limits to know about

- **LinkedIn's terms restrict automation**, and it can challenge or restrict accounts, especially
  logins from data-center IP addresses like a Droplet's. There is no attempt to hide the browser or
  get past those checks. When a posting has a company career page, use that link.
- **Employer portals vary.** Standard HTML forms are covered. Heavily custom widgets may pause for
  you to finish in the live browser.
- **Connected email** works with Gmail, Yahoo and iCloud app passwords over IMAP. An app password can
  read the whole mailbox even though this app only opens mail from the site asking for a code, so a
  separate job-search mailbox is safer. Codes and verification links from a sender on a different
  domain than the site, such as an employer's portal mailing from its own address, still go to you,
  as do links wrapped in an email service's click-tracking redirect.
- **Not built yet, as agreed in the chat:** SMS and email notifications, per-user platform login
  (one shared password protects the dashboard).
- **Vault passwords cannot be read back.** To log in to an employer site by hand, use that site's
  "forgot password", then save the new password under Saved Site Accounts.
- A paused application frees its slot for the next one but keeps its page open, using some memory,
  for up to `PAUSE_TIMEOUT_SECONDS`. After that its answers are kept and replying fills the form
  again from the job link.
