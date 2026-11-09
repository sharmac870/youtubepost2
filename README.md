# youtubeposts2

India trending topics → original 30–58 second videos → **YouTube Shorts** ([@chansharma870](https://www.youtube.com/@chansharma870)) and **Instagram Reels** (@Sharmac_870), with a review console and a tracking dashboard.

Built from *India Trending Video Automation LLD v2.0* (in `docs/`), with one overriding requirement from the owner: **keep the running cost as low as possible.** Every place this build differs from the LLD is a cost decision, listed under [Cost decisions](#cost-decisions) with what it trades away.

```
 every 6 h          q-script             (you approve)       q-render            q-publish-youtube
Discovery ───────▶ Script + policy ───▶ Review console ───▶ Render + QA ───┬──▶ YouTube publisher
YouTube mostPopular  Claude (no tools)   approve / edit /    TTS + Pillow +   │   resumable upload, ref tag,
regionCode=IN        deny-list rules +   reject, kill switch ffmpeg, ffprobe  │   reconcile before retry
velocity score,      LLM moderation                          checks           └──▶ Instagram publisher
7-day topic dedupe                                                             q-publish-instagram
                                                                               REELS container, resumable
 State: Azure Table Storage (ETag concurrency + transactional outbox) · Media: private Blob · Secrets: Key Vault
```

## What it does

1. **Discovery** (Container Apps cron job, 06:00 / 12:00 / 18:00 / 00:00 IST): calls the official YouTube Data API `videos.list?chart=mostPopular&regionCode=IN` once per allow-listed category (Sports, Gaming, Entertainment, News, How-to, Science & Tech; never Music), scores by views per hour, drops deny-listed topics and anything covered in the last 7 days, and keeps the top 2. Recent Google News headlines on the topic are attached as citable sources. A run key (`2026-11-07-slot2`) makes double triggers harmless. Scheduled discovery is off by default (on-demand only: **Find trends now** or **Create from theme** in the console); deploy with `DISCOVERY_SCHEDULE=true` to turn the 4-times-a-day schedule back on.
   One of the two slots (`NEWS_TOPICS_PER_RUN`, 0 turns it off) goes to a **news-led story** instead, using the story picker from the earlier YoutubePosts project: it walks a niche hierarchy (cybersecurity, tech & AI, business, breaking; `NEWS_NICHES`), groups Google News and publisher-feed headlines into events with rare-word weighting, takes the first niche whose best story is covered by enough distinct outlets, and reads a capped excerpt of a directly linked article for facts. Only public https hosts are fetched (no IP literals, internal names or redirects to them).
2. **Script** (event-driven worker): Claude writes an **original** commentary script as strict JSON (hook, 3–6 scenes, call to action, title, description, Instagram caption, hashtags, sources). Trending titles are treated as untrusted data inside delimited blocks; the model has no tools; output is validated and regenerated at most twice.
3. **Policy gate**: deny-list rules (elections, communal, tragedies, crime, medical/financial advice, minors), URL and personal-data checks, unsourced claims, then a separate moderation call. Outcomes: **REJECT**, **REVIEW** (the default) or **APPROVE** (only for categories you switch to auto-approve later). Any error means REVIEW.
4. **Review console**: you approve, edit (saved as a new version) or reject. Drafts expire after 6 hours (trends go stale); expired drafts can still be approved.
5. **Render** (event-driven job, 2 vCPU): the script is written shot by shot (each spoken sentence with the one picture that shows it), each sentence is voiced separately so its picture cuts in exactly when it starts, and candidate stock clips (Pexels/Pixabay, free licence) are looked at by Claude and used only when they clearly fit the sentence (about 1–2 cents a video, counted in the spend cap); with `fal-api-key` the hook and every sentence no stock clip fits get a short **AI video clip** generated on demand (fal.ai image-to-video, default Kling 2.5 Turbo Pro at about $0.35 per 5 s clip: a Flux keyframe of the sentence's picture is animated; nothing runs or costs anything between videos), capped at `AI_CLIP_MAX_USD_PER_VIDEO` ($1.20) per video and `AI_CLIP_DAILY_USD` ($6) per day, with `AI_CLIPS=off|hybrid|full` and `VIDEO_MODEL` to switch; over a cap, or without the key, an AI image (about $0.003) or the caption card is shown instead of an unrelated clip; a cartoon host lip-syncs to the voice in the hook and outro and reacts from the corner during scenes; captions pop word by word; the neural voice (edge-tts, free) changes pace, pitch and pauses with each line's tone; colours and music vary per video; an "AI-generated" label shows for the first 3 seconds; loudness normalised to −14 LUFS. Stock clips need a free `pexels-api-key` (and optionally `pixabay-api-key`) in Key Vault (`infra/deploy.sh secrets`); without one the same style renders on a drawn background. `RENDER_STYLE=classic` brings back the original text-card videos, `MASCOT=false` removes the host. Try it locally with `PEXELS_API_KEY=… ANTHROPIC_API_KEY=… python -m ytp render-sample`. **Media QA** checks 1080×1920, H.264/AAC, 23–60 fps, duration, size, loudness and black frames before anything can publish.
6. **Publish**: YouTube and Instagram on separate queues, so one platform's outage never blocks the other. YouTube: resumable upload with the session URI saved first, `containsSyntheticMedia=true`, `selfDeclaredMadeForKids=false`, privacy from settings (private at first), a `ref-<id>` tag used to reconcile. Instagram: REELS container with resumable upload, container id saved first, status checked before any retry, publishing-limit check. Both add an AI disclosure line.
7. **Sweeper** (every 30 min): relays any outbox message that was committed but not sent, expires stale drafts, refreshes views/likes/comments for the dashboard.

### The console

- **Dashboard**: posts published (per platform), views, drafts awaiting approval, in-progress and needs-attention counts; posts per day chart (with a table view); published by category; top posts; live pipeline state. Below that, **channel analytics** for every upload on @chansharma870 (subscribers, uploads, Shorts vs videos, views, likes, most viewed), refreshed by the sweeper every 3 hours with the read-only API key (about 5 quota units). 7/30/90-day ranges.
- **Review**: each draft with its signal (trending video, news story or your theme), why it needs review, the full script and sources; approve / edit / reject. **Create from theme** (from the YoutubePosts Studio) queues a one-off draft about a theme you type: the worker finds recent coverage, then it goes through the same script, policy, approval and publishing steps.
- **All posts**: search and filter every item; open one for the video player, publications with links and metrics, script, policy decision, QA report and full audit history; retry a failed platform; unpublish.
- **Health & settings**: YouTube quota today, Claude spend today, platform sign-in status, queues and dead-letters (with replay), discovery runs, kill switch, YouTube privacy for new uploads, auto-approve categories.

Sign-in is Microsoft Entra ID (Container Apps built-in auth) plus an allow-list of user object ids. The console has **no access to platform tokens**: it only changes state and queues work.

## Guarantees (LLD Sections 5.3, 5.4, 9)

- **No duplicate posts.** Every stage checks the item's state before acting, so redelivered messages are dropped. Uploads write an attempt row before any side effect and save the platform's identifiers the moment they exist. `videos.insert` and `media_publish` are never retried without reconciliation. Tests kill the publisher right after the platform accepted the post and prove exactly one post exists after recovery (`tests/test_pipeline.py`).
- **Fail closed.** Kill switch on, credential revoked, QA not passed, quota near the limit: nothing publishes; messages wait and resume by themselves.
- **Optimistic concurrency**: state changes use ETags; a stale writer loses and drops its message.
- **Transactional outbox**: a state change, its audit event and its next queue message commit in one Table Storage transaction (all rows of an item share one partition).
- **Error taxonomy**: transient errors retry with backoff and Retry-After; quota and publishing limits defer until reset; auth failures mark the credential REVOKED, stop that platform and show a banner; after 5 deliveries a message is dead-lettered and can be replayed from the console.
- **Secrets**: only in Key Vault, read by the worker identity; access tokens live in memory only; logs pass through a redaction filter with a test proving tokens never reach output.

## Cost decisions

At about 5 videos a day the whole system costs **about $5–6 a month**, almost all of it the container registry. Replace the registry with GitHub Container Registry and it drops to **about $1 a month** plus Claude usage.

| LLD v2 component | Typical cost | This build | Cost | What you give up |
|---|---|---|---|---|
| PostgreSQL Flexible (B2s) | ~$25+/mo | Azure Table Storage | cents | No SQL joins (the dashboard aggregates in code; fine for thousands of items) |
| Service Bus Premium (or Standard) | ~$670 (or ~$10)/mo | Storage Queues + `-poison` queues | cents | No broker-side duplicate detection (state checks do it); 64 KB messages |
| Front Door Premium + WAF | ~$330/mo | Container Apps ingress + Entra sign-in + strict CSP/CSRF | $0 | No WAF; the console is small, signed-in only, and holds no tokens |
| NAT Gateway (static egress IP) | ~$32/mo | Default egress | $0 | No fixed IP for vendor allow-lists (none of the vendors require one) |
| Private endpoints + VNet | ~$7 each/mo | Public endpoints with Entra-only auth (storage keys disabled) | $0 | Data planes are internet-reachable but accept only managed identities |
| OpenShorts container (4 vCPU/8 GiB) | always-on or cold starts | In-process Pillow + ffmpeg renderer in a scale-to-zero job | ~$0 (free grant) | Stock clips are generic (no footage of specific events or people); cartoon host, not a human avatar |
| ElevenLabs voice | paid plan | edge-tts neural voices (en-IN) | $0 | Fewer voices; no custom voice |
| Container Registry Premium | ~$50/mo | Basic | ~$5/mo | No geo-replication or content trust |
| App Insights / OTel traces | per GB | JSON logs to Log Analytics, 0.2 GB/day cap, item id in every line | $0 (free 5 GB) | No distributed trace view; the console's audit history covers the per-item story |
| Claude Sonnet | ~$0.02/script | Claude Haiku by default (`ANTHROPIC_MODEL`) | ~$0.003/script | Slightly plainer writing; switch with one setting |
| Alert rules | ~$1.50 each/mo | Console banners (revoked credential, dead letters, kill switch) | $0 | No e-mail/Teams alerts (add Azure Monitor rules if wanted) |

Compute: four Container Apps jobs and a console that scales to zero use the monthly free grant (180,000 vCPU-seconds, 360,000 GiB-seconds). Rendering 5 videos a day at 2 vCPU for ~4 minutes each is about 70,000 vCPU-seconds a month.

Running cost levers, all settings: `TOP_N_TOPICS` (drafts per run), the discovery schedule, `ANTHROPIC_MODEL`, `DAILY_SPEND_CAP_INR` (default ₹150/day; the pipeline stops writing scripts when reached).

## Deploy to Azure

Resources go in subscription `8117e214-4622-48b8-85b9-a6feb362c220`, resource group `youtubeposts2`.

```bash
az login                      # as the subscription owner (sharmac_870@outlook.com)
infra/deploy.sh all           # base resources, sign-in app, image build (in Azure), jobs and console
infra/deploy.sh secrets       # Anthropic key, YouTube API key, Instagram token (prompts, nothing echoed)
pip install -r requirements.txt
KEY_VAULT_URI=https://<vault>.vault.azure.net/ PYTHONPATH=src python -m ytp bootstrap-youtube client_secret.json
```

`deploy.sh all` prints the console URL. Later code changes: `infra/deploy.sh apps-with-new-image`.

## Before go-live (needs you)

These are outside the code and block public publishing (LLD Section 2, critical note):

1. **Google**: in the Google Cloud project, enable YouTube Data API v3, create an OAuth client (Desktop app), set the consent screen to **In production** (in *Testing* the refresh token dies after 7 days), and submit the **YouTube API Services compliance audit**. Until the audit passes, every API upload is locked to private, which is why `YT_PRIVACY_STATUS` starts as `private`.
2. **Instagram**: make @Sharmac_870 a Professional (Business or Creator) account, create a Meta app with **Instagram API with Instagram Login**, add the account as a tester/role-holder, and generate a long-lived token with `instagram_business_basic` and `instagram_business_content_publish`. Long-lived tokens last 60 days: refresh them (runbook R2). If you prefer Facebook Login with a linked Page, set `IG_API_HOST=graph.facebook.com`.
3. **Anthropic**: an API key with a small prepaid balance; $5 lasts months at Haiku prices.
4. **Policy sign-off**: read Section 11 of the LLD and the deny-list in `src/ytp/common/config.py`; adjust before go-live.
5. **Path to auto-approval** (LLD 11.3): weeks 1–2 approve everything and keep YouTube private; weeks 3–4 approve everything and switch privacy to public in the console; after ~100 reviewed items, enable auto-approve for low-risk categories.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python -m pytest                                   # 33 offline tests, no keys needed
export PYTHONPATH=src BACKEND=local CONSOLE_DEV_AUTH=true
python -m ytp demo --runs 4                         # runs the real pipeline against fakes
python -m ytp serve --port 8000                     # console at http://127.0.0.1:8000
```

With real keys in the environment (`ANTHROPIC_API_KEY`, `YOUTUBE_API_KEY`), `python -m ytp discover` then `python -m ytp work` runs real discovery and scripting locally; leave the YouTube/Instagram tokens unset and nothing can publish.

## Runbooks (LLD Section 15)

| ID | Situation | What you see | What to do |
|---|---|---|---|
| R1 | YouTube or Instagram sign-in revoked | Red banner; Health shows "Failed: publishing stopped" | Check the account's security. YouTube: run `bootstrap-youtube` again. Instagram: generate a new long-lived token and `deploy.sh secrets`. Then press "I've fixed it" in Health. |
| R2 | Instagram token near expiry (60 days) | Calendar reminder | `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token` with the current token, store the new one with `deploy.sh secrets`. |
| R3 | Dead-lettered messages | Banner; Health → Queues | Open the item to read its last error, fix the cause, press Replay. Reconciliation runs before any publish retry. |
| R4 | YouTube quota used up | Items wait in Publishing; note "waiting: YouTube quota" | Nothing; uploads resume after 13:30 IST. If it happens daily, lower `TOP_N_TOPICS` or apply for a quota extension. |
| R5 | Duplicate or wrong post | You notice it | Kill switch on (Health). Unpublish from the item page. Read its attempts and history. Add a regression test before switching the kill switch off. |
| R6 | Platform warning or takedown | E-mail from the platform | Kill switch on; clear auto-approve categories; review recent items; extend the deny-list. |
| R7 | Suspected secret leak | Secret scanner, unusual Key Vault access | Revoke and rotate every affected secret (Google grant, Meta token and app secret, Anthropic key); review Key Vault logs. |

## Moving over from YoutubePosts

This project replaces the earlier YoutubePosts repo (GitHub Actions + Azure Functions Studio). Its renderer, voice, story picker, theme flow and channel dashboard now live here. To switch over:

1. Reuse the same Google Cloud project: put its API key, OAuth client id/secret and refresh token into Key Vault (`infra/deploy.sh secrets`, then `KEY_VAULT_URI=… python -m ytp set-secret yt-client-id` (reads the value from stdin) for the client id, secret and refresh token, or run `bootstrap-youtube` with the same `client_secret.json`). GitHub Actions secrets can't be read back, so use your local copies.
2. Once the first draft here publishes, disable the old repo's workflows (Actions → each workflow → Disable) and delete its Azure Functions app. Running both would share the 10,000-unit YouTube quota (about 6 uploads a day) and could cover the same story twice.

## Layout

```
src/ytp/
  common/      config, Table/SQLite store, queues, media, Key Vault secrets, redacting logs, errors, models, repo
  discovery/   TrendDiscoveryService (YouTube mostPopular IN, scoring, fingerprints, Google News context),
               stories.py (news-led niche hierarchy and headline clustering; Create from theme)
  analytics/   channel.py (whole-channel stats for the dashboard)
  scripting/   ScriptGenEngine (Claude, strict JSON, untrusted-data blocks, spend cap)
  policy/      ContentPolicyGate (rules + moderation, fail closed)
  render/      engine (TTS, frames, ffmpeg) and qa (spec checks)
  publish/     youtube (resumable protocol, reconcile) and instagram (REELS container, resumable)
  console/     FastAPI app and the static dashboard/review UI
  pipeline.py  stage handlers, operator actions, worker loop, sweeper
  fakes.py     fakes for every external service (tests and `ytp demo`)
infra/         main.bicep (all resources), deploy.sh
tests/         45 tests incl. the mandatory exactly-once tests and the log-redaction test
```
