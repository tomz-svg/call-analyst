# Call Analyst

Fathom webhook -> Claude -> Slack recap, money-signal alerts, and action-item chasing.

## Setup
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in, then `export $(cat .env | xargs)`
3. Slack app (api.slack.com/apps) bot scopes: `chat:write`, `users:read`, `users:read.email`, `im:write`
   (add `im:history` + event `message.im` pointing at `/slack/events` if you want `done` replies to close items).
   Install it and invite the bot to your sales channel.
4. Run: `gunicorn -w 1 -b 0.0.0.0:3000 app:app`  (keep 1 worker; SQLite + background threads)
5. Expose it publicly (Render, Fly, Railway, or `ngrok http 3000` for testing).
6. In Fathom, add a webhook for new meeting content pointing to `https://YOUR-HOST/fathom`.
   Enable transcript + summary in the payload, and paste the signing secret into `FATHOM_WEBHOOK_SECRET`.
7. Chase open items daily with cron:
   `0 9 * * 1-5 curl -X POST -H "X-Chase-Token: $CHASE_TOKEN" https://YOUR-HOST/chase`

## Test without Fathom
curl -X POST localhost:3000/fathom -H 'Content-Type: application/json' -d '{
  "title":"Acme Corp Demo","share_url":"https://fathom.video/share/test1",
  "calendar_invitees":[{"name":"Sarah L.","email":"you@yourco.com"}],
  "transcript":"Sarah: Thanks for joining. John: Our worry is integrating with our legacy Salesforce setup. Sarah: I will send the technical overview deck by Friday. John: If it works we would roll out to two more teams."}'
(Unset FATHOM_WEBHOOK_SECRET locally so signature checking is skipped.)
