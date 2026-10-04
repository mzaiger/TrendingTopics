# Trending Topics

A small GitHub Pages site that shows what is trending right now from three places. Every topic has
a title, a full description, a picture, a rank, a "trending for" figure, and the date it started
trending.

| Tab | Where the topics come from | Rank means | Volume line | Click goes to |
|---|---|---|---|---|
| **Social Media** | an Apify actor (US top 20) | the platform's own rank (#1 = top trend) | hours trending | a twstalker search for the topic |
| **Google** | Google's public "Daily Search Trends" RSS feed | search volume (most searches = #1) | searches (e.g. `10K+ searches`) | a Google search for the topic |
| **Reddit** | Reddit's public r/popular RSS feed | hours trending (longest = #1) | hours trending | the post on Reddit |

Google and Reddit don't publish a trend rank of their own, so Google is ranked by search volume and
Reddit by hours trending (worked out from `started_trending` in each topic's JSON).

---

## What's in the folder

```
index.html                       the page (reads the three JSON files)
trend_common.py                  shared code: ranking, history, images, Gemini
social_media_trends.py           -> social_media_trends.json
google_trends.py                 -> google_trends.json
reddit_trends.py                 -> reddit_trends.json
requirements.txt                 pip install -r requirements.txt
social_media_trends.json         starter (empty) files so the page loads before the first run
google_trends.json
reddit_trends.json
.github/workflows/
    social-media-trends.yml      one workflow per feed
    google-trends.yml
    reddit-trends.yml
apps-script/trigger.gs           the Google Apps Script that starts the workflows
```

The workflows have **no schedule**. They run when the Apps Script sends a `repository_dispatch`
event (or when you press **Run workflow** in the Actions tab).

---

## How a run works

1. **Get the list**
   - Social media: one Apify call for the US top 20 (paid/promoted trends are dropped).
   - Google: the public RSS feed. Reddit: the public r/popular RSS feed.
2. **Description**
   - Google: Google's own news snippet, or the top news headline. Reddit: the post's own text.
   - Social media has no description of its own, so the script reads recent posts (Sotwe) and the
     last 24 hours of news headlines (Google News RSS) for each topic.
   - **Gemini is only used when a topic has no description.** It is **one call per script per run**
     for all of those topics together (fallback chain `gemini-3.5-flash-lite` ->
     `gemini-3.1-flash-lite` -> `gemini-2.5-flash-lite`). A description from the last run is re-used
     instead of asking again. If Gemini fails, the old description is kept; Google/Reddit fall back
     to a plain line such as "Popular post in r/pics."
3. **History** - the script reads the previous JSON file to work out `first_seen`,
   `started_trending`, the previous rank and how far the rank moved.
4. **Image** - one picture per *new* topic (see "Images" below).
5. **Write** - the JSON file is saved and committed. If a source can't be read, the old file is
   left untouched, so the page keeps showing the last good list.

### Google: why the list is kept for 24 hours
Google retired its old "daily trends" feed. The only feed left is the RSS feed whose title is
**Daily Search Trends**, and it only ever contains the **10 newest** trending searches (all started
within roughly the same 20 minutes). So `google_trends.py` keeps a rolling window: every topic the
feed has shown in the last **24 hours** (`GOOGLE_KEEP_HOURS`) stays in `google_trends.json` after it
leaves the feed, keeping the biggest search volume it reached. The more often the script runs, the
fuller the day's list. Topics older than 24 hours drop off.

---

## JSON format (all three files)

```json
{
  "source": "google",
  "label": "Google",
  "updated": "2026-10-04T15:07:51Z",
  "region": "US",
  "source_url": "https://trends.google.com/trending/rss?geo=US",
  "rank_basis": "search_volume",
  "keep_hours": 24,
  "count": 14,
  "items": [
    {
      "id": "titans-vs-ravens",
      "rank": 1,
      "previous_rank": 2,
      "rank_change": 1,
      "is_new": false,
      "topic": "titans vs ravens",
      "description": "50 Words or Less: Ravens Must Focus on Winning No Matter Who's Missing (Baltimore Ravens)",
      "description_source": "feed",
      "url": "https://www.google.com/search?q=titans+vs+ravens",
      "image_url": "https://...",
      "image_source": "duckduckgo",
      "started_trending": "2026-10-04T16:00:00Z",
      "first_seen": "2026-10-04T16:20:00Z",
      "last_seen": "2026-10-04T17:20:00Z",
      "hours_trending": 1.3,
      "volume": "10K+ searches",
      "volume_value": 10000,
      "volume_unit": "searches",
      "extra": {"news_source": "Baltimore Ravens"}
    }
  ]
}
```

- `rank_basis` is `platform_rank` (social media), `search_volume` (Google) or `time_trending` (Reddit).
- `rank_change` = previous rank minus new rank, so **positive = moved up**, negative = moved down,
  `0` = no change, `null` = it wasn't in the last list. `is_new` is true for a topic that wasn't in
  the previous list (on the very first run nothing is marked new).
- `started_trending` = the earlier of (the start time the feed gives, the first time this script saw
  the topic). Google gives a start time; Reddit uses the post time; the social media list gives none,
  so for that tab it is **the first time the script saw the topic**.
- `hours_trending` is measured at the time of the run; the page recalculates it live.
- `volume_unit` is `hours` (social media, Reddit: `volume_value` = hours trending) or `searches`
  (Google: `volume_value` = Google's approximate search count as a number).
- `last_seen` is the last run in which the topic was in the feed (Google keeps topics for 24 hours after this).
- `description_source` is `feed`, `gemini`, `template`, or whatever it was when carried over.
- `image_source` is `duckduckgo`, or `feed` when DuckDuckGo had no good match and the feed's own picture was used.

---

## The page

- **Tabs**: Social Media / Google / Reddit (the number is how many topics are in that file).
- **Time filter**: 1 Hour / 2 Hours / 5 Hours / 12 Hours / 24 Hours / All - keeps topics whose
  `started_trending` is that recent.
- **Sort**: *Ranking* (lowest number first, so #1 is on top) or *Newest* (most recently started).
- **Tiles** show: picture, rank pill, topic, the **whole description**, then three lines - the time
  (`3h 10m ago`), the volume (`6.5 hrs trending` or `10K+ searches`), and the source with the link.
  **Rows** is the compact table version.
- **Rank pill**: `#3` plus an arrow: green `▲ 2` moved up 2, red `▼ 1` moved down 1, `–` unchanged,
  `NEW` just appeared.
- **Stars**: tick the star on a tile and it is copied into the **Favorites** box at the top. Favorites
  are stored in your browser (localStorage), work across all three tabs, and are kept as "saved" even
  after the topic drops out of the feed. **Clear** removes them all.
- The page remembers your tab, filter, sort and layout, re-reads the JSON files every 5 minutes, and
  has a **Refresh** button.

---

## One-time setup

### 1. Put the files in the repo
Copy everything into the root of `mzaiger/TrendingTopics`. The workflows must be in a folder named
exactly **`.github/workflows`** (check the spelling: GitHub ignores any other folder name).
Delete the old files: the previous workflow file (`trends.yml`), `scrape_trends.py`, and `trends.json`.

### 2. Repository secrets
Settings -> Secrets and variables -> Actions -> **New repository secret**:

| Secret | Used by |
|---|---|
| `APIFY_TOKEN` | social media trends (Apify Console -> Settings -> API & Integrations) |
| `GEMINI_KEY` | all three (same name as your SportsDashboard secret) |

### 3. Let workflows commit
Settings -> Actions -> General -> **Workflow permissions** -> *Read and write permissions* -> Save.
(The workflows also ask for `contents: write` themselves, but an organization or repo setting can
override that.)

### 4. GitHub Pages
Settings -> Pages -> *Deploy from a branch* -> `main` / `(root)`.

### 5. A token for the Apps Script
This is what lets Google Apps Script start the workflows.

- **Fine-grained token** (recommended): GitHub -> Settings -> Developer settings -> Personal access
  tokens -> Fine-grained -> repository access: *Only select repositories* -> `mzaiger/TrendingTopics`
  -> Repository permissions -> **Contents: Read and write**. (If GitHub answers 403 or 404 later, this
  permission is the first thing to check.)
- **Classic token**: tick the `repo` scope. A classic token with `repo` works for every repo you own, so
  your existing Slickdeals token may already be enough.

In Apps Script: Project Settings (gear) -> **Script properties** -> add `GITHUB_TOKEN` = the token.

### 6. The Apps Script triggers
1. Paste `apps-script/trigger.gs` into your Apps Script project (it replaces your function).
2. Run `triggerTrendingTopicsWorkflow` once by hand (approve the permission prompt). The log should say
   *Dispatched ... successfully*, and three runs appear in the repo's **Actions** tab.
3. Triggers (clock icon) -> **Add Trigger** -> `triggerTrendingTopicsWorkflow` -> *Time-driven* ->
   *Hour timer* -> *Every hour*. Nothing runs on a schedule unless you add this.
4. *Optional, recommended for Google:* add a second trigger for `triggerFeedsOnly` every 15 or 30
   minutes. It starts only the Google and Reddit workflows (no Apify cost), and Google's feed only
   holds its 10 newest trends, so polling it more often gives a fuller day's list.

### How the trigger works
The script POSTs to `https://api.github.com/repos/mzaiger/TrendingTopics/dispatches` with an
`event_type`:

| `event_type` | Starts |
|---|---|
| `trigger-trending-pull` (or the old `trigger-expiration-pull`) | all three workflows |
| `trigger-feeds-pull` | Google + Reddit only |

The workflow files have to be on the default branch (`main`) for GitHub to see the event. A successful
call returns HTTP 204 with no body. `SKIP_HOURS` in `trigger.gs` sends `trigger-feeds-pull` instead of
`trigger-trending-pull` during the skipped hours, so social media (which costs money) pauses overnight
while Google and Reddit keep going.

---

## Cost and limits

- **Apify (social media only):** the actor charges per trend returned plus a small start fee. At 20
  trends a run that is under a cent per run; **20 runs a day is roughly $4.50 a month**, inside the
  free $5. Running a full 24 times a day is slightly over $5, which is why `SKIP_HOURS` skips 2-5 AM
  Central by default. Prices change, so check the actor's page. Each run has a hard cap
  (`APIFY_MAX_CHARGE_USD`, default $0.02). When the free credit runs out Apify refuses runs until next
  month and the page keeps showing the last list.
- **Gemini:** at most **two calls per script per run**: one for descriptions (only topics that lack one)
  and one for image search words (only when there are new topics that need a picture). Often zero for
  Google and Reddit.
- **GitHub Actions:** short jobs, free for public repos. Running `triggerFeedsOnly` every 15 minutes is
  about 200 short runs a day across two workflows; on a *private* repo that can use up the free minutes.

---

## Images (DuckDuckGo)

Each new topic gets one picture, in three steps:

1. **Search words.** DuckDuckGo only matches keywords - it can't follow instructions like "the topic is
   A and the description is B". So Gemini (one call, new topics only) turns the topic **and its
   description** into a short search query, e.g. topic `cardinals vs giants` + a Week 4 headline ->
   `Arizona Cardinals New York Giants game`. The description is used only to pick the right meaning.
   Without a Gemini key it falls back to the topic plus the most distinctive capitalised words from the
   description. Set `IMAGE_QUERY_GEMINI=0` to always use the fallback.
2. **Pick the best match.** The script asks DuckDuckGo for 12 candidates and scores each one: how many
   of the topic's words appear in the candidate's own title, how many of the query's extra words do,
   decent size and shape, minus points for logos, clip art, tiny images and stock-photo watermarks.
   If nothing shares a word with the topic, it takes no picture rather than a wrong one.
3. **Fallback.** Google topics then use Google's own news photo; Reddit uses the post's thumbnail;
   otherwise the tile shows "No Image".

Pictures are saved in the JSON and **re-used on every later run**, so only new topics are searched. To
be gentle on DuckDuckGo (same approach as `AddImageUrl.py`): one session, random delays, exponential
backoff, a cache, at most 20 searches per script per run, and a stop after 3 failures in a row. The
search is unofficial and DuckDuckGo may rate-limit GitHub's servers; the run still finishes and the next
run tries again. Pictures are linked from their original sites, so an occasional one may fail to load;
the page then shows "No Image".

---

## Settings you can change (environment variables)

| Variable | Script | Default | Meaning |
|---|---|---|---|
| `APIFY_TOKEN` | social media | - | required |
| `GEMINI_KEY` | all | - | required for social media; optional for the others |
| `APIFY_TRENDS` | social media | 20 | trends requested per run (you pay per trend) |
| `APIFY_LOCATION` | social media | US | location code |
| `APIFY_ACTOR` | social media | set in the script | actor id |
| `APIFY_MAX_CHARGE_USD` | social media | 0.02 | spending cap per run |
| `GOOGLE_TRENDS_GEO` | Google | US | country code |
| `GOOGLE_KEEP_HOURS` | Google | 24 | how long a topic stays after it leaves Google's feed |
| `REDDIT_FEED` | Reddit | r/popular RSS | any Reddit RSS URL |
| `REDDIT_TOP_N` | Reddit | 25 | posts kept |
| `REDDIT_USER_AGENT` | Reddit | descriptive default | Reddit wants a real user agent |
| `OUT_FILE` | all | the file names above | output path |
| `SKIP_IMAGES` | all | - | set to `1` to skip DuckDuckGo |
| `IMAGE_QUERY_GEMINI` | all | 1 | `0` = don't ask Gemini for image search words |
| `IMAGE_CANDIDATES` | all | 12 | pictures DuckDuckGo returns to choose from |
| `IMAGE_MAX_LOOKUPS` | all | 20 | DuckDuckGo searches per run |
| `IMAGE_MIN_DELAY` / `IMAGE_MAX_DELAY` | all | 2.0 / 5.0 | seconds between searches |
| `DDG_PROXY` | all | - | e.g. `socks5://127.0.0.1:9150` |

---

## Run it on your own computer

```bash
pip install -r requirements.txt
export GEMINI_KEY=...            # Windows PowerShell:  $env:GEMINI_KEY="..."
export APIFY_TOKEN=...           # social media only
python google_trends.py
python reddit_trends.py
python social_media_trends.py
python -m http.server 8000       # then open http://localhost:8000
```
(Opening `index.html` straight from disk won't work: browsers block `fetch` on `file://` pages.)

---

## Troubleshooting

| Problem | What to check |
|---|---|
| Actions tab shows no workflows | The folder must be `.github/workflows` and the files must be on `main`. |
| Apps Script log says 401 / 403 / 404 | Token wrong or expired, or it lacks **Contents: Read and write** on this repo. 404 also appears when the token can't see the repo. |
| Dispatch works (204) but nothing runs | The workflow files aren't on the default branch, or Actions are disabled for the repo. |
| `push failed` in a workflow log | Workflows finishing together can collide; each retries 5 times with a rebase. Re-run the failed one. |
| Reddit tab empty, log says 403 / 429 | Reddit often blocks cloud IPs. The script then leaves the old file alone. Options: run `reddit_trends.py` from your own computer, or set `REDDIT_FEED` to a different feed. |
| Social media log says Apify 402 | The free credit is used up for the month. |
| Social media log says Sotwe 403 | Sotwe blocked GitHub's IP; the script carries on with news headlines only. |
| Images missing or wrong | DuckDuckGo rate limiting (see Images), or no candidate matched the topic. Re-running later usually fills gaps; a better Gemini query is the main lever. |
| Google tab has few topics | The feed only has 10 at a time; the 24-hour window fills up as runs accumulate. Add the 15/30-minute `triggerFeedsOnly` trigger. |
| Stars disappeared | Favorites live in one browser; a different browser or a cleared cache starts empty. |
| Social media "trending for" starts at the first run | That list has no start time, so it counts from when the script first saw the topic. |

---

## Naming note

The social media tab never names the platform in the page, the JSON, or the descriptions (Gemini is
told the same). Two technical identifiers can't be renamed because other services define them: the
Apify actor id in `social_media_trends.py`, and a few strings the script must recognise on the source
pages and in the actor's output.
